"""Join Lap Data and Car Telemetry packets into player-car sample rows."""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Sequence

from f1_telemetry.capture_format import (
    CaptureFormatError,
    CaptureRecord,
    iter_capture,
)
from f1_telemetry.decode import (
    CAR_TELEMETRY_PACKET_ID,
    F1_25_PACKET_FORMAT,
    LAP_DATA_PACKET_ID,
    DecodedCarTelemetry,
    DecodedLapData,
    FrameKey,
    PacketDecodeError,
    decode_car_telemetry,
    decode_lap_data,
)
from f1_telemetry.probe import parse_header_prefix


@dataclass(frozen=True)
class NormalizedSample:
    """Version 1 analysis row for the player car at one game frame."""

    captured_unix_ns: int
    session_uid: int
    frame_identifier: int
    overall_frame_identifier: int
    session_time: float
    player_car_index: int
    lap_number: int
    lap_distance: float
    total_distance: float
    current_lap_time_ms: int
    last_lap_time_ms: int
    sector: int
    speed_kph: int
    throttle: float
    brake: float
    steering: float
    clutch: int
    gear: int
    engine_rpm: int
    drs: bool
    lap_invalid: bool
    pit_status: int
    driver_status: int
    result_status: int


@dataclass
class NormalizationStats:
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
class NormalizationResult:
    samples: tuple[NormalizedSample, ...]
    stats: NormalizationStats


PendingLap = tuple[DecodedLapData, int]
PendingTelemetry = tuple[DecodedCarTelemetry, int]


class FrameNormalizer:
    """Order-independent frame joiner with explicit orphan diagnostics."""

    def __init__(self) -> None:
        self.stats = NormalizationStats()
        self.last_join_exact = False
        self._laps: dict[FrameKey, PendingLap] = {}
        self._telemetry: dict[FrameKey, PendingTelemetry] = {}

    def accept(self, record: CaptureRecord) -> NormalizedSample | None:
        self.stats.capture_datagrams += 1
        header = parse_header_prefix(record.payload)
        if header is None:
            self.stats.malformed_packet_headers += 1
            return None
        if header.packet_format != F1_25_PACKET_FORMAT:
            self.stats.unsupported_packet_formats += 1
            return None
        if header.packet_id not in (LAP_DATA_PACKET_ID, CAR_TELEMETRY_PACKET_ID):
            self.stats.ignored_other_packets += 1
            return None

        try:
            if header.packet_id == LAP_DATA_PACKET_ID:
                return self._accept_lap(record)
            return self._accept_telemetry(record)
        except PacketDecodeError:
            self.stats.decode_errors += 1
            return None

    def _accept_lap(self, record: CaptureRecord) -> NormalizedSample | None:
        lap = decode_lap_data(record.payload)
        self.stats.lap_packets += 1
        key = lap.identity.frame_key
        if key in self._laps:
            self.stats.duplicate_lap_packets += 1
            return None

        telemetry_entry = self._telemetry.pop(key, None)
        if telemetry_entry is None:
            self._laps[key] = (lap, record.captured_unix_ns)
            return None

        telemetry, telemetry_capture_ns = telemetry_entry
        return self._join(
            lap,
            record.captured_unix_ns,
            telemetry,
            telemetry_capture_ns,
        )

    def _accept_telemetry(self, record: CaptureRecord) -> NormalizedSample | None:
        telemetry = decode_car_telemetry(record.payload)
        self.stats.telemetry_packets += 1
        key = telemetry.identity.frame_key
        if key in self._telemetry:
            self.stats.duplicate_telemetry_packets += 1
            return None

        lap_entry = self._laps.pop(key, None)
        if lap_entry is None:
            self._telemetry[key] = (telemetry, record.captured_unix_ns)
            return None

        lap, lap_capture_ns = lap_entry
        return self._join(
            lap,
            lap_capture_ns,
            telemetry,
            record.captured_unix_ns,
        )

    def _join(
        self,
        lap: DecodedLapData,
        lap_capture_ns: int,
        telemetry: DecodedCarTelemetry,
        telemetry_capture_ns: int,
    ) -> NormalizedSample:
        self.last_join_exact = lap.identity.session_time == telemetry.identity.session_time
        if abs(lap.identity.session_time - telemetry.identity.session_time) > 1e-6:
            self.stats.session_time_mismatches += 1

        self.stats.samples_joined += 1
        identity = lap.identity
        return NormalizedSample(
            captured_unix_ns=max(lap_capture_ns, telemetry_capture_ns),
            session_uid=identity.session_uid,
            frame_identifier=identity.frame_identifier,
            overall_frame_identifier=identity.overall_frame_identifier,
            session_time=identity.session_time,
            player_car_index=identity.player_car_index,
            lap_number=lap.lap_number,
            lap_distance=lap.lap_distance,
            total_distance=lap.total_distance,
            current_lap_time_ms=lap.current_lap_time_ms,
            last_lap_time_ms=lap.last_lap_time_ms,
            sector=lap.sector,
            speed_kph=telemetry.speed_kph,
            throttle=telemetry.throttle,
            brake=telemetry.brake,
            steering=telemetry.steering,
            clutch=telemetry.clutch,
            gear=telemetry.gear,
            engine_rpm=telemetry.engine_rpm,
            drs=telemetry.drs,
            lap_invalid=lap.lap_invalid,
            pit_status=lap.pit_status,
            driver_status=lap.driver_status,
            result_status=lap.result_status,
        )

    def finish(self) -> None:
        self.stats.unmatched_lap_packets = len(self._laps)
        self.stats.unmatched_telemetry_packets = len(self._telemetry)


