"""Relational indexing of the consistency artifact without storing the file."""

from __future__ import annotations

import json
import math
import sqlite3

from f1_telemetry.bundle import BundleError, array_value, integer_value, object_value
from f1_telemetry.consistency import MINIMUM_LAPS


CONSISTENCY_SCHEMA = """
CREATE TABLE consistency_runs (
    bundle_id TEXT PRIMARY KEY REFERENCES sessions(bundle_id) ON DELETE CASCADE,
    schema_version INTEGER NOT NULL, method_version TEXT NOT NULL,
    status TEXT NOT NULL, reason TEXT, corner_status TEXT NOT NULL, corner_reason TEXT,
    slope_s_per_attempt REAL, intercept_s REAL
);
CREATE INDEX consistency_filter ON consistency_runs(status, corner_status, bundle_id);
CREATE TABLE consistency_laps (
    bundle_id TEXT NOT NULL REFERENCES consistency_runs(bundle_id) ON DELETE CASCADE,
    lap_number INTEGER NOT NULL, ordinal INTEGER NOT NULL, official_time_s REAL NOT NULL,
    sector1_s REAL NOT NULL, sector2_s REAL NOT NULL, sector3_s REAL NOT NULL,
    PRIMARY KEY (bundle_id, lap_number), UNIQUE (bundle_id, ordinal),
    FOREIGN KEY (bundle_id, lap_number) REFERENCES laps(bundle_id, lap_number)
);
CREATE TABLE consistency_corners (
    bundle_id TEXT NOT NULL REFERENCES consistency_runs(bundle_id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL, event_status TEXT NOT NULL, event_reason TEXT,
    PRIMARY KEY (bundle_id, turn_id),
    FOREIGN KEY (bundle_id, turn_id) REFERENCES corners(bundle_id, turn_id)
);
CREATE TABLE consistency_corner_laps (
    bundle_id TEXT NOT NULL, turn_id TEXT NOT NULL, lap_number INTEGER NOT NULL,
    duration_s REAL NOT NULL, event_status TEXT NOT NULL, selected_event_index INTEGER,
    summary_event_index INTEGER,
    PRIMARY KEY (bundle_id, turn_id, lap_number),
    FOREIGN KEY (bundle_id, turn_id) REFERENCES consistency_corners(bundle_id, turn_id) ON DELETE CASCADE,
    FOREIGN KEY (bundle_id, lap_number) REFERENCES consistency_laps(bundle_id, lap_number)
);
CREATE TABLE consistency_events (
    bundle_id TEXT NOT NULL, turn_id TEXT NOT NULL, lap_number INTEGER NOT NULL,
    event_index INTEGER NOT NULL, braking_start_distance REAL NOT NULL,
    braking_end_distance REAL NOT NULL, braking_distance REAL NOT NULL, peak_brake REAL NOT NULL,
    minimum_speed_distance REAL NOT NULL, minimum_speed_kph REAL NOT NULL,
    throttle_reapplication_distance REAL, coast_distance REAL NOT NULL, analysis_end_distance REAL NOT NULL,
    PRIMARY KEY (bundle_id, turn_id, lap_number, event_index),
    FOREIGN KEY (bundle_id, turn_id, lap_number)
        REFERENCES consistency_corner_laps(bundle_id, turn_id, lap_number) ON DELETE CASCADE
);
CREATE TABLE consistency_timing_summaries (
    bundle_id TEXT NOT NULL REFERENCES consistency_runs(bundle_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL, minimum REAL NOT NULL, maximum REAL NOT NULL,
    median REAL NOT NULL, mad REAL NOT NULL, PRIMARY KEY (bundle_id, ordinal)
);
CREATE TABLE consistency_corner_summaries (
    bundle_id TEXT NOT NULL, turn_id TEXT NOT NULL, ordinal INTEGER NOT NULL, metric TEXT NOT NULL,
    minimum REAL NOT NULL, maximum REAL NOT NULL, median REAL NOT NULL, mad REAL NOT NULL,
    PRIMARY KEY (bundle_id, turn_id, ordinal), UNIQUE (bundle_id, turn_id, metric),
    FOREIGN KEY (bundle_id, turn_id) REFERENCES consistency_corners(bundle_id, turn_id) ON DELETE CASCADE
);
"""

