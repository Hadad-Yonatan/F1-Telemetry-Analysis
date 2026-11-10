"""Strict setup experiment manifests and eligibility evidence."""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from struct import pack, unpack

from f1_telemetry.bundle import BundleError, VerifiedBundle, _load_object, array_value, digest_value, integer_value, object_value
from f1_telemetry.context_decode import CarSetup
from f1_telemetry.session_context import setup_fingerprint


EXPERIMENT_SCHEMA_VERSION = 1
EXPERIMENT_METHOD_VERSION = "1.0.0"
MIN_GROUP_LAPS = 3
SETUP_FIELDS = {field.name: field.type for field in fields(CarSetup)}
CONDITION_LIMITS = {"air_temperature_c": 2, "track_temperature_c": 3, "lap_start_fuel_kg": 0.25}
ASSIST_FLAGS = ("steering", "pit", "pit_release", "ers", "drs")
ASSIST_CODES = ("braking", "gearbox", "dynamic_racing_line", "dynamic_racing_line_type")
_VALIDATED_POLICY = {
    "schema_version": 1, "method_version": "1.0.0", "status": "validated", "name": "time-trial", "session_type": 18,
}


class ExperimentError(ValueError):
    """Input evidence cannot support the declared controlled experiment."""


@dataclass(frozen=True, slots=True)
class ExperimentGroup:
    bundle_id: str
    label: str
    laps: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ExperimentManifest:
    control: ExperimentGroup
    candidate: ExperimentGroup
    changed_setup_fields: tuple[str, ...]
    schema_version: int = EXPERIMENT_SCHEMA_VERSION

    def document(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "control": {"bundle_id": self.control.bundle_id, "label": self.control.label, "laps": list(self.control.laps)},
            "candidate": {"bundle_id": self.candidate.bundle_id, "label": self.candidate.label, "laps": list(self.candidate.laps)},
            "changed_setup_fields": list(self.changed_setup_fields),
        }


def _exact(value: object, keys: set[str], name: str) -> dict:
    document = object_value(value, name)
    if document.keys() != keys:
        raise ExperimentError(f"{name} must contain exactly: {', '.join(sorted(keys))}")
    return document


def _group(value: object, name: str) -> ExperimentGroup:
    group = _exact(value, {"bundle_id", "label", "laps"}, name)
    bundle_id = digest_value(group["bundle_id"], f"{name} bundle ID")
    label = group["label"]
    if not isinstance(label, str) or not label or label != label.strip() or len(label) > 80 or not label.isprintable():
        raise ExperimentError(f"{name} label must be 1–80 printable characters with no surrounding whitespace")
    laps = tuple(sorted(integer_value(item, f"{name} lap", 1, 100)
                        for item in array_value(group["laps"], f"{name} laps")))
    if len(laps) < MIN_GROUP_LAPS or len(laps) != len(set(laps)):
        raise ExperimentError(f"{name} requires at least three distinct eligible laps")
    return ExperimentGroup(bundle_id, label, laps)


def parse_manifest(value: object) -> ExperimentManifest:
    document = _exact(value, {"schema_version", "control", "candidate", "changed_setup_fields"}, "experiment manifest")
    integer_value(document["schema_version"], "experiment manifest schema", EXPERIMENT_SCHEMA_VERSION, EXPERIMENT_SCHEMA_VERSION)
    control, candidate = (_group(document[name], name) for name in ("control", "candidate"))
    changed = array_value(document["changed_setup_fields"], "changed setup fields")
    if not changed or any(type(field) is not str or field not in SETUP_FIELDS for field in changed):
        raise ExperimentError("declare at least one known changed setup field")
    if "fuel_load" in changed:
        raise ExperimentError("fuel_load cannot be a declared setup change")
    if len(set(changed)) != len(changed):
        raise ExperimentError("changed setup fields cannot contain duplicates")
    if control.label == candidate.label:
        raise ExperimentError("control and candidate labels must differ")
    if control.bundle_id == candidate.bundle_id and set(control.laps) & set(candidate.laps):
        raise ExperimentError("control and candidate laps overlap")
    return ExperimentManifest(control, candidate, tuple(sorted(changed)))


