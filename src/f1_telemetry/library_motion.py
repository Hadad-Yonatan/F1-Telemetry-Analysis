"""Index verified Motion run metadata and paired summary records."""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import fields

from f1_telemetry.bundle import BundleError, array_value, integer_value, object_value
from f1_telemetry.composite_motion import CompositeCoverage, MotionStreamCoverage
from f1_telemetry.motion_analysis import MOTION_METRICS


MOTION_LIBRARY_SCHEMA = """
CREATE TABLE motion_runs (
    bundle_id TEXT PRIMARY KEY REFERENCES sessions(bundle_id) ON DELETE CASCADE,
    schema_version INTEGER NOT NULL, method_version TEXT NOT NULL, reference_lap INTEGER NOT NULL,
    status TEXT NOT NULL, reason TEXT, transform_json TEXT, coverage_json TEXT NOT NULL,
    FOREIGN KEY (bundle_id, reference_lap) REFERENCES laps(bundle_id, lap_number)
);
CREATE TABLE motion_metrics (
    bundle_id TEXT NOT NULL REFERENCES motion_runs(bundle_id) ON DELETE CASCADE,
    lap_number INTEGER NOT NULL, ordinal INTEGER NOT NULL, metric TEXT NOT NULL,
    sample_count INTEGER NOT NULL, reference_json TEXT, comparison_json TEXT, difference_json TEXT,
    PRIMARY KEY (bundle_id, lap_number, ordinal), UNIQUE (bundle_id, lap_number, metric),
    FOREIGN KEY (bundle_id, lap_number) REFERENCES laps(bundle_id, lap_number)
);
"""


def _canonical(value: object) -> str | None:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) if value is not None else None


def _numeric(value: object) -> bool:
    return type(value) in (int, float) and abs(value) <= 1e100 and math.isfinite(value)


def _valid_distribution(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"minimum", "maximum", "median", "mad"}:
        return False
    if not all(_numeric(item) for item in value.values()):
        return False
    return value["minimum"] <= value["median"] <= value["maximum"] and value["mad"] >= 0


