"""Index and verify experiment records without storing artifact bytes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from statistics import median

from f1_telemetry.bundle import (
    BundleError, _files, _load_object, _read_artifact, _root, array_value, digest_value, integer_value, object_value,
)
from f1_telemetry.experiment_model import (
    ASSIST_CODES, ASSIST_FLAGS, CONDITION_LIMITS, EXPERIMENT_METHOD_VERSION, EXPERIMENT_SCHEMA_VERSION,
    MIN_GROUP_LAPS, _bool, _exact, _finite, parse_manifest,
    require_validated_time_trial,
)


EXPERIMENT_FILENAME = "experiment.json"
EXPERIMENT_REPORT_FILENAME = "report.html"
EXPERIMENT_LIBRARY_SCHEMA = """
CREATE TABLE experiments (
    experiment_id TEXT PRIMARY KEY, root TEXT NOT NULL UNIQUE, report_sha256 TEXT NOT NULL,
    schema_version INTEGER NOT NULL, method_version TEXT NOT NULL, track_id INTEGER NOT NULL,
    track_length_m INTEGER NOT NULL, control_bundle_id TEXT NOT NULL REFERENCES bundles(bundle_id),
    candidate_bundle_id TEXT NOT NULL REFERENCES bundles(bundle_id), control_label TEXT NOT NULL,
    candidate_label TEXT NOT NULL, manifest_json TEXT NOT NULL, conditions_json TEXT NOT NULL,
    changes_json TEXT NOT NULL, line_reference_json TEXT NOT NULL
);
CREATE TABLE experiment_metrics (
    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL, metric TEXT NOT NULL, unit TEXT NOT NULL,
    control_count INTEGER NOT NULL, control_median REAL, control_mad REAL,
    candidate_count INTEGER NOT NULL, candidate_median REAL, candidate_mad REAL,
    difference REAL, reason TEXT, PRIMARY KEY (experiment_id, ordinal), UNIQUE (experiment_id, metric)
);
"""

_RESULT_FIELDS = {
    "schema_version", "method_version", "manifest", "track", "session_policy", "conditions", "control", "candidate",
    "setup_changes", "metrics", "catalog_corner_reason", "line_reason", "line_reference", "interpretation",
}
_CONDITION_INTEGERS = {
    "weather": (0, 5), "formula": (0, 255), "team_id": (0, 254), "tech_level": (0, 255),
    "actual_tyre_compound": (1, 255), "visual_tyre_compound": (1, 255), "traction_control": (0, 2),
    "fuel_mix": (0, 3),
}
_CONDITION_FLAGS = ("equal_car_performance", "my_team", "anti_lock_brakes")


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _optional_text(value: object, name: str) -> None:
    if value is not None and (type(value) is not str or not value):
        raise BundleError(f"invalid {name}")


def _verified_summary(value: object, laps: tuple[int, ...]) -> tuple[int, float | None]:
    group = _exact(value, {"samples", "count", "median", "mad"}, "metric group")
    samples = array_value(group["samples"], "metric samples")
    if len(samples) != len(laps):
        raise BundleError("experiment metric must include every selected lap")
    observed = []
    for expected, raw in zip(laps, samples):
        sample = _exact(raw, {"lap_number", "value", "support_count", "reason"}, "metric sample")
        if integer_value(sample["lap_number"], "metric lap", 1, 100) != expected:
            raise BundleError("experiment metric lap order does not match manifest")
        support = integer_value(sample["support_count"], "metric sample support")
        _optional_text(sample["reason"], "metric sample reason")
        if sample["value"] is None:
            if support or sample["reason"] is None:
                raise BundleError("unavailable experiment sample must have zero support and a reason")
        else:
            if not support or sample["reason"] is not None:
                raise BundleError("observed experiment sample must have support")
            observed.append(_finite(sample["value"], "metric value"))
    count = integer_value(group["count"], "observed metric lap count", 0, len(laps))
    if count != len(observed):
        raise BundleError("experiment metric count does not match samples")
    center = median(observed) if observed else None
    mad = median(abs(value - center) for value in observed) if observed else None
    for field, expected in (("median", center), ("mad", mad)):
        actual = group[field] if group[field] is None else _finite(group[field], f"metric {field}")
        if actual != expected:
            raise BundleError(f"experiment metric {field} does not match samples")
    return count, center


def _validate_experiment(result: dict):
    _exact(result, _RESULT_FIELDS, "experiment artifact")
    integer_value(result.get("schema_version"), "experiment schema", EXPERIMENT_SCHEMA_VERSION, EXPERIMENT_SCHEMA_VERSION)
    if result.get("method_version") != EXPERIMENT_METHOD_VERSION:
        raise BundleError("unsupported experiment method")
    manifest = parse_manifest(result["manifest"])
    track = _exact(result["track"], {"track_id", "track_length_m"}, "experiment track")
    integer_value(track["track_id"], "experiment track ID", -1, 255)
    integer_value(track["track_length_m"], "experiment track length", 1)
    conditions = _exact(
        result["conditions"], {*_CONDITION_INTEGERS, *_CONDITION_FLAGS, *CONDITION_LIMITS, "assists"},
        "experiment conditions",
    )
    for name, (low, high) in _CONDITION_INTEGERS.items():
        integer_value(conditions[name], name, low, high)
    for name in _CONDITION_FLAGS:
        _bool(conditions[name], name)
    assists = _exact(conditions["assists"], {*ASSIST_FLAGS, *ASSIST_CODES}, "experiment assists")
    for name in ASSIST_FLAGS:
        _bool(assists[name], name)
    for name in ASSIST_CODES:
        integer_value(assists[name], name, 0, 255)
    require_validated_time_trial(result["session_policy"])
    _optional_text(result["catalog_corner_reason"], "catalog availability")
    _optional_text(result["line_reason"], "line availability")
    if result["interpretation"] != "Numeric associations only. These comparisons do not prescribe setup changes.":
        raise BundleError("invalid experiment interpretation")
    measurements = {name: [] for name in CONDITION_LIMITS}
    for role, group in (("control", manifest.control), ("candidate", manifest.candidate)):
        data = _exact(result[role], {
            "bundle_id", "label", "capture_sha256", "session_uid", "player_car_index", "setup_fingerprint", "laps",
        }, f"{role} experiment result")
        if data.get("bundle_id") != group.bundle_id or data.get("label") != group.label:
            raise BundleError("experiment result groups do not match the manifest")
        digest_value(data["capture_sha256"], "experiment capture identity")
        digest_value(data["setup_fingerprint"], "experiment setup fingerprint")
        integer_value(data["player_car_index"], "experiment player index", 0, 21)
        uid = data["session_uid"]
        if not isinstance(uid, str) or not uid.isascii() or not uid.isdecimal() or len(uid) > 20 or str(int(uid)) != uid or int(uid) > (1 << 64) - 1:
            raise BundleError("invalid experiment session UID")
        raw_laps = array_value(data["laps"], "experiment group laps")
        if len(raw_laps) != len(group.laps):
            raise BundleError("experiment group laps do not match the manifest")
        for raw, expected in zip(raw_laps, group.laps):
            lap = _exact(raw, {
                "lap_number", "official_lap_time_s", "sectors_s", "start_fuel_kg", "front_brake_bias",
                "air_temperatures_c", "track_temperatures_c",
            }, "experiment group lap")
            if integer_value(lap["lap_number"], "experiment lap", 1, 100) != expected:
                raise BundleError("experiment group laps do not match the manifest")
            _finite(lap["official_lap_time_s"], "official lap time", minimum=0)
            integer_value(lap["front_brake_bias"], "running brake bias", 0, 100)
            measurements["lap_start_fuel_kg"].append(_finite(lap["start_fuel_kg"], "lap-start fuel", minimum=0))
            sectors = array_value(lap["sectors_s"], "sector times")
            if len(sectors) != 3:
                raise BundleError("experiment lap must include three sector times")
            for value in sectors:
                _finite(value, "sector time", minimum=0)
            for field in ("air_temperatures_c", "track_temperatures_c"):
                values = array_value(lap[field], field)
                if not values:
                    raise BundleError("experiment lap must include observed temperatures")
                for value in values:
                    measurements[field.replace("temperatures", "temperature")].append(
                        integer_value(value, field, -128, 127),
                    )
    for name, values in measurements.items():
        observed = _exact(conditions[name], {"minimum", "maximum", "allowed_spread"}, name)
        low, high, limit = (_finite(observed[key], key) for key in ("minimum", "maximum", "allowed_spread"))
        if (low, high, limit) != (min(values), max(values), CONDITION_LIMITS[name]) or high - low > limit:
            raise BundleError(f"experiment {name} bounds do not match observations")
    anchor = _exact(result["line_reference"], {"bundle_id", "lap_number"}, "experiment line reference")
    if anchor["bundle_id"] != manifest.control.bundle_id or integer_value(anchor["lap_number"], "line reference lap") != manifest.control.laps[0]:
        raise BundleError("experiment line reference is not the first control lap")
    changes = []
    for row in array_value(result["setup_changes"], "setup differences"):
        change = _exact(row, {"field", "control", "candidate"}, "setup difference")
        first, second = (_finite(change[role], "setup value") for role in ("control", "candidate"))
        if first.hex() == second.hex():
            raise BundleError("declared experiment setup field has no change")
        changes.append(change["field"])
    if tuple(changes) != manifest.changed_setup_fields:
        raise BundleError("experiment setup differences do not match the manifest")
    names = set()
    for raw in array_value(result["metrics"], "experiment metrics"):
        metric = _exact(raw, {"metric", "unit", "control", "candidate", "candidate_minus_control", "reason"}, "experiment metric")
        name = metric["metric"]
        if type(name) is not str or not name or name in names or type(metric["unit"]) is not str or not metric["unit"]:
            raise BundleError("experiment metrics must have unique names and known units")
        names.add(name)
        first, center1 = _verified_summary(metric["control"], manifest.control.laps)
        second, center2 = _verified_summary(metric["candidate"], manifest.candidate.laps)
        expected = center2 - center1 if min(first, second) >= MIN_GROUP_LAPS else None
        actual = metric["candidate_minus_control"]
        if actual is not None:
            actual = _finite(actual, "experiment metric difference")
        expected_reason = None if expected is not None else "requires_three_observed_laps_in_each_group"
        if actual != expected or metric["reason"] != expected_reason:
            raise BundleError("experiment metric difference does not match summaries")
    if not {"pace.lap_time", "pace.sector_1", "pace.sector_2", "pace.sector_3"} <= names:
        raise BundleError("experiment pace metrics are missing")
    return manifest


def read_experiment_artifact(root: str | Path, experiment_id: str, report_sha: str) -> dict:
    original = Path(root).absolute()
    root = _root(original)
    if root != original:
        raise BundleError("experiment root path has changed or contains a symlink")
    files = _files(root)
    if set(files) != {EXPERIMENT_FILENAME, EXPERIMENT_REPORT_FILENAME}:
        raise BundleError("experiment directory must contain only experiment.json and report.html")
    raw = _read_artifact(files[EXPERIMENT_FILENAME])
    report = _read_artifact(files[EXPERIMENT_REPORT_FILENAME])
    if hashlib.sha256(raw).hexdigest() != experiment_id or hashlib.sha256(report).hexdigest() != report_sha:
        raise BundleError("indexed experiment artifact SHA-256 changed")
    result = _load_object(raw, EXPERIMENT_FILENAME)
    _validate_experiment(result)
    return result


def index_experiment(
    connection: sqlite3.Connection, root: Path, experiment_id: str, report_sha: str, document: dict,
) -> bool:
    existing = connection.execute(
        "SELECT root, report_sha256 FROM experiments WHERE experiment_id=?", (experiment_id,),
    ).fetchone()
    if existing is not None:
        if existing[1] != report_sha:
            raise BundleError("different report claims an already indexed experiment")
        read_experiment_artifact(existing[0], experiment_id, report_sha)
        return False
    manifest = _validate_experiment(document)
    track = document["track"]
    for group in (manifest.control, manifest.candidate):
        row = connection.execute(
            "SELECT track_id, track_length_m FROM sessions WHERE bundle_id=?", (group.bundle_id,),
        ).fetchone()
        if row != (track["track_id"], track["track_length_m"]):
            raise BundleError("experiment source bundle missing or indexed under a different track")
    connection.execute("INSERT INTO experiments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        experiment_id, str(root), report_sha, document["schema_version"], document["method_version"],
        track["track_id"], track["track_length_m"], manifest.control.bundle_id, manifest.candidate.bundle_id,
        manifest.control.label, manifest.candidate.label, _json(document["manifest"]), _json(document["conditions"]),
        _json(document["setup_changes"]), _json(document["line_reference"]),
    ))
    for ordinal, metric in enumerate(document["metrics"]):
        control, candidate = metric["control"], metric["candidate"]
        connection.execute("INSERT INTO experiment_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            experiment_id, ordinal, metric["metric"], metric["unit"], control["count"], control["median"], control["mad"],
            candidate["count"], candidate["median"], candidate["mad"], metric["candidate_minus_control"], metric["reason"],
        ))
    return True


def experiment_references(connection: sqlite3.Connection) -> tuple[tuple[str, str, str], ...]:
    return tuple(connection.execute("SELECT experiment_id, root, report_sha256 FROM experiments ORDER BY experiment_id"))


def experiment_index_summary(connection: sqlite3.Connection, experiment_id: str) -> dict | None:
    digest_value(experiment_id, "experiment ID")
    row = connection.execute(
        "SELECT schema_version, method_version, track_id, track_length_m, manifest_json, conditions_json, changes_json, "
        "line_reference_json FROM experiments WHERE experiment_id=?", (experiment_id,),
    ).fetchone()
    if row is None:
        return None
    schema, method, track_id, length, manifest, conditions, changes, reference = row
    metrics = []
    for ordinal, metric, unit, count1, median1, mad1, count2, median2, mad2, difference, reason in connection.execute(
        "SELECT ordinal, metric, unit, control_count, control_median, control_mad, candidate_count, candidate_median, "
        "candidate_mad, difference, reason FROM experiment_metrics WHERE experiment_id=? ORDER BY ordinal",
        (experiment_id,),
    ):
        if ordinal != len(metrics):
            raise BundleError("invalid indexed experiment metric ordinal")
        metrics.append({
            "metric": metric, "unit": unit, "control": {"count": count1, "median": median1, "mad": mad1},
            "candidate": {"count": count2, "median": median2, "mad": mad2},
            "candidate_minus_control": difference, "reason": reason,
        })
    return {
        "experiment_id": experiment_id, "schema_version": schema, "method_version": method,
        "track": {"track_id": track_id, "track_length_m": length}, "manifest": json.loads(manifest),
        "conditions": json.loads(conditions), "setup_changes": json.loads(changes),
        "line_reference": json.loads(reference), "metrics": metrics,
    }


def verify_indexed_experiments(connection: sqlite3.Connection) -> None:
    for experiment_id, root, report_sha in experiment_references(connection):
        artifact = read_experiment_artifact(root, experiment_id, report_sha)
        expected = {key: artifact[key] for key in (
            "schema_version", "method_version", "track", "manifest", "conditions", "setup_changes", "line_reference",
        )}
        expected["experiment_id"] = experiment_id
        expected["metrics"] = [
            {**metric, **{role: {key: value for key, value in metric[role].items() if key != "samples"}
                          for role in ("control", "candidate")}}
            for metric in artifact["metrics"]
        ]
        if experiment_index_summary(connection, experiment_id) != expected:
            raise BundleError(f"indexed experiment changed: {experiment_id}")
        row = connection.execute(
            "SELECT control_bundle_id, candidate_bundle_id, control_label, candidate_label FROM experiments "
            "WHERE experiment_id=?", (experiment_id,),
        ).fetchone()
        if row != (
            artifact["control"]["bundle_id"], artifact["candidate"]["bundle_id"],
            artifact["control"]["label"], artifact["candidate"]["label"],
        ):
            raise BundleError(f"indexed experiment sources changed: {experiment_id}")