def load_manifest(path: str | Path) -> ExperimentManifest:
    return parse_manifest(_load_object(Path(path).read_bytes(), "experiment manifest"))


@dataclass(frozen=True, slots=True)
class LapEvidence:
    lap_number: int
    official_lap_time_s: float
    sectors_s: tuple[float, float, float]
    setup: CarSetup
    fingerprint: str
    conditions: dict
    air_temperatures_c: tuple[int, ...]
    track_temperatures_c: tuple[int, ...]
    start_fuel_kg: float


@dataclass(frozen=True, slots=True)
class GroupEvidence:
    request: ExperimentGroup
    bundle: VerifiedBundle
    capture_sha256: str
    session_uid: str
    player_car_index: int
    track: tuple[int, int]
    laps: tuple[LapEvidence, ...]

    def document(self) -> dict:
        return {
            "bundle_id": self.request.bundle_id, "label": self.request.label,
            "capture_sha256": self.capture_sha256, "session_uid": self.session_uid,
            "player_car_index": self.player_car_index,
            "setup_fingerprint": self.laps[0].fingerprint,
            "laps": [
                {"lap_number": lap.lap_number, "official_lap_time_s": lap.official_lap_time_s,
                 "sectors_s": list(lap.sectors_s), "start_fuel_kg": lap.start_fuel_kg,
                 "front_brake_bias": lap.setup.brake_bias,
                 "air_temperatures_c": list(lap.air_temperatures_c), "track_temperatures_c": list(lap.track_temperatures_c)}
                for lap in self.laps
            ],
        }


def _finite(value: object, name: str, *, minimum: float | None = None) -> float:
    if (
        type(value) not in (int, float) or abs(value) > 1e30 or not math.isfinite(value)
        or minimum is not None and value < minimum
    ):
        raise ExperimentError(f"{name} must be a known finite number")
    return float(value)


def _setup(value: dict) -> tuple[CarSetup, str]:
    values = _exact(value.get("values"), set(SETUP_FIELDS), "setup values")
    checked = {}
    for field, kind in SETUP_FIELDS.items():
        if kind in (int, "int"):
            checked[field] = integer_value(values[field], field, 0, 255)
        else:
            number = _finite(values[field], field)
            if unpack("<f", pack("<f", number))[0] != number:
                raise ExperimentError(f"{field} is not an exact binary32 setup value")
            checked[field] = number
    setup = CarSetup(**checked)
    fingerprint = digest_value(value.get("fingerprint"), "setup fingerprint")
    if setup_fingerprint(setup) != fingerprint:
        raise ExperimentError("setup values do not match their fingerprint")
    return setup, fingerprint


def _bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ExperimentError(f"{name} must be known")
    return value


def _session_conditions(value: dict) -> tuple[dict, int, int]:
    integer_value(value.get("session_type"), "recorded session type", 18, 18)
    rules = object_value(value.get("rules"), "lap session rules")
    if integer_value(rules.get("car_damage"), "car damage rules", 0, 3) != 0:
        raise ExperimentError("damage-free operation cannot be verified unless car damage was disabled")
    for key in ("safety_car_status", "num_safety_car_periods", "num_virtual_safety_car_periods", "num_red_flag_periods"):
        if integer_value(value.get(key), key, 0, 255) != 0:
            raise ExperimentError("caution periods are not allowed in a setup experiment")
    if _bool(value.get("game_paused"), "game pause state"):
        raise ExperimentError("paused laps are not allowed in a setup experiment")
    assists = object_value(value.get("assists"), "session assists")
    conditions = {
        "weather": integer_value(value.get("weather"), "weather", 0, 5),
        "formula": integer_value(value.get("formula"), "formula", 0, 255),
        "equal_car_performance": _bool(value.get("equal_car_performance"), "performance equality"),
        "assists": {
            **{key: _bool(assists.get(key), f"{key} assist") for key in ASSIST_FLAGS},
            **{key: integer_value(assists.get(key), f"{key} assist", 0, 255) for key in ASSIST_CODES},
        },
    }
    air = integer_value(value.get("air_temperature_c"), "air temperature", -128, 127)
    track = integer_value(value.get("track_temperature_c"), "track temperature", -128, 127)
    return conditions, air, track


