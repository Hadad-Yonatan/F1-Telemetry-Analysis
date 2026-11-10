"""Controlled experiment metrics with explicit sample support and missing state."""

from __future__ import annotations

from math import hypot
from statistics import median

from f1_telemetry.experiment_model import (
    EXPERIMENT_METHOD_VERSION, EXPERIMENT_SCHEMA_VERSION, MIN_GROUP_LAPS,
    ExperimentManifest, GroupEvidence, check_groups, collect_group,
)
from f1_telemetry.motion_analysis import MOTION_METRICS


_EVENT_METRICS = {
    "braking_start_distance": "m",
    "braking_distance": "m",
    "peak_brake": "fraction",
    "minimum_speed_kph": "km/h",
    "throttle_reapplication_distance": "m",
    "coast_distance": "m",
}
_TRACTION = {
    name: "rad" if name.endswith("_rad") else "G" if name.endswith("_g") else "ratio"
    for name in MOTION_METRICS if name not in ("lateral_deviation_m", "heading_difference_rad")
}


def _sample(lap: int, value: float | None, support: int = 1, reason: str | None = None) -> dict:
    return {"lap_number": lap, "value": value, "support_count": support if value is not None else 0, "reason": reason}


def _summary(samples: list[dict]) -> dict:
    observed = [sample["value"] for sample in samples if sample["value"] is not None]
    center = median(observed) if observed else None
    return {
        "samples": samples,
        "count": len(observed),
        "median": center,
        "mad": median(abs(value - center) for value in observed) if observed else None,
    }


def _metric(name: str, unit: str, control: list[dict], candidate: list[dict]) -> dict:
    first, second = _summary(control), _summary(candidate)
    comparable = min(first["count"], second["count"]) >= MIN_GROUP_LAPS
    return {
        "metric": name, "unit": unit, "control": first, "candidate": second,
        "candidate_minus_control": second["median"] - first["median"] if comparable else None,
        "reason": None if comparable else "requires_three_observed_laps_in_each_group",
    }


def _metric_values(name, unit, groups, lookup) -> dict:
    return _metric(name, unit, *[
        [lookup(group, lap) for lap in group.laps] for group in groups
    ])


def _timing_metrics(groups: tuple[GroupEvidence, GroupEvidence]) -> list[dict]:
    output = [_metric_values(
        "pace.lap_time", "s", groups, lambda _, lap: _sample(lap.lap_number, lap.official_lap_time_s),
    )]
    for index in range(3):
        output.append(_metric_values(
            f"pace.sector_{index + 1}", "s", groups,
            lambda _, lap: _sample(lap.lap_number, lap.sectors_s[index]),
        ))
    return output


def _turns(group: GroupEvidence) -> list[dict]:
    track = (group.bundle.document.get("corner_catalog") or {}).get("selected_track") or {}
    return track.get("turns", [])


def _corner_metrics(groups: tuple[GroupEvidence, GroupEvidence]) -> tuple[list[dict], str | None]:
    turns = _turns(groups[0])
    if not turns or turns != _turns(groups[1]):
        return [], "requires_matching_catalog_turn_definitions"
    all_corners = {
        group.request.label: {
            corner["turn_id"]: {item["lap_number"]: item for item in corner["samples"]}
            for corner in (group.bundle.consistency or {}).get("corners", [])
        }
        for group in groups
    }
    output = []
    for turn in turns:
        turn_id = turn["turn_id"]

        def evidence(group, lap, field=None):
            number = lap.lap_number
            raw = all_corners[group.request.label].get(turn_id, {}).get(number)
            if raw is None:
                return _sample(number, None, reason="corner_evidence_unavailable")
            if field is None:
                return _sample(number, raw["duration_s"])
            if raw["event_status"] != "complete":
                return _sample(number, None, reason=f"event_{raw['event_status']}")
            event = next(item for item in raw["events"] if item["event_index"] == raw["summary_event_index"])
            return _sample(number, event[field])

        output.append(_metric_values(f"consistency.{turn_id}.duration", "s", groups, evidence))
        for name, unit in _EVENT_METRICS.items():
            output.append(_metric_values(
                f"event.{turn_id}.{name}", unit, groups, lambda group, lap: evidence(group, lap, name),
            ))
    return output, None


def _motion_inputs(groups: tuple[GroupEvidence, GroupEvidence]):
    first, second = (group.bundle.motion for group in groups)
    if first is None or second is None:
        return None, "motion_not_available_in_both_bundles"
    if first["distance_m"] != second["distance_m"]:
        return None, "motion_distance_grids_differ"
    return tuple(
        {
            "transform": document.get("transform"),
            "profiles": {lap["lap_number"]: lap for lap in document["laps"]},
        }
        for document in (first, second)
    ), None


