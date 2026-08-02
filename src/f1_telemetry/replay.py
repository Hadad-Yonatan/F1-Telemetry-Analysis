"""Inspect an F1CAP file and optionally replay its exact payloads over UDP."""

from __future__ import annotations

import argparse
import socket
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from f1_telemetry.capture_format import CaptureFormatError, iter_capture
from f1_telemetry.probe import PACKET_NAMES, parse_header_prefix


@dataclass
class ReplaySummary:
    datagrams: int = 0
    payload_bytes: int = 0
    truncated_packet_headers: int = 0
    packet_counts: Counter[int] = field(default_factory=Counter)
    format_counts: Counter[tuple[int, int]] = field(default_factory=Counter)


def replay_capture(
    path: Path,
    *,
    destination: tuple[str, int] | None = None,
    realtime: bool = False,
    speed: float = 1.0,
    allow_truncated_tail: bool = False,
) -> ReplaySummary:
    """Verify all records and optionally send each original payload over UDP."""
    if speed <= 0:
        raise ValueError("speed must be greater than zero")

    summary = ReplaySummary()
    udp_socket = (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) if destination else None
    )
    first_capture_ns: int | None = None
    replay_started = time.monotonic()

    try:
        for record in iter_capture(path, allow_truncated_tail=allow_truncated_tail):
            if realtime:
                if first_capture_ns is None:
                    first_capture_ns = record.captured_unix_ns
                target_elapsed = (
                    (record.captured_unix_ns - first_capture_ns) / 1_000_000_000 / speed
                )
                sleep_seconds = target_elapsed - (time.monotonic() - replay_started)
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)

            if udp_socket is not None and destination is not None:
                sent = udp_socket.sendto(record.payload, destination)
                if sent != len(record.payload):
                    raise OSError(
                        f"short UDP send: expected {len(record.payload)}, sent {sent}"
                    )

            summary.datagrams += 1
            summary.payload_bytes += len(record.payload)
            header = parse_header_prefix(record.payload)
            if header is None:
                summary.truncated_packet_headers += 1
            else:
                summary.packet_counts[header.packet_id] += 1
                summary.format_counts[(header.packet_format, header.game_year)] += 1
    finally:
        if udp_socket is not None:
            udp_socket.close()

    return summary


def print_summary(summary: ReplaySummary, destination: tuple[str, int] | None) -> None:
    action = "Replayed" if destination is not None else "Verified"
    print(f"{action} datagrams: {summary.datagrams}")
    print(f"Payload bytes: {summary.payload_bytes}")
    print(f"Truncated packet headers preserved: {summary.truncated_packet_headers}")
    if destination is not None:
        print(f"UDP destination: {destination[0]}:{destination[1]}")

    print("Formats:")
    if not summary.format_counts:
        print("  None")
    for (packet_format, game_year), count in sorted(summary.format_counts.items()):
        print(f"  format={packet_format}, game_year={game_year}: {count}")

    print("Packet counts:")
    if not summary.packet_counts:
        print("  None")
    for packet_id, count in sorted(summary.packet_counts.items()):
        name = PACKET_NAMES.get(packet_id, f"Unknown {packet_id}")
        print(f"  {name} ({packet_id}): {count}")


def parse_destination(value: str) -> tuple[str, int]:
    try:
        host, raw_port = value.rsplit(":", 1)
        port = int(raw_port)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("destination must be HOST:PORT") from None
    if not host or not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("destination must be HOST:PORT")
    return host, port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify an F1CAP file and optionally replay payloads over UDP."
    )
    parser.add_argument("capture", type=Path)
    parser.add_argument(
        "--destination",
        type=parse_destination,
        help="send exact payloads to HOST:PORT; omit to inspect only",
    )
    parser.add_argument(
        "--realtime",
        action="store_true",
        help="preserve capture timing (default sends as fast as possible)",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="timing multiplier with --realtime (default: 1.0)",
    )
    parser.add_argument(
        "--allow-truncated-tail",
        action="store_true",
        help="recover complete records before an interrupted final record",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.speed <= 0:
        raise SystemExit("--speed must be greater than zero")

    try:
        summary = replay_capture(
            args.capture,
            destination=args.destination,
            realtime=args.realtime,
            speed=args.speed,
            allow_truncated_tail=args.allow_truncated_tail,
        )
    except (CaptureFormatError, OSError) as error:
        raise SystemExit(f"Could not replay {args.capture}: {error}") from error

    print_summary(summary, args.destination)


if __name__ == "__main__":
    main()
