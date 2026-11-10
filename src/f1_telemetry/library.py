"""Local, rebuildable SQLite index of verified analysis bundles."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Sequence

from f1_telemetry.bundle import (
    Artifact,
    BundleError,
    VerifiedBundle,
    array_value,
    digest_value,
    integer_value,
    object_value,
    verify_bundle,
)

DEFAULT_DATABASE = Path("data/library/telemetry.sqlite3")
DEFAULT_BUNDLE_ROOT = Path("data/analysis")
DATABASE_VERSION = 1
_APPLICATION_ID = 0x4631544C  # F1TL

_SCHEMA = """
CREATE TABLE captures (
    sha256 TEXT PRIMARY KEY, size_bytes INTEGER NOT NULL, created_unix_ns INTEGER NOT NULL
);
CREATE TABLE bundles (
    bundle_id TEXT PRIMARY KEY, root TEXT NOT NULL UNIQUE, manifested INTEGER NOT NULL,
    algorithm_version TEXT NOT NULL, capture_sha256 TEXT NOT NULL REFERENCES captures(sha256),
    capture_basename TEXT NOT NULL
);
CREATE TABLE artifacts (
    bundle_id TEXT NOT NULL REFERENCES bundles(bundle_id) ON DELETE CASCADE,
    path TEXT NOT NULL, size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
    PRIMARY KEY (bundle_id, path)
);
CREATE TABLE sessions (
    bundle_id TEXT PRIMARY KEY REFERENCES bundles(bundle_id) ON DELETE CASCADE,
    session_uid TEXT NOT NULL, track_id INTEGER NOT NULL, track_length_m INTEGER NOT NULL,
    track_name TEXT, session_type INTEGER, team_id INTEGER, context_json TEXT
);
CREATE TABLE setups (fingerprint TEXT PRIMARY KEY, values_json TEXT NOT NULL);
CREATE TABLE laps (
    bundle_id TEXT NOT NULL REFERENCES sessions(bundle_id) ON DELETE CASCADE,
    lap_number INTEGER NOT NULL, eligible INTEGER NOT NULL, official_lap_time_ms INTEGER NOT NULL,
    setup_fingerprint TEXT REFERENCES setups(fingerprint), lap_json TEXT NOT NULL,
    context_json TEXT, PRIMARY KEY (bundle_id, lap_number)
);
CREATE TABLE comparisons (
    bundle_id TEXT NOT NULL, comparison_lap INTEGER NOT NULL, reference_lap INTEGER NOT NULL,
    official_delta_s REAL NOT NULL, comparison_json TEXT NOT NULL,
    PRIMARY KEY (bundle_id, comparison_lap),
    FOREIGN KEY (bundle_id, comparison_lap) REFERENCES laps(bundle_id, lap_number),
    FOREIGN KEY (bundle_id, reference_lap) REFERENCES laps(bundle_id, lap_number)
);
CREATE TABLE corners (
    bundle_id TEXT NOT NULL REFERENCES sessions(bundle_id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL, turn_number INTEGER NOT NULL, turn_json TEXT NOT NULL,
    PRIMARY KEY (bundle_id, turn_id)
);
CREATE TABLE regions (
    bundle_id TEXT NOT NULL, comparison_lap INTEGER NOT NULL, region_index INTEGER NOT NULL,
    region_json TEXT NOT NULL, PRIMARY KEY (bundle_id, comparison_lap, region_index),
    FOREIGN KEY (bundle_id, comparison_lap) REFERENCES comparisons(bundle_id, comparison_lap)
);
CREATE TABLE region_corners (
    bundle_id TEXT NOT NULL, comparison_lap INTEGER NOT NULL, region_index INTEGER NOT NULL,
    turn_id TEXT NOT NULL, is_primary INTEGER NOT NULL,
    PRIMARY KEY (bundle_id, comparison_lap, region_index, turn_id),
    FOREIGN KEY (bundle_id, comparison_lap, region_index)
        REFERENCES regions(bundle_id, comparison_lap, region_index),
    FOREIGN KEY (bundle_id, turn_id) REFERENCES corners(bundle_id, turn_id)
);
CREATE TABLE events (
    bundle_id TEXT NOT NULL, comparison_lap INTEGER NOT NULL, role TEXT NOT NULL,
    event_index INTEGER NOT NULL, event_json TEXT NOT NULL,
    PRIMARY KEY (bundle_id, comparison_lap, role, event_index),
    FOREIGN KEY (bundle_id, comparison_lap) REFERENCES comparisons(bundle_id, comparison_lap)
);
CREATE INDEX sessions_filter ON sessions(track_id, session_type, team_id);
CREATE INDEX laps_setup ON laps(setup_fingerprint, eligible, bundle_id);
CREATE INDEX bundles_capture ON bundles(capture_sha256);
"""


class LibraryError(ValueError):
    """The library database is invalid or cannot accept the operation."""


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise BundleError(f"{name} must be a nonempty string")
    return value


def _optional_integer(value: object, name: str) -> int | None:
    return None if value is None else integer_value(value, name)


def _session_uid(value: object) -> str:
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 20:
        if str(int(value)) != value:
            raise BundleError("session UID must use canonical decimal")
        value = int(value)
    return str(integer_value(value, "session UID", 0, (1 << 64) - 1))


def _database_path(path: str | Path) -> Path:
    database = Path(path)
    if database.is_symlink():
        raise LibraryError("database path must not be a symlink")
    return database.resolve()


@contextmanager
def _writer_lock(database: Path):
    lock = database.with_name(f".{database.name}.lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "rb+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _check_version(connection: sqlite3.Connection) -> None:
    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if application_id != _APPLICATION_ID or version != DATABASE_VERSION:
        raise LibraryError(f"not a supported telemetry library (schema {version})")


def open_database(path: str | Path, *, read_only: bool = True) -> sqlite3.Connection:
    database = _database_path(path)
    mode = "ro" if read_only else "rw"
    connection = sqlite3.connect(f"{database.as_uri()}?mode={mode}", uri=True)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        if read_only:
            connection.execute("PRAGMA query_only=ON")
        _check_version(connection)
    except BaseException:
        connection.close()
        raise
    return connection


def _initialize(database: Path) -> None:
    if database.exists():
        with closing(open_database(database)):
            return
    with tempfile.TemporaryDirectory(prefix=".library-init-", dir=database.parent) as scratch:
        staged = Path(scratch) / "library.sqlite3"
        with closing(sqlite3.connect(staged)) as connection:
            connection.executescript(
                f"BEGIN; {_SCHEMA} PRAGMA application_id={_APPLICATION_ID}; "
                f"PRAGMA user_version={DATABASE_VERSION}; COMMIT;"
            )
        # Link the finished database without overwriting a concurrently created destination.
        os.link(staged, database)


def init_library(path: str | Path = DEFAULT_DATABASE) -> None:
    database = _database_path(path)
    database.parent.mkdir(parents=True, exist_ok=True)
    with _writer_lock(database):
        _initialize(database)


def _registered_artifacts(connection: sqlite3.Connection, bundle_id: str) -> tuple[Artifact, ...]:
    return tuple(Artifact(*row) for row in connection.execute(
        "SELECT path, size_bytes, sha256 FROM artifacts WHERE bundle_id=? ORDER BY path",
        (bundle_id,),
    ))


def _lap_contexts(context: dict, uid: str, laps: set[int]) -> tuple[dict[int, dict], list[dict]]:
    integer_value(context.get("schema_version"), "context schema", 1, 1)
    if _session_uid(context.get("session_uid")) != uid:
        raise BundleError("context session UID differs from analysis")
    timeline = [
        object_value(value, "snapshot")
        for value in array_value(context.get("timeline"), "context timeline")
    ]
    result = {}
    for value in array_value(context.get("laps"), "lap contexts"):
        row = object_value(value, "lap context")
        number = integer_value(row.get("lap_number"), "context lap number", 1, 100)
        if number not in laps or number in result:
            raise BundleError("context references an unknown or duplicate lap")
        result[number] = row
    return result, timeline


def _snapshot(context: dict, timeline: list[dict], kind: str) -> dict | None:
    indices = object_value(context.get("start_indices"), "lap start indices")
    index = indices.get(kind)
    if index is None:
        return None
    index = integer_value(index, "lap start index", 0, len(timeline) - 1)
    snapshot = timeline[index]
    if snapshot.get("kind") != kind:
        raise BundleError(f"lap start index does not point to a {kind} snapshot")
    value = snapshot.get("value")
    return None if value is None else object_value(value, kind)


def _index_bundle(connection: sqlite3.Connection, bundle: VerifiedBundle) -> bool:
    bundle_id = bundle.bundle_id
    existing = connection.execute("SELECT bundle_id FROM bundles WHERE bundle_id=?", (bundle_id,)).fetchone()
    if existing:
        if _registered_artifacts(connection, bundle_id) != bundle.artifacts:
            raise BundleError("different artifacts claim an already indexed bundle ID")
        return False

    doc = bundle.document
    capture = object_value(doc.get("capture"), "capture")
    capture_sha = digest_value(capture.get("sha256"), "capture digest")
    capture_size = integer_value(capture.get("size_bytes"), "capture size")
    created = integer_value(capture.get("created_unix_ns"), "capture timestamp")
    basename = _text(capture.get("basename"), "capture basename")
    if basename in (".", "..") or "/" in basename or "\\" in basename or "\x00" in basename:
        raise BundleError("capture basename must not be a path")
    connection.execute("INSERT OR IGNORE INTO captures VALUES (?, ?, ?)", (capture_sha, capture_size, created))
    if connection.execute(
        "SELECT size_bytes, created_unix_ns FROM captures WHERE sha256=?", (capture_sha,)
    ).fetchone() != (capture_size, created):
        raise BundleError("conflicting metadata for the same capture digest")
    algorithm = object_value(doc.get("algorithm"), "analysis algorithm")
    connection.execute("INSERT INTO bundles VALUES (?, ?, ?, ?, ?, ?)", (
        bundle_id, str(bundle.root), int(bundle.manifested),
        _text(algorithm.get("version"), "algorithm version"), capture_sha, basename,
    ))
    connection.executemany("INSERT INTO artifacts VALUES (?, ?, ?, ?)", (
        (bundle_id, artifact.path, artifact.size_bytes, artifact.sha256)
        for artifact in bundle.artifacts
    ))

    session = object_value(doc.get("session"), "session")
    uid = _session_uid(session.get("session_uid"))
    track_id = integer_value(session.get("track_id"), "track ID", -1, 255)
    length = integer_value(session.get("track_length_m"), "track length", 1)
    catalog = object_value(doc.get("corner_catalog") or {}, "corner catalog")
    track = object_value(catalog.get("selected_track") or {}, "selected track")
    if track and (track.get("track_id"), track.get("track_length_m")) != (track_id, length):
        raise BundleError("selected catalog layout does not match the session")
    track_name = _text(track.get("display_name"), "track name") if track else None
    laps = {
        row["lap_number"]: row
        for row in array_value(doc.get("lap_eligibility"), "lap eligibility")
    }
    context = doc.get("session_context")
    lap_contexts, timeline, session_type, team_id = {}, [], None, None
    if context is not None:
        context = object_value(context, "session context")
        lap_contexts, timeline = _lap_contexts(
            context, uid, {number for number, row in laps.items() if row["eligible"]},
        )
        settings = context.get("settings")
        if settings is not None:
            session_type = _optional_integer(object_value(settings, "settings").get("session_type"), "session type")
        for number in sorted(lap_contexts):
            participant = _snapshot(lap_contexts[number], timeline, "participant")
            if participant is not None:
                team_id = _optional_integer(participant.get("team_id"), "team ID")
                break
    connection.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
        bundle_id, uid, track_id, length, track_name, session_type, team_id,
        _json(context) if context is not None else None,
    ))
    for number, lap in sorted(laps.items()):
        lap_context = lap_contexts.get(number)
        setup = _snapshot(lap_context, timeline, "setup") if lap_context is not None else None
        fingerprint = None
        if setup is not None:
            fingerprint = digest_value(setup.get("fingerprint"), "setup fingerprint")
            values = _json(object_value(setup.get("values"), "setup values"))
            connection.execute("INSERT OR IGNORE INTO setups VALUES (?, ?)", (fingerprint, values))
            if connection.execute(
                "SELECT values_json FROM setups WHERE fingerprint=?", (fingerprint,)
            ).fetchone() != (values,):
                raise BundleError("conflicting setup values for the same fingerprint")
        connection.execute("INSERT INTO laps VALUES (?, ?, ?, ?, ?, ?, ?)", (
            bundle_id, number, int(lap["eligible"]),
            integer_value(lap.get("official_lap_time_ms"), "official lap time"),
            fingerprint, _json(lap), _json(lap_context) if lap_context is not None else None,
        ))
    for value in array_value(track.get("turns", []), "turns"):
        turn = object_value(value, "turn")
        connection.execute("INSERT INTO corners VALUES (?, ?, ?, ?)", (
            bundle_id, _text(turn.get("turn_id"), "turn ID"),
            integer_value(turn.get("number"), "turn number", 1), _json(turn),
        ))
    for comparison in doc["comparisons"]:
        number = comparison["comparison_lap"]
        ref = comparison["reference_lap"]
        delta = comparison.get("official_delta_s")
        if type(delta) not in (int, float):
            raise BundleError("official delta must be numeric")
        connection.execute("INSERT INTO comparisons VALUES (?, ?, ?, ?, ?)", (
            bundle_id, number, ref, delta, _json(comparison),
        ))
        for index, value in enumerate(array_value(comparison.get("loss_regions"), "loss regions")):
            region = object_value(value, "loss region")
            connection.execute("INSERT INTO regions VALUES (?, ?, ?, ?)", (bundle_id, number, index, _json(region)))
            corner_context = region.get("corner_context")
            if corner_context is not None:
                corner_context = object_value(corner_context, "region corner context")
                primary = _text(corner_context.get("primary_turn_id"), "primary turn ID")
                turn_ids = array_value(corner_context.get("overlapping_turn_ids"), "overlapping turns")
                if primary not in turn_ids:
                    raise BundleError("primary turn is not among overlapping turns")
                connection.executemany("INSERT INTO region_corners VALUES (?, ?, ?, ?, ?)", (
                    (bundle_id, number, index, _text(turn, "turn ID"), int(turn == primary))
                    for turn in turn_ids
                ))
        events = comparison.get("driving_events")
        if events is not None:
            events = object_value(events, "driving events")
            for role, key in (("reference", "event_index"), ("comparison", "event_index"), ("matches", "match_index")):
                for value in array_value(events.get(role), f"{role} driving events"):
                    event = object_value(value, "event")
                    connection.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?)", (
                        bundle_id, number, role, integer_value(event.get(key), "event index"), _json(event),
                    ))
    return True


def import_bundles(path: str | Path, roots: Sequence[str | Path]) -> tuple[int, int]:
    """Verify and index all requested bundles in a single transaction."""
    database = _database_path(path)
    with _writer_lock(database):
        return _import_bundles(database, roots)


def _import_bundles(database: Path, roots: Sequence[str | Path]) -> tuple[int, int]:
    with closing(open_database(database, read_only=False)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        imported = sum(_index_bundle(connection, verify_bundle(root)) for root in roots)
    return imported, len(roots) - imported


def rebuild_library(path: str | Path, bundle_root: str | Path = DEFAULT_BUNDLE_ROOT) -> tuple[int, int]:
    """Replace an existing library only after a complete successful rebuild."""
    database = _database_path(path)
    root = Path(bundle_root)
    if root.is_symlink() or not root.is_dir():
        raise LibraryError("bundle root must be a regular directory")
    roots = sorted({
        entry.parent
        for filename in ("analysis.json", "bundle.json")
        for entry in root.rglob(filename)
        if all(not part.startswith(".") for part in entry.relative_to(root).parts)
    })
    if not roots:
        raise LibraryError("no analysis bundles found")
    database.parent.mkdir(parents=True, exist_ok=True)
    with _writer_lock(database), tempfile.TemporaryDirectory(
        prefix=".library-rebuild-", dir=database.parent
    ) as scratch:
        if database.exists():
            with closing(open_database(database)):
                pass
        staged = Path(scratch) / "library.sqlite3"
        _initialize(staged)
        counts = _import_bundles(staged, roots)
        verify_library(staged)
        if database.exists():
            with closing(open_database(database, read_only=False)) as old, old:
                old.execute("BEGIN IMMEDIATE")
                os.replace(staged, database)
        else:
            os.link(staged, database)
    return counts


def verify_library(path: str | Path = DEFAULT_DATABASE) -> int:
    with closing(open_database(path)) as connection:
        connection.execute("BEGIN")
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise LibraryError("database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise LibraryError("database has broken references")
        rows = connection.execute("SELECT bundle_id, root, manifested FROM bundles ORDER BY bundle_id")
        count = 0
        for bundle_id, root, manifested in rows:
            bundle = verify_bundle(root)
            if (bundle.root != Path(root) or bundle.bundle_id != bundle_id or bundle.manifested != bool(manifested)
                or bundle.artifacts != _registered_artifacts(connection, bundle_id)):
                raise LibraryError(f"indexed bundle changed: {bundle_id}")
            count += 1
        return count


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "import", "rebuild", "verify", "serve"):
        command = commands.add_parser(name)
        command.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
        if name == "import":
            command.add_argument("bundles", metavar="BUNDLE", nargs="+", type=Path)
        elif name == "rebuild":
            command.add_argument("bundle_root", metavar="BUNDLE_ROOT", nargs="?", type=Path, default=DEFAULT_BUNDLE_ROOT)
        elif name == "serve":
            command.add_argument("--host", default="127.0.0.1")
            command.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            init_library(args.database)
            print(f"Library initialized: {args.database}")
        elif args.command == "verify":
            print(f"Verified {verify_library(args.database)} indexed bundles")
        elif args.command == "serve":
            try:
                from f1_telemetry.library_web import serve
            except ModuleNotFoundError as error:
                if error.name != "flask":
                    raise
                raise LibraryError("Install the optional library extra to use serve.") from error
            serve(args.database, args.host, args.port)
        else:
            imported, skipped = (
                import_bundles(args.database, args.bundles)
                if args.command == "import"
                else rebuild_library(args.database, args.bundle_root)
            )
            print(f"Indexed {imported} bundles; skipped {skipped} duplicates")
    except (OSError, sqlite3.Error, ValueError, OverflowError) as error:
        print(f"Library error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
