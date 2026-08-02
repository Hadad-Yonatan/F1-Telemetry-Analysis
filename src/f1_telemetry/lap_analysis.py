"""Validate, reconstruct, align, and compare completed F1 laps."""

from __future__ import annotations

import argparse
import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean
from typing import Sequence

from f1_telemetry.capture_format import iter_capture, read_capture_header
from f1_telemetry.decode import (
    SESSION_HISTORY_PACKET_ID,
    SESSION_PACKET_ID,
    DecodedLapHistory,
    DecodedSessionData,
    decode_session_data,
    decode_session_history,
)
from f1_telemetry.driving_events import (
    DrivingEvent,
    MatchedDrivingEvent,
    detect_driving_events,
    match_driving_events,
    matched_event_for_region,
    unmatched_events_for_region,
)
from f1_telemetry.normalize import (
    NormalizationResult,
    NormalizedSample,
    normalize_capture,
)
from f1_telemetry.probe import parse_header_prefix


@dataclass(frozen=True)
class LapValidation:
    lap_number: int
    official_lap_time_ms: int
    sector_times_ms: tuple[int, int, int]
    validity_flags: int
    officially_valid: bool
    structurally_complete: bool
    eligible: bool
    reason: str


@dataclass(frozen=True)
class LapTrace:
    lap_number: int
    official_lap_time_ms: int
    samples: tuple[NormalizedSample, ...]
    discarded_attempt_frames: int
    sector_times_ms: tuple[int, int, int] = (0, 0, 0)


@dataclass(frozen=True)
class NormalizationSummary:
    capture_datagrams: int = 0
    lap_packets: int = 0
    telemetry_packets: int = 0
    samples_joined: int = 0
    ignored_other_packets: int = 0
    malformed_packet_headers: int = 0
    unsupported_packet_formats: int = 0
    decode_errors: int = 0
    duplicate_lap_packets: int = 0
    duplicate_telemetry_packets: int = 0
    session_time_mismatches: int = 0
    unmatched_lap_packets: int = 0
    unmatched_telemetry_packets: int = 0


@dataclass(frozen=True)
class PreparedSession:
    session_uid: int
    track_length: int
    track_id: int
    sector2_distance: float
    sector3_distance: float
    validations: tuple[LapValidation, ...]
    laps: tuple[LapTrace, ...]
    normalization: NormalizationSummary = NormalizationSummary()


@dataclass(frozen=True)
class ResampledLap:
    lap_number: int
    official_lap_time_ms: int
    distance: tuple[float, ...]
    elapsed_s: tuple[float, ...]
    speed_kph: tuple[float, ...]
    throttle: tuple[float, ...]
    brake: tuple[float, ...]
    steering: tuple[float, ...]
    gear: tuple[int, ...]


@dataclass(frozen=True)
class _SourcePoint:
    distance: float
    elapsed_time: float
    speed: float
    throttle: float
    brake: float
    steering: float
    gear: int