def _shared_motion_metric(name: str, unit: str, groups, series, reason: str | None) -> dict:
    missing = reason or "no_shared_valid_distances"
    all_series = [values for group in series for values in group] if series is not None else []
    indices = [
        index for index, values in enumerate(zip(*all_series, strict=True))
        if all(value is not None for value in values)
    ] if all_series else []
    return _metric(name, unit, *[
        [
            _sample(lap.lap_number, median(series[group_index][lap_index][i] for i in indices), len(indices))
            if indices else _sample(lap.lap_number, None, reason=missing)
            for lap_index, lap in enumerate(group.laps)
        ]
        for group_index, group in enumerate(groups)
    ])


def _world_points(profile: dict, transform: dict) -> tuple[tuple[float, float] | None, ...]:
    x0, z0 = transform["timing_line_world_xz_m"]
    fx, fz = transform["forward_world_xz"]
    return tuple(
        None if point is None else (x0 + point[0] * fz + point[1] * fx, z0 - point[0] * fx + point[1] * fz)
        for point in profile["positions_m"]
    )


def _path_tangents(points: tuple, breaks: list[bool]) -> tuple:
    result = []
    for index, point in enumerate(points):
        if point is None:
            result.append(None)
            continue
        before = points[index - 1] if index and not breaks[index] else None
        after = points[index + 1] if index + 1 < len(points) and not breaks[index + 1] else None
        first, last = before or point, after or point
        dx, dz = last[0] - first[0], last[1] - first[1]
        length = hypot(dx, dz)
        result.append((dx / length, dz / length) if length > 1e-9 else None)
    return tuple(result)


def _line_series(groups, motion) -> tuple[tuple, str | None]:
    if any(group["transform"] is None for group in motion):
        return (), "a_common_world_coordinate_frame_is_unavailable"
    reference = motion[0]["profiles"][groups[0].laps[0].lap_number]
    ref_points = _world_points(reference, motion[0]["transform"])
    tangents = _path_tangents(ref_points, reference["break_before"])
    output = []
    for group, source in zip(groups, motion):
        laps = []
        for lap in group.laps:
            points = _world_points(source["profiles"][lap.lap_number], source["transform"])
            laps.append(tuple(
                None if point is None or anchor is None or tangent is None
                else (point[0] - anchor[0]) * tangent[1] - (point[1] - anchor[1]) * tangent[0]
                for point, anchor, tangent in zip(points, ref_points, tangents, strict=True)
            ))
        output.append(tuple(laps))
    return tuple(output), None


def _motion_metrics(groups: tuple[GroupEvidence, GroupEvidence]) -> tuple[list[dict], str | None]:
    motion, reason = _motion_inputs(groups)
    output = []
    for name, unit in _TRACTION.items():
        series = tuple(
            tuple(next(item["values"] for item in source["profiles"][lap.lap_number]["metrics"] if item["metric"] == name)
                  for lap in group.laps)
            for group, source in zip(groups, motion)
        ) if motion is not None else None
        output.append(_shared_motion_metric(f"dynamics.{name}", unit, groups, series, reason))
    lines, line_reason = _line_series(groups, motion) if motion is not None else ((), reason)
    output.append(_shared_motion_metric("line.signed_lateral_deviation", "m", groups, lines, line_reason))
    absolute = tuple(tuple(tuple(abs(value) if value is not None else None for value in lap) for lap in group) for group in lines)
    output.append(_shared_motion_metric("line.absolute_lateral_deviation", "m", groups, absolute, line_reason))
    return output, line_reason


def evaluate_experiment(manifest: ExperimentManifest, control_bundle, candidate_bundle) -> dict:
    control = collect_group(manifest.control, control_bundle)
    candidate = collect_group(manifest.candidate, candidate_bundle)
    groups = control, candidate
    conditions, changes = check_groups(manifest, *groups)
    corner_metrics, corner_reason = _corner_metrics(groups)
    motion_metrics, line_reason = _motion_metrics(groups)
    return {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "method_version": EXPERIMENT_METHOD_VERSION,
        "manifest": manifest.document(),
        "track": {"track_id": control.track[0], "track_length_m": control.track[1]},
        "session_policy": control.bundle.document["session_policy"],
        "conditions": conditions,
        "control": control.document(),
        "candidate": candidate.document(),
        "setup_changes": changes,
        "metrics": [*_timing_metrics(groups), *corner_metrics, *motion_metrics],
        "catalog_corner_reason": corner_reason,
        "line_reason": line_reason,
        "line_reference": {"bundle_id": control.request.bundle_id, "lap_number": control.laps[0].lap_number},
        "interpretation": "Numeric associations only. These comparisons do not prescribe setup changes.",
    }
