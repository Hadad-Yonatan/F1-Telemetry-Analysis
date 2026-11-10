"""Local read-only Flask adapter for the telemetry library."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
from contextlib import closing
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request, url_for
from werkzeug.exceptions import HTTPException

from f1_telemetry.library import LibraryError, open_database
from f1_telemetry.library_service import ArtifactEntry, LibraryService, PageRequest, SessionFilters


_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_LOOPBACK_HOST = re.compile(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::([1-9][0-9]{0,4}))?", re.IGNORECASE)
_DIGEST = re.compile(r"[0-9a-f]{64}")
_BASENAME = re.compile(r"[a-z0-9][a-z0-9_.-]*")
_QUERY_KEYS = {
    "track_id", "created_from", "created_through", "mode", "team_id",
    "setup_fingerprint", "algorithm_version", "page", "page_size",
}
_MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json",
    ".csv": "text/csv; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
}
_PAGE_CSP = "default-src 'none'; style-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
_ARTIFACT_CSP = (
    "sandbox allow-scripts allow-downloads; default-src 'none'; connect-src 'none'; "
    "script-src 'unsafe-inline'; style-src 'unsafe-inline'; font-src data:; img-src data:; "
    "object-src 'none'; frame-ancestors 'self'; form-action 'none'; base-uri 'none'"
)


def _int(value: str | None, field: str) -> int | None:
    if value is None:
        return None
    if len(value) > 20 or re.fullmatch(r"-?[0-9]+", value) is None:
        raise ValueError(f"{field} must be an integer")
    return int(value)


def _date(value: str | None, field: str) -> date | None:
    if value is None:
        return None
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        raise ValueError(f"{field} must be a YYYY-MM-DD date")
    return date.fromisoformat(value)


def _filters() -> tuple[SessionFilters, PageRequest, dict[str, str]]:
    args = request.args
    if set(args) - _QUERY_KEYS or any(len(args.getlist(key)) != 1 for key in args):
        abort(400, description="Unknown or repeated query parameter.")
    values = {key: value for key, value in args.items() if value}
    try:
        page = _int(values.get("page"), "page")
        page_size = _int(values.get("page_size"), "page_size")
        return (
            SessionFilters(
                track_id=_int(values.get("track_id"), "track_id"),
                created_from=_date(values.get("created_from"), "created_from"),
                created_through=_date(values.get("created_through"), "created_through"),
                mode=values.get("mode"),
                team_id=_int(values.get("team_id"), "team_id"),
                setup_fingerprint=values.get("setup_fingerprint"),
                algorithm_version=values.get("algorithm_version"),
            ),
            PageRequest(page if page is not None else 1, page_size if page_size is not None else 25),
            values,
        )
    except ValueError as error:
        abort(400, description=str(error))


def _id(value: str) -> str:
    if _DIGEST.fullmatch(value) is None:
        abort(400, description="Expected a lowercase SHA-256 identifier.")
    return value


def _summary(summary) -> dict:
    value = asdict(summary)
    value["mode"] = summary.mode
    value["capture_created_unix_ns"] = str(summary.capture_created_unix_ns)
    return value


def _capture_date(nanoseconds: int) -> str:
    return (date(1970, 1, 1) + timedelta(days=nanoseconds // 86_400_000_000_000)).isoformat()


def _artifact_bytes(entry: ArtifactEntry, name: str) -> bytes:
    root, artifact = entry.bundle_root, entry.artifact
    if artifact.path != name or not _BASENAME.fullmatch(name) or not root.is_absolute():
        abort(404)
    try:
        if root.is_symlink() or root.resolve(strict=True) != root:
            abort(404)
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    abort(404)
                if info.st_size != artifact.size_bytes:
                    abort(409, description="This artifact changed since import. Verify or rebuild the index.")
                raw = stream.read(artifact.size_bytes + 1)
        finally:
            os.close(directory)
    except OSError:
        abort(404)
    if len(raw) != artifact.size_bytes or hashlib.sha256(raw).hexdigest() != artifact.sha256:
        abort(409, description="This artifact changed since import. Verify or rebuild the index.")
    return raw


def create_app(database: str | Path) -> Flask:
    service = LibraryService(Path(database).absolute())
    app = Flask(__name__, template_folder="library_templates", static_folder="library_assets")
    app.jinja_env.filters["capture_date"] = _capture_date
    app.jinja_env.globals["database_path"] = str(service.database)

    def api(data, **extra):
        return jsonify(schema_version=1, data=data, **extra)

    def session_or_404(bundle_id: str):
        session = service.get_session(_id(bundle_id))
        if session is None:
            abort(404, description="Session not found.")
        return session

    @app.before_request
    def require_loopback_host():
        # Werkzeug's trusted-list matching splits IPv6 entries at the first colon.
        match = _LOOPBACK_HOST.fullmatch(request.headers.get("Host", ""))
        if match is None or (match[1] is not None and int(match[1]) > 65535):
            if request.path.startswith("/api/v1/"):
                return jsonify(schema_version=1, error={"status": 400, "message": "Untrusted Host."}), 400
            return Response("Untrusted Host.\n", status=400, content_type="text/plain; charset=utf-8")

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("Content-Security-Policy", _PAGE_CSP)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith("/api/v1/"):
            return jsonify(schema_version=1, error={"status": error.code, "message": error.description}), error.code
        return render_template("error.html", error=error), error.code

    @app.errorhandler(sqlite3.Error)
    @app.errorhandler(LibraryError)
    def index_error(error):
        app.logger.warning("Library query failed: %s", error)
        if request.path.startswith("/api/v1/"):
            return jsonify(schema_version=1, error={"status": 503, "message": "Library index unavailable."}), 503
        return render_template("error.html", error={"code": 503, "description": "Library index unavailable. Initialize or verify the index."}), 503

    @app.get("/")
    def sessions():
        filters, page, values = _filters()
        result = service.list_sessions(filters, page)
        query = {key: value for key, value in values.items() if key != "page"}
        return render_template(
            "sessions.html", result=result, query=query,
            previous=url_for("sessions", page=result.page - 1, **query) if result.page > 1 else None,
            following=url_for("sessions", page=result.page + 1, **query)
            if result.page * result.page_size < result.total else None,
        )

    @app.get("/sessions/<bundle_id>")
    def session_page(bundle_id):
        session = session_or_404(bundle_id)
        return render_template(
            "session.html", session=session, laps=service.list_laps(bundle_id),
            comparisons=service.list_comparisons(bundle_id), corners=service.list_corners(bundle_id),
            artifacts=service.list_artifacts(bundle_id),
        )

    @app.get("/api/v1/sessions/<bundle_id>/laps/<int:number>")
    @app.get("/sessions/<bundle_id>/laps/<int:number>")
    def lap_detail(bundle_id, number):
        session = session_or_404(bundle_id)
        if not 1 <= number <= 100:
            abort(404)
        lap = service.get_lap(bundle_id, number)
        if lap is None:
            abort(404)
        if request.path.startswith("/api/"):
            return api(asdict(lap))
        return render_template("detail.html", title=f"Lap {number}", data=asdict(lap), session=session.summary)

    @app.get("/api/v1/sessions/<bundle_id>/comparisons/<int:number>")
    @app.get("/sessions/<bundle_id>/comparisons/<int:number>")
    def comparison_detail(bundle_id, number):
        session = session_or_404(bundle_id)
        if not 1 <= number <= 100:
            abort(404)
        comparison = service.get_comparison(bundle_id, number)
        if comparison is None:
            abort(404)
        if request.path.startswith("/api/"):
            return api(comparison)
        return render_template("comparison.html", session=session.summary, comparison=comparison)

    @app.get("/api/v1/sessions/<bundle_id>/comparisons/<int:number>/regions/<int:index>")
    @app.get("/sessions/<bundle_id>/comparisons/<int:number>/regions/<int:index>")
    def region_detail(bundle_id, number, index):
        session = session_or_404(bundle_id)
        if not 1 <= number <= 100:
            abort(404)
        regions = service.list_regions(bundle_id, number)
        if index >= len(regions):
            abort(404)
        if request.path.startswith("/api/"):
            return api(regions[index])
        return render_template("detail.html", title=f"Loss region {index + 1}", data=regions[index], session=session.summary)

    @app.get("/api/v1/sessions/<bundle_id>/corners/<turn_id>")
    @app.get("/sessions/<bundle_id>/corners/<turn_id>")
    def corner_detail(bundle_id, turn_id):
        session = session_or_404(bundle_id)
        corner = next((item for item in service.list_corners(bundle_id) if item["turn_id"] == turn_id), None)
        if corner is None:
            abort(404)
        if request.path.startswith("/api/"):
            return api(corner)
        return render_template("detail.html", title=f"Turn {corner['number']}", data=corner, session=session.summary)

    @app.get("/api/v1/setups/<fingerprint>")
    @app.get("/setups/<fingerprint>")
    def setup_detail(fingerprint):
        setup = service.get_setup(_id(fingerprint))
        if setup is None:
            abort(404)
        if request.path.startswith("/api/"):
            return api({"fingerprint": fingerprint, "values": setup})
        return render_template("detail.html", title="Setup details", data=setup, fingerprint=fingerprint)

    @app.get("/api/v1/bundles")
    @app.get("/api/v1/sessions")
    def api_sessions():
        filters, page, _ = _filters()
        result = service.list_sessions(filters, page)
        return api(
            [_summary(row) for row in result.items],
            pagination={"page": result.page, "page_size": result.page_size, "total": result.total},
        )

    @app.get("/api/v1/bundles/<bundle_id>")
    @app.get("/api/v1/sessions/<bundle_id>")
    def api_session(bundle_id):
        session = session_or_404(bundle_id)
        return api({
            **_summary(session.summary), "capture_sha256": session.capture_sha256,
            "capture_size_bytes": session.capture_size_bytes, "context": session.context,
        })

    @app.get("/api/v1/sessions/<bundle_id>/<collection>")
    def api_collection(bundle_id, collection):
        session = session_or_404(bundle_id)
        if collection == "laps":
            return api([asdict(lap) for lap in service.list_laps(bundle_id)])
        if collection == "comparisons":
            return api(service.list_comparisons(bundle_id))
        if collection == "corners":
            return api(service.list_corners(bundle_id))
        if collection == "artifacts":
            return api([
                {**asdict(item), "url": (
                    url_for("artifact", bundle_id=bundle_id, name=item.path)
                    if session.summary.manifested else None
                )}
                for item in service.list_artifacts(bundle_id)
            ])
        abort(404)

    @app.get("/api/v1/sessions/<bundle_id>/comparisons/<int:number>/regions")
    def api_regions(bundle_id, number):
        session_or_404(bundle_id)
        if not 1 <= number <= 100 or service.get_comparison(bundle_id, number) is None:
            abort(404)
        return api(service.list_regions(bundle_id, number))

    @app.get("/reports/<bundle_id>")
    def report_page(bundle_id):
        bundle_id = _id(bundle_id)
        if service.get_artifact(bundle_id, "report.html", manifested_only=True) is None:
            abort(404)
        response = app.make_response(render_template("report_frame.html", bundle_id=bundle_id))
        response.headers["Content-Security-Policy"] = _PAGE_CSP + "; frame-src 'self'"
        return response

    @app.get("/artifacts/<bundle_id>/<name>")
    def artifact(bundle_id, name):
        mime_type = _MIME_TYPES.get(Path(name).suffix)
        if mime_type is None or _BASENAME.fullmatch(name) is None:
            abort(404)
        entry = service.get_artifact(_id(bundle_id), name, manifested_only=True)
        if entry is None:
            abort(404)
        response = Response(_artifact_bytes(entry, name), content_type=mime_type)
        response.headers["Content-Security-Policy"] = _ARTIFACT_CSP
        # Executable artifacts are downloads except for the verified report frame.
        inline_report = (
            name == "report.html" and request.args.get("embed") == "1"
            and request.headers.get("Sec-Fetch-Dest") == "iframe"
            and request.headers.get("Sec-Fetch-Site") == "same-origin"
        )
        if name.endswith((".html", ".svg")) and not inline_report:
            response.headers["Content-Disposition"] = f'attachment; filename="{name}"'
        return response

    return app


def serve(database: Path, host: str, port: int) -> None:
    if host not in _LOOPBACK:
        raise LibraryError("host must be localhost, 127.0.0.1, or ::1")
    if not 1 <= port <= 65535:
        raise LibraryError("port must be between 1 and 65535")
    with closing(open_database(database)):
        pass
    print(f"Telemetry library: {database.absolute()}", flush=True)
    bind_host = "127.0.0.1" if host == "localhost" else host
    create_app(database).run(host=bind_host, port=port, debug=False, use_reloader=False)