def _participant_conditions(value: dict) -> dict:
    return {
        "team_id": integer_value(value.get("team_id"), "team", 0, 254),
        "tech_level": integer_value(value.get("tech_level"), "car performance level", 0, 255),
        "my_team": _bool(value.get("my_team"), "car identity"),
    }


def _car_conditions(value: dict, setup_brake_bias: int) -> tuple[dict, float]:
    if integer_value(value.get("vehicle_fia_flags"), "vehicle FIA flag", 0, 4) not in (0, 1):
        raise ExperimentError("caution periods are not allowed in a setup experiment")
    if _bool(value.get("pit_limiter"), "pit limiter") or _bool(value.get("network_paused"), "network pause"):
        raise ExperimentError("pit or interrupted laps are not allowed in a setup experiment")
    if integer_value(value.get("front_brake_bias"), "running brake bias", 0, 100) != setup_brake_bias:
        raise ExperimentError("running brake bias does not match the selected setup")
    return {
        "actual_tyre_compound": integer_value(value.get("actual_tyre_compound"), "actual tyre compound", 1, 255),
        "visual_tyre_compound": integer_value(value.get("visual_tyre_compound"), "visual tyre compound", 1, 255),
        "traction_control": integer_value(value.get("traction_control"), "traction control", 0, 2),
        "anti_lock_brakes": _bool(value.get("anti_lock_brakes"), "anti-lock brakes"),
        "fuel_mix": integer_value(value.get("fuel_mix"), "fuel mix", 0, 3),
    }, _finite(value.get("fuel_in_tank_kg"), "lap fuel", minimum=0)


class _ContextTimeline:
    def __init__(self, raw: list) -> None:
        self.rows = []
        self.frames = []
        self.kind_indices = defaultdict(list)
        self.kind_frames = defaultdict(list)
        last = None
        for index, value in enumerate(raw):
            row = object_value(value, "context snapshot")
            frame = integer_value(row.get("overall_frame_identifier"), "snapshot frame")
            kind = row.get("kind")
            if kind not in ("session", "participant", "setup", "car_status", "tyres", "time_trial"):
                raise ExperimentError("unexpected context snapshot kind")
            timestamp = row.get("captured_unix_ns")
            if (
                type(timestamp) is not str or not timestamp.isascii() or not timestamp.isdecimal()
                or len(timestamp) > 20 or int(timestamp) > (1 << 64) - 1
            ):
                raise ExperimentError("invalid context snapshot timestamp")
            key = frame, int(timestamp), kind
            if last is not None and key < last:
                raise ExperimentError("context timeline is not in frame order")
            last = key
            self.rows.append(row)
            self.frames.append(frame)
            self.kind_indices[kind].append(index)
            self.kind_frames[kind].append(frame)

    def latest(self, kind: str, frame: int) -> int | None:
        position = bisect_right(self.kind_frames[kind], frame) - 1
        return self.kind_indices[kind][position] if position >= 0 else None

    def changes(self, start: int, end: int) -> list[int]:
        return list(range(bisect_right(self.frames, start), bisect_right(self.frames, end)))