def _coverage(value: object) -> dict:
    coverage = object_value(value, "motion coverage")
    if set(coverage) != {field.name for field in fields(CompositeCoverage)}:
        raise BundleError("invalid motion coverage fields")
    core = integer_value(coverage.get("core_samples"), "core sample count")
    integer_value(coverage.get("nonexact_core_samples"), "nonexact core sample count", 0, core)
    integer_value(coverage.get("duplicate_core_identities"), "duplicate core identities", 0, core // 2)
    both = integer_value(coverage.get("joined_both"), "composite sample count", 0, core)
    for field in ("motion", "motion_ex"):
        stream = object_value(coverage[field], field)
        if set(stream) != {item.name for item in fields(MotionStreamCoverage)}:
            raise BundleError(f"invalid coverage fields for {field}")
        for name, count in stream.items():
            integer_value(count, name)
        if (
            stream["joined_samples"] + stream["missing_samples"] != core
            or stream["joined_samples"] < both
            or stream["joined_samples"] + stream["decode_errors"] > stream["packets"]
        ):
            raise BundleError(f"inconsistent coverage counts for {field}")
    return coverage


def motion_projection(motion: dict, analysis: dict) -> dict:
    """Check layout, axes, and counts; return the part of the artifact indexed by SQLite."""
    reference = integer_value(motion.get("reference_lap"), "motion reference", 1, 100)
    eligible = sorted(lap["lap_number"] for lap in analysis["lap_eligibility"] if lap["eligible"])
    if reference != analysis["selected_reference_lap"] or reference not in eligible:
        raise BundleError("motion reference does not match analysis")
    coverage = _coverage(motion.get("coverage"))
    transform = motion.get("transform")
    if transform is not None:
        transform = object_value(transform, "motion transform")
        for key in ("timing_line_world_xz_m", "forward_world_xz"):
            point = array_value(transform.get(key), key)
            if len(point) != 2 or not all(map(_numeric, point)):
                raise BundleError(f"invalid motion transform vector: {key}")
    distances = array_value(motion.get("distance_m"), "motion distances")
    session = object_value(analysis.get("session"), "session")
    if (
        not distances or distances[0] != 0 or distances[-1] != session.get("track_length_m")
        or not all(map(_numeric, distances))
        or any(b <= a for a, b in zip(distances, distances[1:]))
    ):
        raise BundleError("invalid motion distance grid")

    laps = array_value(motion.get("laps"), "motion laps")
    if len(laps) != len(eligible):
        raise BundleError("motion must contain every eligible lap")
    values_by_lap = {}
    for value, number in zip(laps, eligible):
        lap = object_value(value, "motion lap")
        if integer_value(lap.get("lap_number"), "motion lap number", 1, 100) != number:
            raise BundleError("motion lap does not match analysis order")
        points = array_value(lap.get("positions_m"), "motion positions")
        if len(points) != len(distances) or any(
            point is not None and (
                transform is None or not isinstance(point, list) or len(point) != 2 or not all(map(_numeric, point))
            )
            for point in points
        ):
            raise BundleError("invalid motion positions")
        breaks = array_value(lap.get("break_before"), "motion path breaks")
        if len(breaks) != len(distances) or breaks[0] is not True or not all(type(value) is bool for value in breaks):
            raise BundleError("invalid motion path breaks")
        metrics = array_value(lap.get("metrics"), "motion lap series")
        if len(metrics) != len(MOTION_METRICS):
            raise BundleError("invalid motion lap series count")
        values_by_lap[number] = {}
        for series, name in zip(metrics, MOTION_METRICS):
            series = object_value(series, "motion series")
            values = array_value(series.get("values"), "motion series values")
            if series.get("metric") != name or len(values) != len(distances) or any(
                value is not None and not _numeric(value) for value in values
            ):
                raise BundleError(f"invalid motion series: {name}")
            values_by_lap[number][name] = values
    policy = object_value(analysis["session_policy"], "session policy") if "session_policy" in analysis else {}
    if policy.get("status") == "experimental":
        comparison_laps = sorted(comparison["comparison_lap"] for comparison in analysis["comparisons"])
    else:
        comparison_laps = [lap for lap in eligible if lap != reference]
    comparisons = array_value(motion.get("comparisons"), "motion comparisons")
    if len(comparison_laps) != len(comparisons):
        raise BundleError("motion comparison count does not match analysis")
    shared = False
    for value, number in zip(comparisons, comparison_laps):
        comparison = object_value(value, "motion comparison")
        if integer_value(comparison.get("lap_number"), "motion comparison lap", 1, 100) != number:
            raise BundleError("motion comparison does not match analysis order")
        metrics = array_value(comparison.get("shared_metrics"), "shared motion metrics")
        if len(metrics) != len(MOTION_METRICS):
            raise BundleError("invalid shared motion metric count")
        for metric, name in zip(metrics, MOTION_METRICS):
            metric = object_value(metric, "shared motion metric")
            if set(metric) != {"metric", "count", "reference", "comparison", "difference"} or metric["metric"] != name:
                raise BundleError("unexpected shared motion metric")
            count = integer_value(metric.get("count"), "shared motion sample count", 0, len(distances))
            expected = sum(
                a is not None and b is not None
                for a, b in zip(values_by_lap[reference][name], values_by_lap[number][name])
            )
            if count != expected or any(
                not _valid_distribution(metric.get(key)) if count else metric.get(key) is not None
                for key in ("reference", "comparison", "difference")
            ):
                raise BundleError(f"invalid shared motion summary: {name}")
            shared |= count > 0
    expected_status = "available" if shared else "unavailable"
    expected_reason = None if shared else "no_shared_valid_motion_samples"
    if motion.get("status") != expected_status or motion.get("reason") != expected_reason:
        raise BundleError("motion availability does not match shared sample counts")
    return {
        "schema_version": motion["schema_version"], "method_version": motion["method_version"],
        "reference_lap": reference, "status": expected_status, "reason": expected_reason,
        "transform": transform, "coverage": coverage, "comparisons": comparisons,
    }


def index_motion(connection: sqlite3.Connection, bundle_id: str, motion: dict, analysis: dict) -> None:
    result = motion_projection(motion, analysis)
    connection.execute("INSERT INTO motion_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
        bundle_id, result["schema_version"], result["method_version"], result["reference_lap"],
        result["status"], result["reason"], _canonical(result["transform"]), _canonical(result["coverage"]),
    ))
    for comparison in result["comparisons"]:
        connection.executemany("INSERT INTO motion_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
            (bundle_id, comparison["lap_number"], ordinal, item["metric"], item["count"],
             _canonical(item["reference"]), _canonical(item["comparison"]), _canonical(item["difference"]))
            for ordinal, item in enumerate(comparison["shared_metrics"])
        ))
    if read_motion(connection, bundle_id) != result:
        raise BundleError("motion summary has unexpected or missing fields")


def read_motion(connection: sqlite3.Connection, bundle_id: str) -> dict | None:
    row = connection.execute(
        "SELECT schema_version, method_version, reference_lap, status, reason, transform_json, coverage_json "
        "FROM motion_runs WHERE bundle_id=?", (bundle_id,),
    ).fetchone()
    if row is None:
        return None
    schema, method, reference, status, reason, transform, coverage = row
    comparisons: dict[int, list[dict]] = {}
    for lap, ordinal, metric, count, *summary in connection.execute(
        "SELECT lap_number, ordinal, metric, sample_count, reference_json, comparison_json, difference_json "
        "FROM motion_metrics WHERE bundle_id=? ORDER BY lap_number, ordinal", (bundle_id,),
    ):
        rows = comparisons.setdefault(lap, [])
        if ordinal != len(rows):
            raise BundleError("invalid indexed motion metric ordinal")
        rows.append({
            "metric": metric, "count": count,
            **{key: json.loads(value) if value is not None else None
               for key, value in zip(("reference", "comparison", "difference"), summary)},
        })
    return {
        "schema_version": schema, "method_version": method, "reference_lap": reference,
        "status": status, "reason": reason, "transform": json.loads(transform) if transform is not None else None,
        "coverage": json.loads(coverage),
        "comparisons": [{"lap_number": lap, "shared_metrics": metrics} for lap, metrics in comparisons.items()],
    }