@dataclass(frozen=True)
class AnalysisConfig:
    schema_version: int = 1
    algorithm_version: str = "1.1.0"
    distance_step_m: float = 1.0
    loss_bin_size_m: float = 25.0
    minimum_bin_loss_s: float = 0.003
    minimum_region_loss_s: float = 0.020
    minimum_speed_difference_kph: float = 2.0
    minimum_throttle_difference: float = 0.05
    minimum_brake_difference: float = 0.05
    brake_on_threshold: float = 0.05
    brake_on_sustain_m: float = 3.0
    brake_off_threshold: float = 0.02
    brake_off_sustain_m: float = 5.0
    brake_gap_merge_m: float = 5.0
    minimum_braking_event_length_m: float = 10.0
    minimum_peak_brake: float = 0.20
    minimum_speed_search_m: float = 250.0
    full_throttle_threshold: float = 0.80
    full_throttle_sustain_m: float = 10.0
    coast_throttle_threshold: float = 0.05
    coast_brake_threshold: float = 0.02
    coast_minimum_speed_kph: float = 80.0
    minimum_coast_length_m: float = 10.0
    maximum_event_match_distance_m: float = 150.0
    event_exit_extension_m: float = 100.0
    minimum_braking_point_difference_m: float = 5.0
    minimum_braking_distance_difference_m: float = 10.0
    minimum_coast_difference_m: float = 10.0
    minimum_throttle_reapplication_difference_m: float = 5.0

    def __post_init__(self) -> None:
        if self.schema_version < 1:
            raise ValueError("schema version must be positive")
        if not self.algorithm_version:
            raise ValueError("algorithm version cannot be empty")
        if self.distance_step_m <= 0:
            raise ValueError("distance step must be greater than zero")
        if self.loss_bin_size_m <= 0:
            raise ValueError("loss bin size must be greater than zero")
        if not 0 <= self.brake_off_threshold <= self.brake_on_threshold <= 1:
            raise ValueError("brake thresholds must satisfy 0 <= off <= on <= 1")
        if not 0 <= self.minimum_peak_brake <= 1:
            raise ValueError("minimum peak brake must be between zero and one")
        if not 0 <= self.full_throttle_threshold <= 1:
            raise ValueError("full-throttle threshold must be between zero and one")
        if not 0 <= self.coast_throttle_threshold <= 1:
            raise ValueError("coast throttle threshold must be between zero and one")
        if not 0 <= self.coast_brake_threshold <= 1:
            raise ValueError("coast brake threshold must be between zero and one")
        numeric_thresholds = (
            self.minimum_bin_loss_s,
            self.minimum_region_loss_s,
            self.minimum_speed_difference_kph,
            self.minimum_throttle_difference,
            self.minimum_brake_difference,
            self.brake_on_sustain_m,
            self.brake_off_sustain_m,
            self.brake_gap_merge_m,
            self.minimum_braking_event_length_m,
            self.minimum_speed_search_m,
            self.full_throttle_sustain_m,
            self.coast_minimum_speed_kph,
            self.minimum_coast_length_m,
            self.maximum_event_match_distance_m,
            self.event_exit_extension_m,
            self.minimum_braking_point_difference_m,
            self.minimum_braking_distance_difference_m,
            self.minimum_coast_difference_m,
            self.minimum_throttle_reapplication_difference_m,
        )
        if any(value < 0 for value in numeric_thresholds):
            raise ValueError("analysis thresholds cannot be negative")


DEFAULT_ANALYSIS_CONFIG = AnalysisConfig()


@dataclass(frozen=True)
class CaptureMetadata:
    basename: str
    size_bytes: int
    sha256: str
    created_unix_ns: int


@dataclass(frozen=True)
class LossRegion:
    start_distance: float
    end_distance: float
    time_loss_s: float
    reference_min_speed_kph: float
    comparison_min_speed_kph: float
    reference_mean_throttle: float
    comparison_mean_throttle: float
    reference_mean_brake: float
    comparison_mean_brake: float
    minimum_speed_difference_kph: float
    mean_throttle_difference: float
    mean_brake_difference: float
    evidence_labels: tuple[str, ...]
    sector: int
    matched_event_index: int | None
    unmatched_reference_event_indices: tuple[int, ...]
    unmatched_comparison_event_indices: tuple[int, ...]


@dataclass(frozen=True)
class LapComparison:
    reference: ResampledLap
    comparison: ResampledLap
    delta_s: tuple[float, ...]
    sector_deltas_s: tuple[float, float, float]
    official_delta_s: float
    calculated_final_delta_s: float
    final_delta_error_s: float
    reference_discarded_attempt_frames: int
    comparison_discarded_attempt_frames: int
    regions: tuple[LossRegion, ...]
    reference_events: tuple[DrivingEvent, ...]
    comparison_events: tuple[DrivingEvent, ...]
    event_matches: tuple[MatchedDrivingEvent, ...]
    unmatched_reference_event_indices: tuple[int, ...]
    unmatched_comparison_event_indices: tuple[int, ...]

    @property
    def reference_lap(self) -> int:
        return self.reference.lap_number

    @property
    def comparison_lap(self) -> int:
        return self.comparison.lap_number


@dataclass(frozen=True)
class AnalysisResult:
    config: AnalysisConfig
    capture: CaptureMetadata
    session: PreparedSession
    reference_lap: int
    comparisons: tuple[LapComparison, ...]


