"""Deterministic all-eligible-lap consistency, independent of comparisons."""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from dataclasses import asdict, dataclass
from statistics import median
from typing import TYPE_CHECKING, Literal, Mapping

from f1_telemetry.driving_events import DrivingEvent, detect_driving_events

if TYPE_CHECKING:
    from f1_telemetry.corner_catalog import TrackDefinition, TurnDefinition
    from f1_telemetry.lap_analysis import AnalysisConfig, LapTrace, PreparedSession


CONSISTENCY_FILENAME = "consistency.json"
CONSISTENCY_SCHEMA_VERSION = 1
CONSISTENCY_METHOD_VERSION = "1.0.0"
MINIMUM_LAPS = 3
Availability = Literal["available", "unavailable"]


@dataclass(frozen=True, slots=True)
class Distribution:
    minimum: float
    maximum: float
    median: float
    mad: float


@dataclass(frozen=True, slots=True)
class LapTiming:
    lap_number: int
    eligible_attempt_ordinal: int
    official_lap_time_s: float
    sector_times_s: tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class Trend:
    inputs: tuple[tuple[int, float], ...]
    slope_s_per_attempt: float
    intercept_s: float


@dataclass(frozen=True, slots=True)
class CornerLapSample:
    lap_number: int
    duration_s: float
    event_status: Literal["complete", "missing", "incomplete", "ambiguous"]
    selected_event_index: int | None
    summary_event_index: int | None
    events: tuple[DrivingEvent, ...]


@dataclass(frozen=True, slots=True)
class MetricDistribution:
    metric: str
    distribution: Distribution


@dataclass(frozen=True, slots=True)
class CornerConsistency:
    turn_id: str
    turn_number: int
    display_name: str
    samples: tuple[CornerLapSample, ...]
    duration_s: Distribution
    event_status: Availability
    event_reason: str | None
    event_summaries: tuple[MetricDistribution, ...]


@dataclass(frozen=True, slots=True)
class ConsistencyResult:
    status: Availability
    reason: str | None
    laps: tuple[LapTiming, ...]
    lap_time_s: Distribution | None
    sector_times_s: tuple[Distribution, ...]
    trend: Trend | None
    corner_status: Availability
    corner_reason: str | None
    corners: tuple[CornerConsistency, ...]
    schema_version: int = CONSISTENCY_SCHEMA_VERSION
    method_version: str = CONSISTENCY_METHOD_VERSION

    def json_bytes(self) -> bytes:
        return (json.dumps(asdict(self), sort_keys=True, indent=2, allow_nan=False) + "\n").encode()

    def descriptor(self, raw: bytes) -> dict[str, object]:
        return {
            "path": CONSISTENCY_FILENAME,
            "schema_version": self.schema_version,
            "method_version": self.method_version,
            "status": self.status,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }


def _distribution(values: tuple[float, ...]) -> Distribution:
    center = median(values)
    return Distribution(min(values), max(values), center, median(abs(value - center) for value in values))


def _trend(laps: tuple[LapTiming, ...]) -> Trend:
    inputs = tuple((lap.eligible_attempt_ordinal, lap.official_lap_time_s) for lap in laps)
    count = len(inputs)
    x_mean = (count + 1) / 2
    # Center using integer milliseconds to avoid drift on identical lap times.
    milliseconds = tuple(round(time_s * 1000) for _, time_s in inputs)
    slope = sum((x - x_mean) * value for (x, _), value in zip(inputs, milliseconds))
    slope /= sum((x - x_mean) ** 2 for x, _ in inputs) * 1000
    return Trend(inputs, slope, sum(milliseconds) / (1000 * count) - slope * x_mean)


def _timing_points(trace: LapTrace, length: int) -> tuple[tuple[float, ...], tuple[int, ...]]:
    internal = tuple(sample for sample in trace.samples if 0 < sample.lap_distance < length)
    return (
        (0.0, *(sample.lap_distance for sample in internal), float(length)),
        (0, *(sample.current_lap_time_ms for sample in internal), trace.official_lap_time_ms),
    )


def _elapsed_ms(distance: float, points: tuple[tuple[float, ...], tuple[int, ...]]) -> float:
    distances, milliseconds = points
    right = bisect_left(distances, distance)
    if distances[right] == distance:
        return milliseconds[right]
    left = right - 1
    portion = (distance - distances[left]) / (distances[right] - distances[left])
    return milliseconds[left] + portion * (milliseconds[right] - milliseconds[left])