def _lap_evidence(number: int, validation: dict, row: dict, timeline: _ContextTimeline) -> LapEvidence:
    if row.get("unavailable_reason") is not None:
        raise ExperimentError("lap-start context is unavailable")
    indices = object_value(row.get("start_indices"), "lap-start snapshots")
    first_frame = integer_value(row.get("start_frame"), "first timed frame")
    end_frame = integer_value(row.get("end_frame"), "last timed frame", first_frame)

    def initial(kind: str) -> dict:
        position = timeline.latest(kind, first_frame)
        if position is None or integer_value(indices.get(kind), f"{kind} snapshot index") != position:
            raise ExperimentError(f"lap-start {kind} snapshot is missing or not the latest")
        return object_value(timeline.rows[position].get("value"), f"lap-start {kind}")

    setup, fingerprint = _setup(initial("setup"))
    session, air, track = _session_conditions(initial("session"))
    participant = _participant_conditions(initial("participant"))
    car, fuel = _car_conditions(initial("car_status"), setup.brake_bias)
    air_values, track_values = [air], [track]
    recorded = [
        integer_value(value, "in-lap change index") for value in array_value(row.get("change_indices"), "in-lap context changes")
    ]
    changes = timeline.changes(first_frame, end_frame)
    if recorded != changes:
        raise ExperimentError("recorded in-lap changes do not match the context timeline")
    for index in changes:
        item = timeline.rows[index]
        kind = item.get("kind")
        if kind not in ("session", "participant", "setup", "car_status"):
            continue
        value = object_value(item.get("value"), f"in-lap {kind}")
        if kind == "setup" and _setup(value)[1] != fingerprint:
            raise ExperimentError("setup changed during a selected lap")
        if kind == "participant" and _participant_conditions(value) != participant:
            raise ExperimentError("car or team changed during a selected lap")
        if kind == "car_status" and _car_conditions(value, setup.brake_bias)[0] != car:
            raise ExperimentError("tyre compound or car controls changed during a selected lap")
        if kind == "session":
            observed, air, track = _session_conditions(value)
            if observed != session:
                raise ExperimentError("session conditions changed during a selected lap")
            air_values.append(air)
            track_values.append(track)
    sectors = tuple(integer_value(value, "sector time", 1) / 1000
                    for value in array_value(validation.get("sector_times_ms"), "sector times"))
    if len(sectors) != 3:
        raise ExperimentError("selected lap must have three complete sectors")
    return LapEvidence(
        number, integer_value(validation.get("official_lap_time_ms"), "official lap time", 1) / 1000,
        sectors, setup, fingerprint, {**session, **participant, **car}, tuple(air_values), tuple(track_values), fuel,
    )


def require_validated_time_trial(value: object) -> None:
    if (
        not isinstance(value, dict) or value != _VALIDATED_POLICY
        or type(value.get("schema_version")) is not int or type(value.get("session_type")) is not int
    ):
        raise ExperimentError("setup experiments require the validated Time Trial policy")


