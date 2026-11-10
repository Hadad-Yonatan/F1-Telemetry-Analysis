"""Framework-independent read-only queries over the local telemetry index."""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Generic, TypeVar

from f1_telemetry.bundle import Artifact, digest_value, integer_value
from f1_telemetry.library import DEFAULT_DATABASE, open_database


_MAX_SQLITE_INTEGER = (1 << 63) - 1
_UNIX_EPOCH = date(1970, 1, 1)
_DAY_NS = 86_400_000_000_000
# EA F1 25 v3 Session Types appendix, not a claim about policy support.
_SESSION_TYPES = {
    "practice": tuple(range(1, 5)),
    "qualifying": tuple(range(5, 15)),
    "race": (15, 16, 17),
    "time-trial": (18,),
}
_MODE_BY_TYPE = {code: mode for mode, codes in _SESSION_TYPES.items() for code in codes}
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class SessionFilters:
    track_id: int | None = None
    created_from: date | None = None
    created_through: date | None = None
    mode: str | None = None
    team_id: int | None = None
    setup_fingerprint: str | None = None
    algorithm_version: str | None = None

    def __post_init__(self) -> None:
        if self.track_id is not None:
            integer_value(self.track_id, "track ID", -1, 255)
        if self.team_id is not None:
            integer_value(self.team_id, "team ID", 0, 255)
        for label, value in (("created_from", self.created_from), ("created_through", self.created_through)):
            if value is not None and type(value) is not date:
                raise ValueError(f"{label} must be a calendar date")
        if self.created_from is not None and self.created_through is not None and self.created_from > self.created_through:
            raise ValueError("created_from cannot be later than created_through")
        if self.mode is not None and (not isinstance(self.mode, str) or self.mode not in _SESSION_TYPES):
            raise ValueError("mode must be practice, qualifying, race, or time-trial")
        if self.setup_fingerprint is not None:
            digest_value(self.setup_fingerprint, "setup fingerprint")
        if self.algorithm_version is not None and (
            not isinstance(self.algorithm_version, str) or not self.algorithm_version.strip()
        ):
            raise ValueError("algorithm version must be nonempty")