def _corner_samples(
    trace: LapTrace,
    track: TrackDefinition,
    events: tuple[DrivingEvent, ...],
) -> tuple[CornerLapSample, ...]:
    by_turn: dict[str, list[DrivingEvent]] = {turn.turn_id: [] for turn in track.turns}
    for event in events:
        position = event.minimum_speed_distance
        turns = (
            turn for turn in track.turns if turn.entry_distance_m <= position <= turn.exit_distance_m
        )
        turn = min(turns, key=lambda item: (abs(item.apex_distance_m - position), item.number), default=None)
        if turn is not None:
            by_turn[turn.turn_id].append(event)

    points = _timing_points(trace, track.track_length_m)
    samples = []
    for turn in track.turns:
        candidates = tuple(sorted(by_turn[turn.turn_id], key=lambda item: item.event_index))
        selected = min(
            candidates,
            key=lambda item: (abs(item.minimum_speed_distance - turn.apex_distance_m), item.event_index),
            default=None,
        )
        complete = tuple(event for event in candidates if event.throttle_reapplication_distance is not None)
        if selected is None:
            state = "missing"
        elif len(complete) > 1:
            state = "ambiguous"
        elif not complete:
            state = "incomplete"
        else:
            state = "complete"
        duration = (_elapsed_ms(turn.exit_distance_m, points) - _elapsed_ms(turn.entry_distance_m, points)) / 1000
        samples.append(CornerLapSample(
            trace.lap_number, duration, state,
            selected.event_index if selected is not None else None,
            complete[0].event_index if len(complete) == 1 else None,
            candidates,
        ))
    return tuple(samples)


def _summarize_corner(turn: TurnDefinition, samples: tuple[CornerLapSample, ...]) -> CornerConsistency:
    complete = all(sample.event_status == "complete" for sample in samples)
    events = tuple(
        next(event for event in sample.events if event.event_index == sample.summary_event_index)
        for sample in samples
    ) if complete else ()
    metrics = (
        "braking_start_distance", "braking_end_distance", "braking_distance", "peak_brake",
        "minimum_speed_distance", "minimum_speed_kph", "throttle_reapplication_distance",
        "coast_distance",
    )
    return CornerConsistency(
        turn.turn_id, turn.number, turn.display_name, samples,
        _distribution(tuple(sample.duration_s for sample in samples)),
        "available" if complete else "unavailable",
        None if complete else "requires_one_complete_event_per_eligible_lap",
        tuple(MetricDistribution(metric, _distribution(tuple(getattr(event, metric) for event in events)))
              for metric in metrics) if complete else (),
    )


def analyze_consistency(
    session: PreparedSession,
    track: TrackDefinition | None,
    config: AnalysisConfig,
    *,
    known_events: Mapping[int, tuple[DrivingEvent, ...]] | None = None,
) -> ConsistencyResult:
    """Use all eligible laps in attempt order, even if comparisons omit some."""
    from f1_telemetry.lap_analysis import resample_lap

    traces = tuple(sorted(session.laps, key=lambda trace: trace.lap_number))
    laps = tuple(
        LapTiming(trace.lap_number, index, trace.official_lap_time_ms / 1000,
                  tuple(time_ms / 1000 for time_ms in trace.sector_times_ms))
        for index, trace in enumerate(traces, 1)
    )
    if len(laps) < MINIMUM_LAPS:
        return ConsistencyResult(
            "unavailable", "fewer_than_three_eligible_laps", laps, None, (), None,
            "unavailable", "fewer_than_three_eligible_laps", (),
        )

    corners = ()
    if track is not None:
        rows = []
        for trace in traces:
            if known_events is not None and trace.lap_number in known_events:
                events = known_events[trace.lap_number]
            else:
                grid = resample_lap(trace, session.track_length, step=config.distance_step_m)
                events = detect_driving_events(grid, config)
            rows.append(_corner_samples(trace, track, events))
        corners = tuple(_summarize_corner(turn, samples) for turn, samples in zip(track.turns, zip(*rows)))
    return ConsistencyResult(
        "available", None, laps, _distribution(tuple(lap.official_lap_time_s for lap in laps)),
        tuple(_distribution(tuple(lap.sector_times_s[index] for lap in laps)) for index in range(3)),
        _trend(laps), "available" if track is not None else "unavailable",
        None if track is not None else "no_catalog_track", corners,
    )