def _validation(history: DecodedLapHistory) -> LapValidation:
    eligible = history.officially_valid and history.structurally_complete
    if eligible:
        reason = "eligible"
    elif not history.structurally_complete:
        reason = "incomplete"
    else:
        reason = "officially invalid"
    return LapValidation(
        lap_number=history.lap_number,
        official_lap_time_ms=history.lap_time_ms,
        sector_times_ms=(
            history.sector1_time_ms,
            history.sector2_time_ms,
            history.sector3_time_ms,
        ),
        validity_flags=history.validity_flags,
        officially_valid=history.officially_valid,
        structurally_complete=history.structurally_complete,
        eligible=eligible,
        reason=reason,
    )


def _resolve_trace(
    samples: list[NormalizedSample],
    validation: LapValidation,
    track_length: int,
) -> LapTrace:
    ordered = sorted(samples, key=lambda sample: sample.overall_frame_identifier)
    # Restart Lap can retain the lap number and keep frame IDs increasing while
    # resetting the timer. The last timer start near the line is the attempt
    # that eventually produced the official completed-lap record.
    timer_starts = [
        index
        for index in range(1, len(ordered))
        if ordered[index - 1].current_lap_time_ms == 0
        and ordered[index].current_lap_time_ms > 0
        and ordered[index].lap_distance < 100
    ]
    start_index = timer_starts[-1] if timer_starts else 0
    if start_index > 0 and ordered[start_index - 1].lap_distance >= 0:
        start_index -= 1
    ordered = ordered[start_index:]
    candidates = [
        sample
        for sample in ordered
        if sample.driver_status == 1
        and sample.lap_distance >= 0
        and sample.lap_distance <= track_length + 5
        and sample.current_lap_time_ms <= validation.official_lap_time_ms
    ]

    monotonic: list[NormalizedSample] = []
    for sample in candidates:
        if monotonic and sample.lap_distance <= monotonic[-1].lap_distance:
            continue
        if monotonic and sample.current_lap_time_ms < monotonic[-1].current_lap_time_ms:
            continue
        monotonic.append(sample)

    if len(monotonic) < 2:
        raise ValueError(f"lap {validation.lap_number} has too few usable samples")
    if monotonic[-1].lap_distance < track_length - 10:
        raise ValueError(
            f"lap {validation.lap_number} trace ends at "
            f"{monotonic[-1].lap_distance:.1f} m, before the finish"
        )

    return LapTrace(
        lap_number=validation.lap_number,
        official_lap_time_ms=validation.official_lap_time_ms,
        samples=tuple(monotonic),
        discarded_attempt_frames=start_index,
        sector_times_ms=validation.sector_times_ms,
    )


def _normalization_summary(result: NormalizationResult) -> NormalizationSummary:
    stats = result.stats
    return NormalizationSummary(
        capture_datagrams=stats.capture_datagrams,
        lap_packets=stats.lap_packets,
        telemetry_packets=stats.telemetry_packets,
        samples_joined=stats.samples_joined,
        ignored_other_packets=stats.ignored_other_packets,
        malformed_packet_headers=stats.malformed_packet_headers,
        unsupported_packet_formats=stats.unsupported_packet_formats,
        decode_errors=stats.decode_errors,
        duplicate_lap_packets=stats.duplicate_lap_packets,
        duplicate_telemetry_packets=stats.duplicate_telemetry_packets,
        session_time_mismatches=stats.session_time_mismatches,
        unmatched_lap_packets=stats.unmatched_lap_packets,
        unmatched_telemetry_packets=stats.unmatched_telemetry_packets,
    )