_DISTRIBUTION = ("minimum", "maximum", "median", "mad")
_EVENT_METRICS = (
    "braking_start_distance", "braking_end_distance", "braking_distance", "peak_brake",
    "minimum_speed_distance", "minimum_speed_kph", "throttle_reapplication_distance", "coast_distance",
)
_EVENT_FIELDS = ("event_index", *_EVENT_METRICS, "analysis_end_distance")
_NOT_ENOUGH = "fewer_than_three_eligible_laps"
_EVENT_REASON = "requires_one_complete_event_per_eligible_lap"


def _number(value: object, field: str, *, signed: bool = False) -> float:
    if type(value) not in (int, float) or abs(value) > 1e100 or not math.isfinite(value) or (not signed and value < 0):
        raise BundleError(f"invalid consistency value: {field}")
    return float(value)


def _distribution(value: object) -> tuple[float, float, float, float]:
    summary = object_value(value, "consistency distribution")
    low, high, center, mad = (_number(summary.get(field), field) for field in _DISTRIBUTION)
    if not low <= center <= high or mad > high - low:
        raise BundleError("inconsistent distribution bounds")
    return low, high, center, mad


def _expect(value: object, expected: object, field: str) -> None:
    if value != expected:
        raise BundleError(f"consistency {field} does not match eligible laps or availability")


def _index_events(
    connection: sqlite3.Connection, key: tuple[str, str, int], sample: dict, assigned: set[tuple[int, int]],
) -> None:
    rows = []
    for value in array_value(sample.get("events"), "consistency event candidates"):
        event = object_value(value, "consistency event candidate")
        index = integer_value(event.get("event_index"), "event index")
        fields = tuple(
            None if field == "throttle_reapplication_distance" and event.get(field) is None
            else _number(event.get(field), field)
            for field in _EVENT_FIELDS[1:]
        )
        if rows and index <= rows[-1][0]:
            raise BundleError("consistency event indices must be unique and sorted")
        if (key[2], index) in assigned:
            raise BundleError("a consistency event cannot belong to two turns")
        assigned.add((key[2], index))
        rows.append((index, *fields))

    throttle = _EVENT_FIELDS.index("throttle_reapplication_distance")
    complete = [row for row in rows if row[throttle] is not None]
    expected = "missing" if not rows else "incomplete" if not complete else "complete" if len(complete) == 1 else "ambiguous"
    _expect(sample.get("event_status"), expected, "event state")
    selected = sample.get("selected_event_index")
    if rows:
        if integer_value(selected, "selected event index") not in {row[0] for row in rows}:
            raise BundleError("selected consistency event is not a candidate")
    else:
        _expect(selected, None, "selected event")
    summary_index = sample.get("summary_event_index")
    _expect(
        integer_value(summary_index, "summary event index") if summary_index is not None else None,
        complete[0][0] if len(complete) == 1 else None, "summary event",
    )

    connection.execute("INSERT INTO consistency_corner_laps VALUES (?, ?, ?, ?, ?, ?, ?)", (
        *key, _number(sample.get("duration_s"), "corner duration"), expected,
        sample.get("selected_event_index"), sample.get("summary_event_index"),
    ))
    connection.executemany("INSERT INTO consistency_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        (*key, *row) for row in rows
    ))