@dataclass(frozen=True, slots=True)
class PageRequest:
    page: int = 1
    page_size: int = 25

    def __post_init__(self) -> None:
        integer_value(self.page_size, "page size", 1, 100)
        integer_value(self.page, "page", 1, _MAX_SQLITE_INTEGER // self.page_size + 1)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


@dataclass(frozen=True, slots=True)
class Page(Generic[_T]):
    items: tuple[_T, ...]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class SessionSummary:
    bundle_id: str
    session_uid: str
    track_id: int
    track_length_m: int
    track_name: str | None
    session_type: int | None
    team_id: int | None
    algorithm_version: str
    capture_basename: str
    capture_created_unix_ns: int
    manifested: bool

    @property
    def mode(self) -> str | None:
        return _MODE_BY_TYPE.get(self.session_type)


@dataclass(frozen=True, slots=True)
class SessionDetail:
    summary: SessionSummary
    capture_sha256: str
    capture_size_bytes: int
    context: dict | None


@dataclass(frozen=True, slots=True)
class LapEntry:
    number: int
    eligible: bool
    official_lap_time_ms: int
    setup_fingerprint: str | None
    details: dict
    context: dict | None


@dataclass(frozen=True, slots=True)
class ArtifactEntry:
    bundle_id: str
    bundle_root: Path
    artifact: Artifact


_SUMMARY_COLUMNS = """
    b.bundle_id, s.session_uid, s.track_id, s.track_length_m, s.track_name,
    s.session_type, s.team_id, b.algorithm_version, b.capture_basename,
    c.created_unix_ns, b.manifested
"""
_SESSION_TABLES = """
    FROM bundles AS b JOIN sessions AS s USING (bundle_id)
    JOIN captures AS c ON c.sha256=b.capture_sha256
"""


def _summary(row: tuple) -> SessionSummary:
    return SessionSummary(*row[:-1], bool(row[-1]))


def _where(filters: SessionFilters) -> tuple[str, tuple]:
    terms, values = [], []
    for column, value in (
        ("s.track_id", filters.track_id),
        ("s.team_id", filters.team_id),
        ("b.algorithm_version", filters.algorithm_version),
    ):
        if value is not None:
            terms.append(f"{column} = ?")
            values.append(value)
    if filters.mode is not None:
        codes = _SESSION_TYPES[filters.mode]
        terms.append("s.session_type IN (" + ",".join("?" for _ in codes) + ")")
        values.extend(codes)
    if filters.setup_fingerprint is not None:
        terms.append(
            "EXISTS (SELECT 1 FROM laps WHERE bundle_id=b.bundle_id AND eligible=1 AND setup_fingerprint=?)"
        )
        values.append(filters.setup_fingerprint)
    if filters.created_from is not None:
        start = (filters.created_from - _UNIX_EPOCH).days * _DAY_NS
        if start > _MAX_SQLITE_INTEGER:
            terms.append("0")
        elif start > 0:
            terms.append("c.created_unix_ns >= ?")
            values.append(start)
    if filters.created_through is not None:
        end = ((filters.created_through - _UNIX_EPOCH).days + 1) * _DAY_NS
        if end <= 0:
            terms.append("0")
        elif end <= _MAX_SQLITE_INTEGER:
            terms.append("c.created_unix_ns < ?")
            values.append(end)
    return (" WHERE " + " AND ".join(terms) if terms else ""), tuple(values)


def _decoded(value: str | None) -> dict | None:
    return json.loads(value) if value is not None else None


class LibraryService:
    """Read the current on-disk index without opening it for writing."""

    def __init__(self, database: str | Path = DEFAULT_DATABASE) -> None:
        self.database = Path(database)

    def _rows(self, sql: str, values: tuple) -> list[tuple]:
        with closing(open_database(self.database)) as connection:
            return connection.execute(sql, values).fetchall()

    def list_sessions(
        self, filters: SessionFilters = SessionFilters(), page: PageRequest = PageRequest()
    ) -> Page[SessionSummary]:
        where, values = _where(filters)
        with closing(open_database(self.database)) as connection:
            connection.execute("BEGIN")
            total = connection.execute("SELECT count(*) " + _SESSION_TABLES + where, values).fetchone()[0]
            rows = connection.execute(
                f"SELECT {_SUMMARY_COLUMNS} {_SESSION_TABLES} {where} "
                "ORDER BY c.created_unix_ns DESC, b.bundle_id ASC LIMIT ? OFFSET ?",
                (*values, page.page_size, page.offset),
            ).fetchall()
        return Page(tuple(map(_summary, rows)), total, page.page, page.page_size)

    def get_session(self, bundle_id: str) -> SessionDetail | None:
        rows = self._rows(
            f"SELECT {_SUMMARY_COLUMNS}, c.sha256, c.size_bytes, s.context_json "
            f"{_SESSION_TABLES} WHERE b.bundle_id=?",
            (digest_value(bundle_id, "bundle ID"),),
        )
        if not rows:
            return None
        row = rows[0]
        return SessionDetail(_summary(row[:-3]), row[-3], row[-2], _decoded(row[-1]))

    def list_laps(self, bundle_id: str) -> tuple[LapEntry, ...]:
        rows = self._rows(
            "SELECT lap_number, eligible, official_lap_time_ms, setup_fingerprint, lap_json, context_json "
            "FROM laps WHERE bundle_id=? ORDER BY lap_number",
            (digest_value(bundle_id, "bundle ID"),),
        )
        return tuple(
            LapEntry(number, bool(eligible), time, fingerprint, json.loads(lap), _decoded(context))
            for number, eligible, time, fingerprint, lap, context in rows
        )

    def get_lap(self, bundle_id: str, lap_number: int) -> LapEntry | None:
        number = integer_value(lap_number, "lap number", 1, 100)
        return next((lap for lap in self.list_laps(bundle_id) if lap.number == number), None)

    def list_comparisons(self, bundle_id: str) -> tuple[dict, ...]:
        rows = self._rows(
            "SELECT comparison_json FROM comparisons WHERE bundle_id=? ORDER BY comparison_lap",
            (digest_value(bundle_id, "bundle ID"),),
        )
        return tuple(json.loads(row[0]) for row in rows)

    def get_comparison(self, bundle_id: str, lap_number: int) -> dict | None:
        rows = self._rows(
            "SELECT comparison_json FROM comparisons WHERE bundle_id=? AND comparison_lap=?",
            (digest_value(bundle_id, "bundle ID"), integer_value(lap_number, "comparison lap", 1, 100)),
        )
        return json.loads(rows[0][0]) if rows else None

    def list_regions(self, bundle_id: str, lap_number: int) -> tuple[dict, ...]:
        rows = self._rows(
            "SELECT region_json FROM regions WHERE bundle_id=? AND comparison_lap=? ORDER BY region_index",
            (digest_value(bundle_id, "bundle ID"), integer_value(lap_number, "comparison lap", 1, 100)),
        )
        return tuple(json.loads(row[0]) for row in rows)

    def list_corners(self, bundle_id: str) -> tuple[dict, ...]:
        rows = self._rows(
            "SELECT turn_json FROM corners WHERE bundle_id=? ORDER BY turn_number, turn_id",
            (digest_value(bundle_id, "bundle ID"),),
        )
        return tuple(json.loads(row[0]) for row in rows)

    def get_setup(self, fingerprint: str) -> dict | None:
        rows = self._rows(
            "SELECT values_json FROM setups WHERE fingerprint=?",
            (digest_value(fingerprint, "setup fingerprint"),),
        )
        return json.loads(rows[0][0]) if rows else None

    def list_artifacts(self, bundle_id: str) -> tuple[Artifact, ...]:
        rows = self._rows(
            "SELECT path, size_bytes, sha256 FROM artifacts WHERE bundle_id=? ORDER BY path",
            (digest_value(bundle_id, "bundle ID"),),
        )
        return tuple(Artifact(*row) for row in rows)

    def get_artifact(self, bundle_id: str, path: str) -> ArtifactEntry | None:
        bundle_id = digest_value(bundle_id, "bundle ID")
        rows = self._rows(
            "SELECT b.root, a.path, a.size_bytes, a.sha256 FROM artifacts AS a "
            "JOIN bundles AS b USING(bundle_id) WHERE a.bundle_id=? AND a.path=?",
            (bundle_id, path),
        )
        return ArtifactEntry(bundle_id, Path(rows[0][0]), Artifact(*rows[0][1:])) if rows else None