def prepare_session(
    path: str | Path,
    *,
    normalized: NormalizationResult | None = None,
) -> PreparedSession:
    """Build eligible final lap timelines from one capture."""
    if normalized is None:
        normalized = normalize_capture(path)
    if not normalized.samples:
        raise ValueError("capture has no normalized player samples")

    session_counts = Counter(sample.session_uid for sample in normalized.samples)
    session_uid = session_counts.most_common(1)[0][0]
    player_index = next(
        sample.player_car_index
        for sample in normalized.samples
        if sample.session_uid == session_uid
    )

    session_data: DecodedSessionData | None = None
    player_history = None
    for record in iter_capture(path):
        header = parse_header_prefix(record.payload)
        if header is None or header.packet_format != 2025:
            continue
        if header.packet_id == SESSION_PACKET_ID:
            decoded_session = decode_session_data(record.payload)
            if decoded_session.identity.session_uid == session_uid:
                session_data = decoded_session
        elif header.packet_id == SESSION_HISTORY_PACKET_ID:
            history = decode_session_history(record.payload)
            if (
                history.identity.session_uid == session_uid
                and history.car_index == player_index
            ):
                player_history = history

    if session_data is None:
        raise ValueError("capture has no Session packet for the player session")
    if player_history is None:
        raise ValueError("capture has no player Session History packet")

    validations = tuple(_validation(lap) for lap in player_history.laps)
    samples_by_lap: dict[int, list[NormalizedSample]] = defaultdict(list)
    for sample in normalized.samples:
        if sample.session_uid == session_uid:
            samples_by_lap[sample.lap_number].append(sample)

    traces = []
    for validation in validations:
        if not validation.eligible:
            continue
        traces.append(
            _resolve_trace(
                samples_by_lap[validation.lap_number],
                validation,
                session_data.track_length,
            )
        )

    return PreparedSession(
        session_uid=session_uid,
        track_length=session_data.track_length,
        track_id=session_data.track_id,
        sector2_distance=session_data.sector2_distance,
        sector3_distance=session_data.sector3_distance,
        validations=validations,
        laps=tuple(traces),
        normalization=_normalization_summary(normalized),
    )


def _grid(track_length: int, step: float) -> tuple[float, ...]:
    if step <= 0:
        raise ValueError("distance step must be greater than zero")
    values = [index * step for index in range(int(track_length / step) + 1)]
    if values[-1] < track_length:
        values.append(float(track_length))
    else:
        values[-1] = float(track_length)
    return tuple(values)


def resample_lap(
    lap: LapTrace,
    track_length: int,
    *,
    step: float = 1.0,
) -> ResampledLap:
    """Linearly interpolate a lap onto a common distance grid."""
    first = lap.samples[0]
    source = [
        _SourcePoint(
            distance=0.0,
            elapsed_time=0.0,
            speed=first.speed_kph,
            throttle=first.throttle,
            brake=first.brake,
            steering=first.steering,
            gear=first.gear,
        )
    ]
    source.extend(
        _SourcePoint(
            distance=sample.lap_distance,
            elapsed_time=sample.current_lap_time_ms / 1000,
            speed=sample.speed_kph,
            throttle=sample.throttle,
            brake=sample.brake,
            steering=sample.steering,
            gear=sample.gear,
        )
        for sample in lap.samples
        if 0 < sample.lap_distance < track_length
    )
    last = source[-1]
    source.append(
        _SourcePoint(
            distance=float(track_length),
            elapsed_time=lap.official_lap_time_ms / 1000,
            speed=last.speed,
            throttle=last.throttle,
            brake=last.brake,
            steering=last.steering,
            gear=last.gear,
        )
    )

    distances = _grid(track_length, step)
    elapsed = []
    speed = []
    throttle = []
    brake = []
    steering = []
    gear = []
    right = 1
    for distance in distances:
        while right < len(source) - 1 and source[right].distance < distance:
            right += 1
        left = right - 1
        x0, x1 = source[left].distance, source[right].distance
        ratio = 0.0 if x1 == x0 else (distance - x0) / (x1 - x0)

        left_point = source[left]
        right_point = source[right]
        elapsed.append(
            left_point.elapsed_time
            + ratio * (right_point.elapsed_time - left_point.elapsed_time)
        )
        speed.append(left_point.speed + ratio * (right_point.speed - left_point.speed))
        throttle.append(
            left_point.throttle + ratio * (right_point.throttle - left_point.throttle)
        )
        brake.append(left_point.brake + ratio * (right_point.brake - left_point.brake))
        steering.append(
            left_point.steering + ratio * (right_point.steering - left_point.steering)
        )
        nearest = left_point if ratio < 0.5 else right_point
        gear.append(nearest.gear)

    return ResampledLap(
        lap_number=lap.lap_number,
        official_lap_time_ms=lap.official_lap_time_ms,
        distance=distances,
        elapsed_s=tuple(elapsed),
        speed_kph=tuple(speed),
        throttle=tuple(throttle),
        brake=tuple(brake),
        steering=tuple(steering),
        gear=tuple(gear),
    )