def index_consistency(connection: sqlite3.Connection, bundle_id: str, document: dict, analysis: dict) -> None:
    """Index a verified document, requiring correspondence with its owning bundle."""
    eligible = sorted(
        (lap for lap in analysis["lap_eligibility"] if lap["eligible"]),
        key=lambda lap: lap["lap_number"],
    )
    available = len(eligible) >= MINIMUM_LAPS
    track = (analysis.get("corner_catalog") or {}).get("selected_track")
    catalog_turns = array_value(track.get("turns"), "analysis turns") if track and available else []
    corner_available = bool(track) and available
    reason = None if available else _NOT_ENOUGH
    corner_reason = None if corner_available else ("no_catalog_track" if available else _NOT_ENOUGH)
    _expect(document.get("status"), "available" if available else "unavailable", "status")
    _expect(document.get("reason"), reason, "reason")
    _expect(document.get("corner_status"), "available" if corner_available else "unavailable", "corner status")
    _expect(document.get("corner_reason"), corner_reason, "corner reason")

    samples = array_value(document.get("laps"), "consistency laps")
    _expect(len(samples), len(eligible), "lap count")
    timings = []
    for ordinal, (raw, lap) in enumerate(zip(samples, eligible), 1):
        sample = object_value(raw, "consistency lap sample")
        number = integer_value(sample.get("lap_number"), "lap number", 1, 100)
        time = _number(sample.get("official_lap_time_s"), "official lap time")
        sectors = tuple(_number(value, "sector time") for value in array_value(sample.get("sector_times_s"), "sector times"))
        _expect(integer_value(sample.get("eligible_attempt_ordinal"), "ordinal", 1, 100), ordinal, "ordinal")
        _expect(number, lap["lap_number"], "lap number")
        _expect(time, lap["official_lap_time_ms"] / 1000, "official lap time")
        original_sectors = array_value(lap.get("sector_times_ms"), "analysis sector times")
        _expect(len(original_sectors), 3, "analysis sector count")
        _expect(sectors, tuple(integer_value(ms, "analysis sector time") / 1000 for ms in original_sectors), "sector times")
        timings.append((number, ordinal, time, *sectors))

    slope = intercept = None
    trend = document.get("trend")
    if available:
        trend = object_value(trend, "consistency trend")
        inputs = []
        for pair in array_value(trend.get("inputs"), "trend inputs"):
            pair = array_value(pair, "trend input")
            _expect(len(pair), 2, "trend input size")
            inputs.append([integer_value(pair[0], "trend ordinal", 1, 100), _number(pair[1], "trend lap time")])
        _expect(inputs, [[ordinal, time] for _, ordinal, time, *_ in timings], "trend inputs")
        slope = _number(trend.get("slope_s_per_attempt"), "trend slope", signed=True)
        intercept = _number(trend.get("intercept_s"), "trend intercept", signed=True)
    else:
        _expect(trend, None, "unavailable trend")
    connection.execute("INSERT INTO consistency_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        bundle_id, document["schema_version"], document["method_version"], document["status"], reason,
        document["corner_status"], corner_reason, slope, intercept,
    ))
    connection.executemany("INSERT INTO consistency_laps VALUES (?, ?, ?, ?, ?, ?, ?)", (
        (bundle_id, *timing) for timing in timings
    ))
    sectors = array_value(document.get("sector_times_s"), "sector distributions")
    _expect(len(sectors), 3 if available else 0, "sector distribution count")
    if available:
        connection.executemany("INSERT INTO consistency_timing_summaries VALUES (?, ?, ?, ?, ?, ?)", (
            (bundle_id, ordinal, *_distribution(summary))
            for ordinal, summary in enumerate([document.get("lap_time_s"), *sectors])
        ))
    else:
        _expect(document.get("lap_time_s"), None, "unavailable lap distribution")

    corners = array_value(document.get("corners"), "consistency corners")
    _expect(len(corners), len(catalog_turns), "corner count")
    assigned: set[tuple[int, int]] = set()
    for raw, turn in zip(corners, catalog_turns):
        corner = object_value(raw, "consistency corner")
        turn_id = turn["turn_id"]
        _expect(corner.get("turn_id"), turn_id, "turn ID")
        _expect(integer_value(corner.get("turn_number"), "turn number", 1), turn["number"], "turn number")
        name = turn.get("display_name")
        if not isinstance(name, str) or not name:
            raise BundleError("invalid turn display name for consistency")
        _expect(corner.get("display_name"), name, "turn name")
        raw_laps = array_value(corner.get("samples"), "corner lap samples")
        _expect(len(raw_laps), len(eligible), "corner sample count")
        corner_laps = [object_value(item, "corner lap sample") for item in raw_laps]
        complete = all(sample.get("event_status") == "complete" for sample in corner_laps)
        state, why = ("available", None) if complete else ("unavailable", _EVENT_REASON)
        _expect(corner.get("event_status"), state, "corner event status")
        _expect(corner.get("event_reason"), why, "corner event reason")
        connection.execute("INSERT INTO consistency_corners VALUES (?, ?, ?, ?)", (bundle_id, turn_id, state, why))
        for sample, lap in zip(corner_laps, eligible):
            number = integer_value(sample.get("lap_number"), "corner lap", 1, 100)
            _expect(number, lap["lap_number"], "corner lap number")
            _index_events(connection, (bundle_id, turn_id, number), sample, assigned)
        summaries = [object_value(item, "corner event summary")
                     for item in array_value(corner.get("event_summaries"), "corner event summaries")]
        _expect(tuple(item.get("metric") for item in summaries), _EVENT_METRICS if complete else (), "event metrics")
        connection.executemany("INSERT INTO consistency_corner_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
            (bundle_id, turn_id, ordinal, metric, *_distribution(distribution))
            for ordinal, (metric, distribution) in enumerate(
                [("duration_s", corner.get("duration_s")), *((item["metric"], item.get("distribution")) for item in summaries)]
            )
        ))
    if read_consistency(connection, bundle_id) != document:
        raise BundleError("consistency document has unexpected or missing fields")


