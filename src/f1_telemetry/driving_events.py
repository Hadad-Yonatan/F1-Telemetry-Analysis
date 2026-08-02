"""Deterministic driving-event extraction from distance-aligned telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class AlignedTelemetry(Protocol):
    distance: tuple[float, ...]
    speed_kph: tuple[float, ...]
    throttle: tuple[float, ...]
    brake: tuple[float, ...]


class EventParameters(Protocol):
    brake_on_threshold: float
    brake_on_sustain_m: float
    brake_off_threshold: float
    brake_off_sustain_m: float
    brake_gap_merge_m: float
    minimum_braking_event_length_m: float
    minimum_peak_brake: float
    minimum_speed_search_m: float
    full_throttle_threshold: float
    full_throttle_sustain_m: float
    coast_throttle_threshold: float
    coast_brake_threshold: float
    coast_minimum_speed_kph: float
    minimum_coast_length_m: float
    maximum_event_match_distance_m: float
    event_exit_extension_m: float
    minimum_braking_point_difference_m: float
    minimum_braking_distance_difference_m: float
    minimum_coast_difference_m: float
    minimum_throttle_reapplication_difference_m: float
    minimum_speed_difference_kph: float


@dataclass(frozen=True)
class DrivingEvent:
    event_index: int
    braking_start_distance: float
    braking_end_distance: float
    braking_distance: float
    peak_brake: float
    minimum_speed_distance: float
    minimum_speed_kph: float
    throttle_reapplication_distance: float | None
    coast_distance: float
    analysis_end_distance: float


@dataclass(frozen=True)
class EventDifferences:
    braking_start_distance: float
    braking_end_distance: float
    braking_distance: float
    peak_brake: float
    minimum_speed_distance: float
    minimum_speed_kph: float
    throttle_reapplication_distance: float | None
    coast_distance: float


@dataclass(frozen=True)
class MatchedDrivingEvent:
    match_index: int
    reference: DrivingEvent
    comparison: DrivingEvent
    differences: EventDifferences
    evidence_labels: tuple[str, ...]


@dataclass(frozen=True)
class EventMatchResult:
    matches: tuple[MatchedDrivingEvent, ...]
    unmatched_reference_event_indices: tuple[int, ...]
    unmatched_comparison_event_indices: tuple[int, ...]


@dataclass(frozen=True)
class _BrakingRange:
    start_index: int
    end_index: int


def _sustained_run_end(
    distance: tuple[float, ...],
    values: tuple[float, ...],
    start_index: int,
    threshold: float,
    required_distance: float,
    maximum_index: int,
) -> int | None:
    for index in range(start_index, maximum_index + 1):
        if values[index] < threshold:
            return None
        if distance[index] - distance[start_index] >= required_distance:
            return index
    return None


def _raw_braking_ranges(
    lap: AlignedTelemetry,
    config: EventParameters,
) -> tuple[_BrakingRange, ...]:
    ranges = []
    active_start: int | None = None
    on_start: int | None = None
    off_start: int | None = None

    for index, brake in enumerate(lap.brake):
        if active_start is None:
            if brake >= config.brake_on_threshold:
                if on_start is None:
                    on_start = index
                if (
                    lap.distance[index] - lap.distance[on_start]
                    >= config.brake_on_sustain_m
                ):
                    active_start = on_start
                    off_start = None
            else:
                on_start = None
            continue

        if brake <= config.brake_off_threshold:
            if off_start is None:
                off_start = index
            if (
                lap.distance[index] - lap.distance[off_start]
                >= config.brake_off_sustain_m
            ):
                ranges.append(
                    _BrakingRange(
                        active_start,
                        max(active_start, off_start - 1),
                    )
                )
                active_start = None
                on_start = None
                off_start = None
        else:
            off_start = None

    if active_start is not None:
        ranges.append(_BrakingRange(active_start, len(lap.distance) - 1))
    return tuple(ranges)


def _merged_braking_ranges(
    lap: AlignedTelemetry,
    config: EventParameters,
) -> tuple[_BrakingRange, ...]:
    merged: list[_BrakingRange] = []
    for current in _raw_braking_ranges(lap, config):
        if (
            merged
            and lap.distance[current.start_index]
            - lap.distance[merged[-1].end_index]
            <= config.brake_gap_merge_m
        ):
            merged[-1] = _BrakingRange(
                merged[-1].start_index,
                current.end_index,
            )
        else:
            merged.append(current)

    accepted = []
    for event_range in merged:
        start = event_range.start_index
        end = event_range.end_index
        if (
            lap.distance[end] - lap.distance[start]
            < config.minimum_braking_event_length_m
        ):
            continue
        if max(lap.brake[start : end + 1]) < config.minimum_peak_brake:
            continue
        accepted.append(event_range)
    return tuple(accepted)


def _last_index_at_or_before(
    distance: tuple[float, ...],
    maximum_distance: float,
) -> int:
    result = 0
    for index, value in enumerate(distance):
        if value > maximum_distance:
            break
        result = index
    return result


def _coast_distance(
    lap: AlignedTelemetry,
    start_index: int,
    end_index: int,
    config: EventParameters,
) -> float:
    total = 0.0
    run_start: int | None = None
    for index in range(start_index, end_index + 1):
        coasting = (
            lap.throttle[index] <= config.coast_throttle_threshold
            and lap.brake[index] <= config.coast_brake_threshold
            and lap.speed_kph[index] >= config.coast_minimum_speed_kph
        )
        if coasting and run_start is None:
            run_start = index
        if not coasting and run_start is not None:
            run_end = index - 1
            length = lap.distance[run_end] - lap.distance[run_start]
            if length >= config.minimum_coast_length_m:
                total += length
            run_start = None
    if run_start is not None:
        length = lap.distance[end_index] - lap.distance[run_start]
        if length >= config.minimum_coast_length_m:
            total += length
    return total


def detect_driving_events(
    lap: AlignedTelemetry,
    config: EventParameters,
) -> tuple[DrivingEvent, ...]:
    """Extract accepted braking events and their corner-exit measurements."""
    ranges = _merged_braking_ranges(lap, config)
    events = []
    for event_index, event_range in enumerate(ranges):
        start = event_range.start_index
        end = event_range.end_index
        search_limit = lap.distance[end] + config.minimum_speed_search_m
        if event_index + 1 < len(ranges):
            search_limit = min(
                search_limit,
                lap.distance[ranges[event_index + 1].start_index],
            )
        search_end = _last_index_at_or_before(lap.distance, search_limit)
        minimum_speed_index = min(
            range(start, search_end + 1),
            key=lambda index: (lap.speed_kph[index], lap.distance[index]),
        )

        throttle_index = None
        for index in range(minimum_speed_index, search_end + 1):
            sustained_end = _sustained_run_end(
                lap.distance,
                lap.throttle,
                index,
                config.full_throttle_threshold,
                config.full_throttle_sustain_m,
                search_end,
            )
            if sustained_end is not None:
                throttle_index = index
                break

        coast_end = throttle_index if throttle_index is not None else search_end
        coast = _coast_distance(lap, end, coast_end, config)
        throttle_distance = (
            lap.distance[throttle_index] if throttle_index is not None else None
        )
        analysis_end = (
            throttle_distance + config.event_exit_extension_m
            if throttle_distance is not None
            else lap.distance[search_end]
        )
        analysis_end = min(analysis_end, lap.distance[-1])
        events.append(
            DrivingEvent(
                event_index=event_index,
                braking_start_distance=lap.distance[start],
                braking_end_distance=lap.distance[end],
                braking_distance=lap.distance[end] - lap.distance[start],
                peak_brake=max(lap.brake[start : end + 1]),
                minimum_speed_distance=lap.distance[minimum_speed_index],
                minimum_speed_kph=lap.speed_kph[minimum_speed_index],
                throttle_reapplication_distance=throttle_distance,
                coast_distance=coast,
                analysis_end_distance=analysis_end,
            )
        )
    return tuple(events)


def _event_labels(
    differences: EventDifferences,
    config: EventParameters,
) -> tuple[str, ...]:
    labels = []
    if (
        differences.braking_start_distance
        >= config.minimum_braking_point_difference_m
    ):
        labels.append("braked later")
    elif (
        differences.braking_start_distance
        <= -config.minimum_braking_point_difference_m
    ):
        labels.append("braked earlier")

    if (
        differences.braking_distance
        >= config.minimum_braking_distance_difference_m
    ):
        labels.append("braked over a longer distance")
    elif (
        differences.braking_distance
        <= -config.minimum_braking_distance_difference_m
    ):
        labels.append("braked over a shorter distance")

    if differences.coast_distance >= config.minimum_coast_difference_m:
        labels.append("coasted longer")
    elif differences.coast_distance <= -config.minimum_coast_difference_m:
        labels.append("coasted less")

    throttle_difference = differences.throttle_reapplication_distance
    if throttle_difference is not None:
        if (
            throttle_difference
            >= config.minimum_throttle_reapplication_difference_m
        ):
            labels.append("full throttle occurred later")
        elif (
            throttle_difference
            <= -config.minimum_throttle_reapplication_difference_m
        ):
            labels.append("full throttle occurred earlier")

    if differences.minimum_speed_kph <= -config.minimum_speed_difference_kph:
        labels.append("lower minimum speed")
    elif differences.minimum_speed_kph >= config.minimum_speed_difference_kph:
        labels.append("higher minimum speed")

    if not labels:
        labels.append("event differences below thresholds")
    return tuple(labels)


def match_driving_events(
    reference_events: tuple[DrivingEvent, ...],
    comparison_events: tuple[DrivingEvent, ...],
    config: EventParameters,
) -> EventMatchResult:
    """Greedily pair events by nearest unmatched minimum-speed location."""
    comparison_by_index = {
        event.event_index: event for event in comparison_events
    }
    unused_comparison = set(comparison_by_index)
    matches = []
    unmatched_reference = []
    for reference in reference_events:
        candidates = [
            comparison_by_index[index]
            for index in unused_comparison
            if abs(
                comparison_by_index[index].minimum_speed_distance
                - reference.minimum_speed_distance
            )
            <= config.maximum_event_match_distance_m
        ]
        if not candidates:
            unmatched_reference.append(reference.event_index)
            continue
        comparison = min(
            candidates,
            key=lambda event: (
                abs(event.minimum_speed_distance - reference.minimum_speed_distance),
                event.minimum_speed_distance,
                event.event_index,
            ),
        )
        unused_comparison.remove(comparison.event_index)
        throttle_difference = None
        if (
            reference.throttle_reapplication_distance is not None
            and comparison.throttle_reapplication_distance is not None
        ):
            throttle_difference = (
                comparison.throttle_reapplication_distance
                - reference.throttle_reapplication_distance
            )
        differences = EventDifferences(
            braking_start_distance=(
                comparison.braking_start_distance
                - reference.braking_start_distance
            ),
            braking_end_distance=(
                comparison.braking_end_distance - reference.braking_end_distance
            ),
            braking_distance=(
                comparison.braking_distance - reference.braking_distance
            ),
            peak_brake=comparison.peak_brake - reference.peak_brake,
            minimum_speed_distance=(
                comparison.minimum_speed_distance
                - reference.minimum_speed_distance
            ),
            minimum_speed_kph=(
                comparison.minimum_speed_kph - reference.minimum_speed_kph
            ),
            throttle_reapplication_distance=throttle_difference,
            coast_distance=comparison.coast_distance - reference.coast_distance,
        )
        matches.append(
            MatchedDrivingEvent(
                match_index=len(matches),
                reference=reference,
                comparison=comparison,
                differences=differences,
                evidence_labels=_event_labels(differences, config),
            )
        )
    return EventMatchResult(
        matches=tuple(matches),
        unmatched_reference_event_indices=tuple(unmatched_reference),
        unmatched_comparison_event_indices=tuple(
            sorted(unused_comparison)
        ),
    )


def matched_event_for_region(
    start_distance: float,
    end_distance: float,
    matches: tuple[MatchedDrivingEvent, ...],
) -> int | None:
    """Return the match with the largest event-window overlap."""
    candidates = []
    for match in matches:
        event_start = min(
            match.reference.braking_start_distance,
            match.comparison.braking_start_distance,
        )
        event_end = max(
            match.reference.analysis_end_distance,
            match.comparison.analysis_end_distance,
        )
        overlap = min(end_distance, event_end) - max(start_distance, event_start)
        if overlap <= 0:
            continue
        candidates.append(
            (
                -overlap,
                abs(
                    match.comparison.minimum_speed_distance
                    - match.reference.minimum_speed_distance
                ),
                match.match_index,
            )
        )
    if not candidates:
        return None
    return min(candidates)[2]


def unmatched_events_for_region(
    start_distance: float,
    end_distance: float,
    events: tuple[DrivingEvent, ...],
    unmatched_event_indices: tuple[int, ...],
) -> tuple[int, ...]:
    """Return unmatched events whose analysis windows overlap a region."""
    unmatched = set(unmatched_event_indices)
    return tuple(
        event.event_index
        for event in events
        if event.event_index in unmatched
        and min(end_distance, event.analysis_end_distance)
        - max(start_distance, event.braking_start_distance)
        > 0
    )