def collect_group(group: ExperimentGroup, bundle: VerifiedBundle) -> GroupEvidence:
    if bundle.bundle_id != group.bundle_id:
        raise ExperimentError("verified bundle does not match experiment manifest")
    doc = bundle.document
    require_validated_time_trial(doc.get("session_policy"))
    capture = object_value(doc.get("capture"), "capture identity")
    session = object_value(doc.get("session"), "session")
    track = (integer_value(session.get("track_id"), "track ID", -1, 255),
             integer_value(session.get("track_length_m"), "track length", 1))
    context = object_value(doc.get("session_context"), "session context")
    integer_value(context.get("schema_version"), "session context schema", 1, 1)
    session_uid = str(session.get("session_uid"))
    if (
        not session_uid.isascii() or not session_uid.isdecimal() or len(session_uid) > 20
        or str(int(session_uid)) != session_uid or int(session_uid) > (1 << 64) - 1
        or context.get("session_uid") != session_uid
    ):
        raise ExperimentError("session context UID does not match the bundle")
    timeline = _ContextTimeline(array_value(context.get("timeline"), "context timeline"))
    player = integer_value(context.get("player_car_index"), "selected player index", 0, 21)
    diagnostics = object_value(context.get("diagnostics"), "context diagnostics")
    for kind in ("session", "participant", "setup", "car_status"):
        count = object_value(diagnostics.get(kind), f"{kind} diagnostics")
        if integer_value(count.get("conflicts"), f"{kind} conflicts"):
            raise ExperimentError(f"conflicting {kind} evidence cannot support an experiment")
        if integer_value(count.get("decode_errors"), f"{kind} decode errors"):
            raise ExperimentError(f"malformed {kind} evidence cannot support an experiment")
    records = {lap["lap_number"]: lap for lap in doc["lap_eligibility"]}
    contexts = {}
    for item in array_value(context.get("laps"), "lap contexts"):
        row = object_value(item, "lap context")
        number = integer_value(row.get("lap_number"), "context lap", 1, 100)
        if number in contexts:
            raise ExperimentError("duplicate lap context")
        contexts[number] = row
    laps = []
    for number in group.laps:
        row = records.get(number, {})
        if not all(row.get(key) is True for key in ("eligible", "officially_valid", "structurally_complete")) or number not in contexts:
            raise ExperimentError(f"{group.label} lap {number} is not eligible or lacks context")
        try:
            laps.append(_lap_evidence(number, row, contexts[number], timeline))
        except (BundleError, ExperimentError) as error:
            raise ExperimentError(f"{group.label} lap {number}: {error}") from error
    if len({lap.fingerprint for lap in laps}) != 1:
        raise ExperimentError(f"{group.label} must have one stable setup fingerprint")
    return GroupEvidence(
        group, bundle, digest_value(capture.get("sha256"), "capture SHA-256"),
        session_uid, player, track, tuple(laps),
    )


def check_groups(manifest: ExperimentManifest, control: GroupEvidence, candidate: GroupEvidence) -> tuple[dict, list[dict]]:
    if control.track != candidate.track:
        raise ExperimentError("control and candidate must use the same track and in-game length")
    if control.capture_sha256 == candidate.capture_sha256 and set(control.request.laps) & set(candidate.request.laps):
        raise ExperimentError("control and candidate select overlapping laps from the same capture")
    if (
        (control.session_uid, control.player_car_index) == (candidate.session_uid, candidate.player_car_index)
        and set(control.request.laps) & set(candidate.request.laps)
    ):
        raise ExperimentError("control and candidate select overlapping laps from the same player session")
    laps = (*control.laps, *candidate.laps)
    conditions = laps[0].conditions
    for lap in laps[1:]:
        differences = sorted(key for key, value in conditions.items() if lap.conditions.get(key) != value)
        if differences:
            raise ExperimentError(f"control conditions differ: {', '.join(differences)}")
    bounds = {}
    for name, values in (
        ("air_temperature_c", [value for lap in laps for value in lap.air_temperatures_c]),
        ("track_temperature_c", [value for lap in laps for value in lap.track_temperatures_c]),
        ("lap_start_fuel_kg", [lap.start_fuel_kg for lap in laps]),
    ):
        limit = CONDITION_LIMITS[name]
        low, high = min(values), max(values)
        if high - low > limit:
            raise ExperimentError(f"{name} spread exceeds the limit of {limit}")
        bounds[name] = {"minimum": low, "maximum": high, "allowed_spread": limit}
    first, second = asdict(control.laps[0].setup), asdict(candidate.laps[0].setup)
    changes = [
        {"field": field, "control": first[field], "candidate": second[field]}
        for field in sorted(SETUP_FIELDS)
        if (first[field].hex() if isinstance(first[field], float) else first[field])
        != (second[field].hex() if isinstance(second[field], float) else second[field])
    ]
    actual = {row["field"] for row in changes}
    declared = set(manifest.changed_setup_fields)
    if actual - declared:
        raise ExperimentError(f"undeclared setup differences: {', '.join(sorted(actual - declared))}")
    if declared - actual:
        raise ExperimentError(f"declared setup fields did not change: {', '.join(sorted(declared - actual))}")
    return {**conditions, **bounds}, changes
