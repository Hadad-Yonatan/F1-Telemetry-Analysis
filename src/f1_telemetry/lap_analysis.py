"""Validate, reconstruct, align, and compare completed F1 laps."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean
from typing import Sequence

from f1_telemetry.capture_format import iter_capture
from f1_telemetry.decode import (
    SESSION_HISTORY_PACKET_ID,
    SESSION_PACKET_ID,
    DecodedLapHistory,
    DecodedSessionData,
    decode_session_data,
    decode_session_history,
)
from f1_telemetry.normalize import NormalizedSample, normalize_capture
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


@dataclass(frozen=True)
class PreparedSession:
    session_uid: int
    track_length: int
    track_id: int
    sector2_distance: float
    sector3_distance: float
    validations: tuple[LapValidation, ...]
    laps: tuple[LapTrace, ...]


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


@dataclass(frozen=True)
class LapComparison:
    reference_lap: int
    comparison_lap: int
    official_delta_s: float
    calculated_final_delta_s: float
    final_delta_error_s: float
    regions: tuple[LossRegion, ...]


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
    )


def prepare_session(path: str | Path) -> PreparedSession:
    """Build eligible final lap timelines from one capture."""
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
        (
            0.0,
            0.0,
            first.speed_kph,
            first.throttle,
            first.brake,
            first.steering,
            first.gear,
        )
    ]
    source.extend(
        (
            sample.lap_distance,
            sample.current_lap_time_ms / 1000,
            sample.speed_kph,
            sample.throttle,
            sample.brake,
            sample.steering,
            sample.gear,
        )
        for sample in lap.samples
        if 0 < sample.lap_distance < track_length
    )
    last = source[-1]
    source.append(
        (
            float(track_length),
            lap.official_lap_time_ms / 1000,
            last[2],
            last[3],
            last[4],
            last[5],
            last[6],
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
        while right < len(source) - 1 and source[right][0] < distance:
            right += 1
        left = right - 1
        x0, x1 = source[left][0], source[right][0]
        ratio = 0.0 if x1 == x0 else (distance - x0) / (x1 - x0)

        def interpolate(column: int) -> float:
            return source[left][column] + ratio * (
                source[right][column] - source[left][column]
            )

        elapsed.append(interpolate(1))
        speed.append(interpolate(2))
        throttle.append(interpolate(3))
        brake.append(interpolate(4))
        steering.append(interpolate(5))
        gear.append(int(source[left if ratio < 0.5 else right][6]))

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
    bin_metres: int = 25,
    min_bin_loss_s: float = 0.003,
    min_region_loss_s: float = 0.02,
) -> tuple[LossRegion, ...]:
    step = reference.distance[1] - reference.distance[0]
    bin_samples = max(1, round(bin_metres / step))
    marked = []
    for start in range(0, len(delta) - 1, bin_samples):
        end = min(start + bin_samples, len(delta) - 1)
        if delta[end] - delta[start] >= min_bin_loss_s:
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
        if loss < min_region_loss_s:
            continue
        window = slice(start, end + 1)
        regions.append(
            LossRegion(
                start_distance=reference.distance[start],
                end_distance=reference.distance[end],
                time_loss_s=loss,
                reference_min_speed_kph=min(reference.speed_kph[window]),
                comparison_min_speed_kph=min(comparison.speed_kph[window]),
                reference_mean_throttle=fmean(reference.throttle[window]),
                comparison_mean_throttle=fmean(comparison.throttle[window]),
                reference_mean_brake=fmean(reference.brake[window]),
                comparison_mean_brake=fmean(comparison.brake[window]),
            )
        )
    return tuple(sorted(regions, key=lambda region: region.time_loss_s, reverse=True))


def compare_laps(reference: ResampledLap, comparison: ResampledLap) -> LapComparison:
    if reference.distance != comparison.distance:
        raise ValueError("laps must use the same distance grid")
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
    return LapComparison(
        reference_lap=reference.lap_number,
        comparison_lap=comparison.lap_number,
        official_delta_s=official_delta,
        calculated_final_delta_s=delta[-1],
        final_delta_error_s=delta[-1] - official_delta,
        regions=_loss_regions(reference, comparison, delta),
    )


def _format_lap_time(milliseconds: int) -> str:
    minutes, remainder = divmod(milliseconds, 60_000)
    seconds = remainder / 1000
    return f"{minutes}:{seconds:06.3f}"


def print_analysis(session: PreparedSession, *, step: float = 1.0) -> None:
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

    if len(session.laps) < 2:
        print("Fewer than two eligible laps; comparison is not possible.")
        return

    fastest = min(session.laps, key=lambda lap: lap.official_lap_time_ms)
    reference = resample_lap(fastest, session.track_length, step=step)
    print(
        f"Reference: lap {fastest.lap_number} "
        f"({_format_lap_time(fastest.official_lap_time_ms)})"
    )
    print(f"  Earlier-attempt frames removed: {fastest.discarded_attempt_frames}")

    for lap in session.laps:
        if lap.lap_number == fastest.lap_number:
            continue
        comparison_lap = resample_lap(lap, session.track_length, step=step)
        comparison = compare_laps(reference, comparison_lap)
        print(
            f"Comparison lap {lap.lap_number} "
            f"({_format_lap_time(lap.official_lap_time_ms)}): "
            f"+{comparison.official_delta_s:.3f} s, "
            f"final-delta error={comparison.final_delta_error_s:+.6f} s"
        )
        print(f"  Earlier-attempt frames removed: {lap.discarded_attempt_frames}")
        for region in comparison.regions[:5]:
            evidence = []
            speed_difference = (
                region.comparison_min_speed_kph - region.reference_min_speed_kph
            )
            throttle_difference = (
                region.comparison_mean_throttle - region.reference_mean_throttle
            )
            brake_difference = (
                region.comparison_mean_brake - region.reference_mean_brake
            )
            if speed_difference < -2:
                evidence.append(f"minimum speed {abs(speed_difference):.1f} km/h lower")
            if throttle_difference < -0.05:
                evidence.append("less average throttle")
            if brake_difference > 0.05:
                evidence.append("more average brake")
            evidence_text = (
                "; ".join(evidence) if evidence else "trace difference is mixed"
            )
            print(
                f"    {region.start_distance:.0f}-{region.end_distance:.0f} m: "
                f"+{region.time_loss_s:.3f} s ({evidence_text})"
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
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        session = prepare_session(args.capture)
        print_analysis(session, step=args.distance_step)
    except (OSError, ValueError) as error:
        raise SystemExit(f"Could not analyze {args.capture}: {error}") from error


if __name__ == "__main__":
    main()
