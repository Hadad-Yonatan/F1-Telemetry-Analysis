"""Distance-aligned dynamics and track geometry, independent of core comparison math."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from math import atan2, hypot
from statistics import median
from typing import TYPE_CHECKING, Literal

from f1_telemetry.composite_motion import (
    MAX_MOTION_INTERPOLATION_GAP_M,
    CompositeCoverage,
    CompositeNormalizationResult,
    CompositeSample,
    resample_motion_channel,
)
from f1_telemetry.consistency import Distribution
from f1_telemetry.motion_decode import WHEEL_ORDER, Motion, MotionEx

if TYPE_CHECKING:
    from f1_telemetry.lap_analysis import LapTrace, PreparedSession


MOTION_FILENAME = "motion.json"
TRACK_MAP_FILENAME = "track-map.svg"
MOTION_SCHEMA_VERSION = 1
MOTION_METHOD_VERSION = "1.0.0"
MotionMode = Literal["auto", "required", "off"]

_G = ("lateral_g", "longitudinal_g")
_EX = (
    *(f"slip_ratio_{wheel}" for wheel in WHEEL_ORDER),
    *(f"slip_angle_{wheel}_rad" for wheel in WHEEL_ORDER),
    "front_wheels_angle_rad",
)
_LINE = ("lateral_deviation_m", "heading_difference_rad")
MOTION_METRICS = (*_G, *_EX, *_LINE)

Point = tuple[float, float]
_Observed = tuple[tuple[float, tuple[float, ...]], ...]
_Aligned = tuple[tuple[float, ...] | None, ...]


@dataclass(frozen=True, slots=True)
class MotionMetricSeries:
    metric: str
    values: tuple[float | None, ...]


@dataclass(frozen=True, slots=True)
class MotionLap:
    lap_number: int
    positions_m: tuple[Point | None, ...]
    break_before: tuple[bool, ...]
    metrics: tuple[MotionMetricSeries, ...]


@dataclass(frozen=True, slots=True)
class SharedMetric:
    metric: str
    count: int
    reference: Distribution | None
    comparison: Distribution | None
    difference: Distribution | None


@dataclass(frozen=True, slots=True)
class MotionComparison:
    lap_number: int
    shared_metrics: tuple[SharedMetric, ...]


@dataclass(frozen=True, slots=True)
class TrackTransform:
    timing_line_world_xz_m: Point
    forward_world_xz: Point

    def to_map(self, point: Point) -> Point:
        x = point[0] - self.timing_line_world_xz_m[0]
        z = point[1] - self.timing_line_world_xz_m[1]
        fx, fz = self.forward_world_xz
        return x * fz - z * fx, x * fx + z * fz


@dataclass(frozen=True, slots=True)
class MotionResult:
    status: Literal["available", "unavailable"]
    reason: str | None
    reference_lap: int
    coverage: CompositeCoverage
    transform: TrackTransform | None
    distance_m: tuple[float, ...]
    laps: tuple[MotionLap, ...]
    comparisons: tuple[MotionComparison, ...]
    schema_version: int = MOTION_SCHEMA_VERSION
    method_version: str = MOTION_METHOD_VERSION

    def json_bytes(self) -> bytes:
        return (json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()

    def descriptor(self, raw: bytes, svg: bytes) -> dict[str, object]:
        return {
            "path": MOTION_FILENAME,
            "schema_version": self.schema_version,
            "method_version": self.method_version,
            "status": self.status,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "track_map": {"path": TRACK_MAP_FILENAME, "sha256": hashlib.sha256(svg).hexdigest()},
        }


@dataclass(frozen=True, slots=True)
class MotionInputs:
    coverage: CompositeCoverage
    retained_groups: dict[int, tuple[Motion | None, MotionEx | None]]
    timing_line: Point | None


@dataclass(frozen=True, slots=True)
class _Trajectory:
    base: _Aligned
    directions: _Aligned
    extended: _Aligned
    break_before: tuple[bool, ...]
    tangents: _Aligned


def _xz(values: tuple[float, float, float]) -> Point:
    return values[0], values[2]


def _unit(x: float, z: float) -> Point | None:
    length = hypot(x, z)
    if length < 1e-9:
        return None
    return x / length, z / length


def _timing_line(reference: LapTrace, composite: CompositeNormalizationResult, groups: dict) -> Point | None:
    # Use the observed side of the retained start, not a synthetic point at 0.
    right = next((sample for sample in reference.samples if groups.get(id(sample), (None,))[0]), None)
    if right is None or not 0 <= right.lap_distance <= MAX_MOTION_INTERPOLATION_GAP_M:
        return None
    right_position = _xz(groups[id(right)][0].position_m)
    if right.lap_distance == 0:
        return right_position
    first = reference.samples[0]
    # Total distance places previous-lap packets on the same continuous
    # coordinate without treating the rounded Session length as a float offset.
    line_total_distance = first.total_distance - first.lap_distance
    left: CompositeSample | None = None
    for row in composite.samples:
        sample = row.core
        if (
            row.motion is not None
            and sample.session_uid == first.session_uid
            and sample.player_car_index == first.player_car_index
            and sample.overall_frame_identifier < first.overall_frame_identifier
            and sample.frame_identifier < first.frame_identifier
            and first.overall_frame_identifier - sample.overall_frame_identifier
                == first.frame_identifier - sample.frame_identifier
            and sample.session_time <= first.session_time
            and 0 <= line_total_distance - sample.total_distance <= MAX_MOTION_INTERPOLATION_GAP_M
            and (left is None or sample.overall_frame_identifier > left.core.overall_frame_identifier)
        ):
            left = row
    if left is None:
        return None
    left_distance = left.core.total_distance - line_total_distance
    span = right.lap_distance - left_distance
    if span > MAX_MOTION_INTERPOLATION_GAP_M:
        return None
    ratio = -left_distance / span
    left_position = _xz(left.motion.position_m)
    return tuple(a + ratio * (b - a) for a, b in zip(left_position, right_position))


def retain_motion_inputs(
    session: PreparedSession, composite: CompositeNormalizationResult, reference_lap: int,
) -> MotionInputs:
    """Retain optional groups only for the resolved attempts, with their line anchor."""
    retained = {id(sample) for trace in session.laps for sample in trace.samples}
    groups = {
        id(row.core): (row.motion, row.motion_ex)
        for row in composite.samples
        if id(row.core) in retained and (row.motion is not None or row.motion_ex is not None)
    }
    reference = next(trace for trace in session.laps if trace.lap_number == reference_lap)
    return MotionInputs(composite.coverage, groups, _timing_line(reference, composite, groups))


def _observations(trace: LapTrace, inputs: MotionInputs, track_length: int):
    for sample in trace.samples:
        if 0 <= sample.lap_distance <= track_length:
            motion, extended = inputs.retained_groups.get(id(sample), (None, None))
            yield sample.lap_distance, motion, extended


def _reference_transform(trace: LapTrace, inputs: MotionInputs, track_length: int) -> TrackTransform | None:
    if inputs.timing_line is None:
        return None
    for _, motion, _ in _observations(trace, inputs, track_length):
        if motion is not None:
            forward = _unit(*_xz(motion.forward))
            if forward is not None:
                return TrackTransform(inputs.timing_line, forward)
    return None


def _trajectory(
    trace: LapTrace, inputs: MotionInputs, distances: tuple[float, ...], track_length: int, *, reference: bool = False,
) -> _Trajectory:
    observations = tuple(_observations(trace, inputs, track_length))
    base_points = tuple(
        (distance, (*_xz(motion.position_m), *motion.g_force[:2]))
        for distance, motion, _ in observations if motion is not None
    )
    base = resample_motion_channel(base_points, distances)
    directions = resample_motion_channel(
        ((distance, forward) for distance, motion, _ in observations
         if motion is not None and (forward := _unit(*_xz(motion.forward))) is not None),
        distances,
    )
    extended = resample_motion_channel(
        ((distance, (*extra.wheel_slip_ratio, *extra.wheel_slip_angle, extra.front_wheels_angle_rad))
         for distance, _, extra in observations if extra is not None), distances,
    )
    return _Trajectory(
        base, directions, extended, _path_breaks(base_points, distances),
        resample_motion_channel(_observed_tangents(base_points), distances) if reference else (),
    )


def _summarize(values: tuple[float, ...]) -> Distribution:
    center = median(values)
    return Distribution(min(values), max(values), center, median(abs(value - center) for value in values))


def _shared(name: str, reference: tuple[float | None, ...], comparison: tuple[float | None, ...]) -> SharedMetric:
    paired = [(first, second) for first, second in zip(reference, comparison, strict=True)
              if first is not None and second is not None]
    if not paired:
        return SharedMetric(name, 0, None, None, None)
    return SharedMetric(
        name, len(paired), _summarize(tuple(first for first, _ in paired)),
        _summarize(tuple(second for _, second in paired)),
        _summarize(tuple(second - first for first, second in paired)),
    )


def _observed_tangents(points: _Observed):
    for index, (distance, current) in enumerate(points):
        left = index - 1
        right = index + 1
        before = left >= 0 and distance - points[left][0] <= MAX_MOTION_INTERPOLATION_GAP_M
        after = right < len(points) and points[right][0] - distance <= MAX_MOTION_INTERPOLATION_GAP_M
        if before and after and points[right][0] - points[left][0] > MAX_MOTION_INTERPOLATION_GAP_M:
            after = False
        start, end = points[left][1] if before else current, points[right][1] if after else current
        tangent = _unit(end[0] - start[0], end[1] - start[1])
        if tangent is not None:
            yield distance, tangent


def _path_breaks(points: _Observed, distances: tuple[float, ...]) -> tuple[bool, ...]:
    gaps = (
        (left[0], right[0]) for left, right in zip(points, points[1:])
        if right[0] - left[0] > MAX_MOTION_INTERPOLATION_GAP_M
    )
    gap = next(gaps, None)
    breaks = [True]
    for start, end in zip(distances, distances[1:]):
        while gap is not None and gap[1] <= start:
            gap = next(gaps, None)
        breaks.append(gap is not None and gap[0] < end)
    return tuple(breaks)


def _line_metrics(
    reference, comparison, tangent: Point | None, reference_forward, comparison_forward,
) -> tuple[float | None, float | None]:
    lateral = None
    unit_tangent = _unit(*tangent) if tangent is not None else None
    if unit_tangent is not None and reference is not None and comparison is not None:
        tx, tz = unit_tangent
        lateral = (comparison[0] - reference[0]) * tz - (comparison[1] - reference[1]) * tx
    reference_direction = _unit(*reference_forward) if reference_forward is not None else None
    comparison_direction = _unit(*comparison_forward) if comparison_forward is not None else None
    if reference_direction is None or comparison_direction is None:
        return lateral, None
    fx, fz = reference_direction
    cx, cz = comparison_direction
    heading = atan2(fz * cx - fx * cz, fx * cx + fz * cz)
    return lateral, heading


def _lap_profile(
    trace: LapTrace, trajectory: _Trajectory, reference_trajectory: _Trajectory,
    transform: TrackTransform | None, *, is_reference: bool = False,
) -> MotionLap:
    base, directions, extended = trajectory.base, trajectory.directions, trajectory.extended
    positions = tuple(transform.to_map((row[0], row[1])) if transform is not None and row is not None else None
                      for row in base)
    if is_reference and transform is not None:
        positions = ((0.0, 0.0), *positions[1:])
    line = tuple(
        _line_metrics(first, second, tangent, reference_forward, comparison_forward)
        for first, second, tangent, reference_forward, comparison_forward
        in zip(reference_trajectory.base, base, reference_trajectory.tangents,
               reference_trajectory.directions, directions, strict=True)
    )
    return MotionLap(
        trace.lap_number, positions, trajectory.break_before,
        (
            *(MotionMetricSeries(name, tuple(row[index] if row is not None else None for row in base))
              for index, name in enumerate(_G, 2)),
            *(MotionMetricSeries(name, tuple(row[index] if row is not None else None for row in extended))
              for index, name in enumerate(_EX)),
            *(MotionMetricSeries(name, tuple(row[index] for row in line))
              for index, name in enumerate(_LINE)),
        ),
    )


def analyze_motion(
    session: PreparedSession, inputs: MotionInputs, reference_lap: int, distances: tuple[float, ...],
) -> MotionResult:
    traces = tuple(sorted(session.laps, key=lambda trace: trace.lap_number))
    reference = next(trace for trace in traces if trace.lap_number == reference_lap)
    transform = _reference_transform(reference, inputs, session.track_length)
    reference_trajectory = _trajectory(reference, inputs, distances, session.track_length, reference=True)
    reference_profile = _lap_profile(
        reference, reference_trajectory, reference_trajectory, transform, is_reference=True,
    )
    laps, comparisons = [], []
    for trace in traces:
        if trace is reference:
            laps.append(reference_profile)
            continue
        trajectory = _trajectory(trace, inputs, distances, session.track_length)
        profile = _lap_profile(trace, trajectory, reference_trajectory, transform)
        laps.append(profile)
        comparisons.append(MotionComparison(
            trace.lap_number,
            tuple(_shared(first.metric, first.values, second.values)
                  for first, second in zip(reference_profile.metrics, profile.metrics, strict=True)),
        ))
    available = any(metric.count for comparison in comparisons for metric in comparison.shared_metrics)
    return MotionResult(
        "available" if available else "unavailable",
        None if available else "no_shared_valid_motion_samples",
        reference_lap, inputs.coverage, transform, distances, tuple(laps), tuple(comparisons),
    )


def require_motion(result: MotionResult, comparison_laps: set[int]) -> None:
    if result.transform is None:
        raise ValueError("required motion cannot anchor the reference at the timing line")
    for comparison in result.comparisons:
        if comparison.lap_number in comparison_laps:
            counts = {metric.metric: metric.count for metric in comparison.shared_metrics}
            if not counts["heading_difference_rad"] or not counts["front_wheels_angle_rad"]:
                raise ValueError(f"required Motion and Motion Ex have no shared valid data for lap {comparison.lap_number}")