def normalize_capture(
    path: str | Path,
    *,
    allow_truncated_tail: bool = False,
) -> NormalizationResult:
    """Decode and join all supported player-car frames in a capture."""
    normalizer = FrameNormalizer()
    samples: list[NormalizedSample] = []
    for record in iter_capture(path, allow_truncated_tail=allow_truncated_tail):
        sample = normalizer.accept(record)
        if sample is not None:
            samples.append(sample)
    normalizer.finish()
    samples.sort(key=lambda sample: (sample.captured_unix_ns, sample.session_uid))
    return NormalizationResult(tuple(samples), normalizer.stats)


def write_normalized_csv(result: NormalizationResult, output: Path) -> None:
    """Write the v1 contract without silently overwriting an existing export."""
    output.parent.mkdir(parents=True, exist_ok=True)
    field_names = [field.name for field in fields(NormalizedSample)]
    with output.open("x", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=field_names)
        writer.writeheader()
        writer.writerows(asdict(sample) for sample in result.samples)


def print_summary(result: NormalizationResult) -> None:
    stats = result.stats
    print(f"Capture datagrams: {stats.capture_datagrams}")
    print(f"Lap Data packets decoded: {stats.lap_packets}")
    print(f"Car Telemetry packets decoded: {stats.telemetry_packets}")
    print(f"Normalized samples: {stats.samples_joined}")
    print(f"Unmatched Lap Data packets: {stats.unmatched_lap_packets}")
    print(f"Unmatched Car Telemetry packets: {stats.unmatched_telemetry_packets}")
    print(f"Decode errors: {stats.decode_errors}")

    if not result.samples:
        return

    sessions = Counter(sample.session_uid for sample in result.samples)
    laps = Counter(sample.lap_number for sample in result.samples)
    invalid_samples = sum(sample.lap_invalid for sample in result.samples)
    print(f"Sessions: {dict(sessions)}")
    print(f"Samples by current lap: {dict(sorted(laps.items()))}")
    print(f"Samples marked lap-invalid: {invalid_samples}")
    print(
        "Frame range: "
        f"{result.samples[0].overall_frame_identifier}.."
        f"{result.samples[-1].overall_frame_identifier}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Join F1CAP Lap Data and Car Telemetry into normalized rows."
    )
    parser.add_argument("capture", type=Path)
    parser.add_argument("--output", type=Path, help="optional new CSV output path")
    parser.add_argument(
        "--allow-truncated-tail",
        action="store_true",
        help="recover complete capture records before an interrupted final record",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        result = normalize_capture(
            args.capture,
            allow_truncated_tail=args.allow_truncated_tail,
        )
        print_summary(result)
        if args.output is not None:
            write_normalized_csv(result, args.output)
            print(f"Normalized CSV written: {args.output}")
    except (CaptureFormatError, OSError) as error:
        raise SystemExit(f"Could not normalize {args.capture}: {error}") from error


if __name__ == "__main__":
    main()