def read_consistency(connection: sqlite3.Connection, bundle_id: str) -> dict | None:
    """Reconstruct the indexed result with the artifact's stable ordering."""
    parameters = (bundle_id,)
    run = connection.execute(
        "SELECT schema_version, method_version, status, reason, corner_status, corner_reason, "
        "slope_s_per_attempt, intercept_s FROM consistency_runs WHERE bundle_id=?",
        parameters,
    ).fetchone()
    if run is None:
        return None
    schema, method, status, reason, corner_status, corner_reason, slope, intercept = run
    laps = [
        {"lap_number": number, "eligible_attempt_ordinal": ordinal, "official_lap_time_s": time, "sector_times_s": sectors}
        for number, ordinal, time, *sectors in connection.execute(
            "SELECT lap_number, ordinal, official_time_s, sector1_s, sector2_s, sector3_s "
            "FROM consistency_laps WHERE bundle_id=? ORDER BY ordinal", parameters,
        )
    ]
    trend = None if slope is None else {
        "inputs": [[lap["eligible_attempt_ordinal"], lap["official_lap_time_s"]] for lap in laps],
        "slope_s_per_attempt": slope, "intercept_s": intercept,
    }
    timing = []
    for ordinal, *row in connection.execute(
        "SELECT ordinal, minimum, maximum, median, mad FROM consistency_timing_summaries "
        "WHERE bundle_id=? ORDER BY ordinal", parameters,
    ):
        if ordinal != len(timing):
            raise BundleError("invalid indexed consistency timing ordinal")
        timing.append(dict(zip(_DISTRIBUTION, row)))
    events: dict[tuple[str, int], list[dict]] = {}
    event_columns = ", ".join(_EVENT_FIELDS)
    for turn_id, number, *fields in connection.execute(
        f"SELECT turn_id, lap_number, {event_columns} FROM consistency_events "
        "WHERE bundle_id=? ORDER BY turn_id, lap_number, event_index", parameters,
    ):
        events.setdefault((turn_id, number), []).append(dict(zip(_EVENT_FIELDS, fields)))
    samples: dict[str, list[dict]] = {}
    for turn_id, number, duration, state, selected, complete in connection.execute(
        "SELECT turn_id, lap_number, duration_s, event_status, selected_event_index, summary_event_index "
        "FROM consistency_corner_laps WHERE bundle_id=? ORDER BY turn_id, lap_number", parameters,
    ):
        samples.setdefault(turn_id, []).append({
            "lap_number": number, "duration_s": duration, "event_status": state,
            "selected_event_index": selected, "summary_event_index": complete,
            "events": events.get((turn_id, number), []),
        })
    summaries: dict[str, list[dict]] = {}
    for turn_id, ordinal, metric, *summary in connection.execute(
        "SELECT turn_id, ordinal, metric, minimum, maximum, median, mad FROM consistency_corner_summaries "
        "WHERE bundle_id=? ORDER BY turn_id, ordinal", parameters,
    ):
        bucket = summaries.setdefault(turn_id, [])
        if ordinal != len(bucket) or (ordinal == 0 and metric != "duration_s"):
            raise BundleError("invalid indexed consistency corner metric")
        bucket.append({"metric": metric, "distribution": dict(zip(_DISTRIBUTION, summary))})
    corners = []
    for turn_id, number, turn, state, why in connection.execute(
        "SELECT cc.turn_id, c.turn_number, c.turn_json, cc.event_status, cc.event_reason "
        "FROM consistency_corners AS cc JOIN corners AS c USING (bundle_id, turn_id) "
        "WHERE cc.bundle_id=? ORDER BY c.turn_number, cc.turn_id", parameters,
    ):
        distributions = summaries.get(turn_id, [])
        corners.append({
            "turn_id": turn_id, "turn_number": number, "display_name": json.loads(turn)["display_name"],
            "samples": samples.get(turn_id, []), "event_status": state, "event_reason": why,
            "duration_s": distributions[0]["distribution"] if distributions else None,
            "event_summaries": distributions[1:],
        })
    return {
        "schema_version": schema, "method_version": method, "status": status, "reason": reason,
        "corner_status": corner_status, "corner_reason": corner_reason, "laps": laps, "trend": trend,
        "lap_time_s": timing[0] if timing else None, "sector_times_s": timing[1:], "corners": corners,
    }