def _loss_regions(
    reference: ResampledLap,
    comparison: ResampledLap,
    delta: tuple[float, ...],
    *,
    config: AnalysisConfig,
    event_matches: tuple[MatchedDrivingEvent, ...],
    reference_events: tuple[DrivingEvent, ...],
    comparison_events: tuple[DrivingEvent, ...],
    unmatched_reference_event_indices: tuple[int, ...],
    unmatched_comparison_event_indices: tuple[int, ...],
    sector2_distance: float,
    sector3_distance: float,
) -> tuple[LossRegion, ...]:
    step = reference.distance[1] - reference.distance[0]
    bin_samples = max(1, round(config.loss_bin_size_m / step))
    marked = []
    for start in range(0, len(delta) - 1, bin_samples):
        end = min(start + bin_samples, len(delta) - 1)
        if delta[end] - delta[start] >= config.minimum_bin_loss_s:
            marked.append((start, end))

    merged = []
    for start, end in marked:
        if merged and start <= merged[-1][1] + bin_samples:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))

    regions = []
    for start, end in merged:
        loss = delta[end] - delta[start]
        if loss < config.minimum_region_loss_s:
            continue
        window = slice(start, end + 1)
        reference_min_speed = min(reference.speed_kph[window])
        comparison_min_speed = min(comparison.speed_kph[window])
        reference_mean_throttle = fmean(reference.throttle[window])
        comparison_mean_throttle = fmean(comparison.throttle[window])
        reference_mean_brake = fmean(reference.brake[window])
        comparison_mean_brake = fmean(comparison.brake[window])
        speed_difference = comparison_min_speed - reference_min_speed
        throttle_difference = comparison_mean_throttle - reference_mean_throttle
        brake_difference = comparison_mean_brake - reference_mean_brake
        evidence = []
        if speed_difference < -config.minimum_speed_difference_kph:
            evidence.append("lower minimum speed")
        if throttle_difference < -config.minimum_throttle_difference:
            evidence.append("lower average throttle")
        if brake_difference > config.minimum_brake_difference:
            evidence.append("higher average brake")
        if not evidence:
            evidence.append("mixed trace difference")
        midpoint = (reference.distance[start] + reference.distance[end]) / 2
        if midpoint < sector2_distance:
            sector = 1
        elif midpoint < sector3_distance:
            sector = 2
        else:
            sector = 3
        regions.append(
            LossRegion(
                start_distance=reference.distance[start],
                end_distance=reference.distance[end],
                time_loss_s=loss,
                reference_min_speed_kph=reference_min_speed,
                comparison_min_speed_kph=comparison_min_speed,
                reference_mean_throttle=reference_mean_throttle,
                comparison_mean_throttle=comparison_mean_throttle,
                reference_mean_brake=reference_mean_brake,
                comparison_mean_brake=comparison_mean_brake,
                minimum_speed_difference_kph=speed_difference,
                mean_throttle_difference=throttle_difference,
                mean_brake_difference=brake_difference,
                evidence_labels=tuple(evidence),
                sector=sector,
                matched_event_index=matched_event_for_region(
                    reference.distance[start],
                    reference.distance[end],
                    event_matches,
                ),
                unmatched_reference_event_indices=unmatched_events_for_region(
                    reference.distance[start],
                    reference.distance[end],
                    reference_events,
                    unmatched_reference_event_indices,
                ),
                unmatched_comparison_event_indices=unmatched_events_for_region(
                    reference.distance[start],
                    reference.distance[end],
                    comparison_events,
                    unmatched_comparison_event_indices,
                ),
            )
        )
    return tuple(sorted(regions, key=lambda region: region.time_loss_s, reverse=True))


def compare_laps(
    reference: ResampledLap,
    comparison: ResampledLap,
    *,
    sector_deltas_s: tuple[float, float, float] = (0.0, 0.0, 0.0),
    reference_discarded_attempt_frames: int = 0,
    comparison_discarded_attempt_frames: int = 0,
    config: AnalysisConfig = DEFAULT_ANALYSIS_CONFIG,
    sector2_distance: float = float("inf"),
    sector3_distance: float = float("inf"),
) -> LapComparison:
    if reference.distance != comparison.distance:
        raise ValueError("laps must use the same distance grid")
    if reference.lap_number == comparison.lap_number:
        raise ValueError("reference and comparison laps must be different")
    delta = tuple(
        comparison_time - reference_time
        for reference_time, comparison_time in zip(
            reference.elapsed_s,
            comparison.elapsed_s,
        )
    )
    official_delta = (
        comparison.official_lap_time_ms - reference.official_lap_time_ms
    ) / 1000
    reference_events = detect_driving_events(reference, config)
    comparison_events = detect_driving_events(comparison, config)
    event_match_result = match_driving_events(
        reference_events,
        comparison_events,
        config,
    )
    return LapComparison(
        reference=reference,
        comparison=comparison,
        delta_s=delta,
        sector_deltas_s=sector_deltas_s,
        official_delta_s=official_delta,
        calculated_final_delta_s=delta[-1],
        final_delta_error_s=delta[-1] - official_delta,
        reference_discarded_attempt_frames=reference_discarded_attempt_frames,
        comparison_discarded_attempt_frames=comparison_discarded_attempt_frames,
        regions=_loss_regions(
            reference,
            comparison,
            delta,
            config=config,
            event_matches=event_match_result.matches,
            reference_events=reference_events,
            comparison_events=comparison_events,
            unmatched_reference_event_indices=(
                event_match_result.unmatched_reference_event_indices
            ),
            unmatched_comparison_event_indices=(
                event_match_result.unmatched_comparison_event_indices
            ),
            sector2_distance=sector2_distance,
            sector3_distance=sector3_distance,
        ),
        reference_events=reference_events,
        comparison_events=comparison_events,
        event_matches=event_match_result.matches,
        unmatched_reference_event_indices=(
            event_match_result.unmatched_reference_event_indices
        ),
        unmatched_comparison_event_indices=(
            event_match_result.unmatched_comparison_event_indices
        ),
    )


def _capture_metadata(path: Path) -> CaptureMetadata:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        header = read_capture_header(file)
        file.seek(0)
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return CaptureMetadata(
        basename=path.name,
        size_bytes=path.stat().st_size,
        sha256=digest.hexdigest(),
        created_unix_ns=header.created_unix_ns,
    )


def _selected_lap(
    session: PreparedSession,
    lap_number: int,
    role: str,
) -> LapTrace:
    trace = next(
        (lap for lap in session.laps if lap.lap_number == lap_number),
        None,
    )
    if trace is not None:
        return trace
    validation = next(
        (lap for lap in session.validations if lap.lap_number == lap_number),
        None,
    )
    if validation is None:
        raise ValueError(f"{role} lap {lap_number} is missing")
    raise ValueError(f"{role} lap {lap_number} is {validation.reason}")


def analyze_capture(
    path: str | Path,
    *,
    config: AnalysisConfig = DEFAULT_ANALYSIS_CONFIG,
    reference_lap: int | None = None,
    comparison_laps: Sequence[int] | None = None,
) -> AnalysisResult:
    """Return the complete deterministic analysis for one capture."""
    capture_path = Path(path)
    session = prepare_session(capture_path)
    if not session.laps:
        raise ValueError("capture has no eligible laps")

    if reference_lap is None:
        reference_trace = min(
            session.laps,
            key=lambda lap: (lap.official_lap_time_ms, lap.lap_number),
        )
    else:
        reference_trace = _selected_lap(session, reference_lap, "reference")

    if comparison_laps is None:
        selected_comparisons = tuple(
            lap
            for lap in sorted(session.laps, key=lambda item: item.lap_number)
            if lap.lap_number != reference_trace.lap_number
        )
    else:
        if len(set(comparison_laps)) != len(comparison_laps):
            raise ValueError("comparison laps cannot contain duplicates")
        selected_comparisons = tuple(
            _selected_lap(session, lap_number, "comparison")
            for lap_number in comparison_laps
        )

    if not selected_comparisons:
        raise ValueError("at least one comparison lap is required")
    if any(
        lap.lap_number == reference_trace.lap_number
        for lap in selected_comparisons
    ):
        raise ValueError("reference and comparison laps must be different")

    reference = resample_lap(
        reference_trace,
        session.track_length,
        step=config.distance_step_m,
    )
    comparisons = []
    for comparison_trace in selected_comparisons:
        comparison = resample_lap(
            comparison_trace,
            session.track_length,
            step=config.distance_step_m,
        )
        sector_deltas = tuple(
            (comparison_time - reference_time) / 1000
            for reference_time, comparison_time in zip(
                reference_trace.sector_times_ms,
                comparison_trace.sector_times_ms,
            )
        )
        comparison_result = compare_laps(
            reference,
            comparison,
            sector_deltas_s=sector_deltas,
            reference_discarded_attempt_frames=(
                reference_trace.discarded_attempt_frames
            ),
            comparison_discarded_attempt_frames=(
                comparison_trace.discarded_attempt_frames
            ),
            config=config,
            sector2_distance=session.sector2_distance,
            sector3_distance=session.sector3_distance,
        )
        if abs(comparison_result.final_delta_error_s) > 1e-9:
            raise ValueError(
                f"lap {comparison_trace.lap_number} finish delta differs from "
                "the official lap-time difference"
            )
        comparisons.append(comparison_result)

    return AnalysisResult(
        config=config,
        capture=_capture_metadata(capture_path),
        session=session,
        reference_lap=reference_trace.lap_number,
        comparisons=tuple(comparisons),
    )


def _format_lap_time(milliseconds: int) -> str:
    minutes, remainder = divmod(milliseconds, 60_000)
    seconds = remainder / 1000
    return f"{minutes}:{seconds:06.3f}"


def print_analysis(result: AnalysisResult) -> None:
    session = result.session
    print(
        f"Session {session.session_uid} | track_id={session.track_id} | "
        f"length={session.track_length} m"
    )
    print("Lap eligibility:")
    for lap in session.validations:
        time_text = (
            _format_lap_time(lap.official_lap_time_ms)
            if lap.official_lap_time_ms
            else "--"
        )
        print(
            f"  Lap {lap.lap_number}: {time_text}, flags={lap.validity_flags}, "
            f"{lap.reason}"
        )

    reference = result.comparisons[0].reference
    print(
        f"Reference: lap {reference.lap_number} "
        f"({_format_lap_time(reference.official_lap_time_ms)})"
    )
    print(
        "  Earlier-attempt frames removed: "
        f"{result.comparisons[0].reference_discarded_attempt_frames}"
    )

    for comparison in result.comparisons:
        lap = comparison.comparison
        print(
            f"Comparison lap {lap.lap_number} "
            f"({_format_lap_time(lap.official_lap_time_ms)}): "
            f"{comparison.official_delta_s:+.3f} s, "
            f"final-delta error={comparison.final_delta_error_s:+.6f} s"
        )
        print(
            "  Earlier-attempt frames removed: "
            f"{comparison.comparison_discarded_attempt_frames}"
        )
        for region in comparison.regions[:5]:
            print(
                f"    {region.start_distance:.0f}-{region.end_distance:.0f} m: "
                f"+{region.time_loss_s:.3f} s "
                f"({'; '.join(region.evidence_labels)})"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and compare completed laps from an F1CAP capture."
    )
    parser.add_argument("capture", type=Path)
    parser.add_argument(
        "--distance-step",
        type=float,
        default=1.0,
        help="resampling grid in metres (default: 1)",
    )
    parser.add_argument(
        "--reference-lap",
        type=int,
        help="eligible reference lap (default: fastest eligible lap)",
    )
    parser.add_argument(
        "--comparison-lap",
        type=int,
        action="append",
        dest="comparison_laps",
        help="eligible comparison lap; repeat to select multiple laps",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="new directory for the JSON, CSV, and SVG analysis bundle",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        config = AnalysisConfig(distance_step_m=args.distance_step)
        result = analyze_capture(
            args.capture,
            config=config,
            reference_lap=args.reference_lap,
            comparison_laps=args.comparison_laps,
        )
        print_analysis(result)
        if args.output_dir is not None:
            from f1_telemetry.analysis_output import write_analysis_bundle

            write_analysis_bundle(result, args.output_dir)
            print(f"Analysis bundle written: {args.output_dir}")
    except (OSError, ValueError) as error:
        raise SystemExit(f"Could not analyze {args.capture}: {error}") from error


if __name__ == "__main__":
    main()
