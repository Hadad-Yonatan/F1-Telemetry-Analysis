"""Small UDP probe for verifying that F1 25 telemetry reaches this computer."""

from __future__ import annotations

import argparse
import socket
import struct
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Sequence

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 20777
DEFAULT_REPORT_INTERVAL_SECONDS = 2.0
F1_25_PACKET_FORMAT = 2025

PACKET_NAMES = {
    0: "Motion",
    1: "Session",
    2: "Lap Data",
    3: "Event",
    4: "Participants",
    5: "Car Setups",
    6: "Car Telemetry",
    7: "Car Status",
    8: "Final Classification",
    9: "Lobby Info",
    10: "Car Damage",
    11: "Session History",
    12: "Tyre Sets",
    13: "Motion Ex",
    14: "Time Trial",
    15: "Lap Positions",
}

# The fields needed by the probe are the first seven bytes of every F1 25
# packet header. The full header is 29 bytes; decoding it belongs downstream.
HEADER_PREFIX = struct.Struct("<HBBBBB")


@dataclass(frozen=True)
class HeaderPrefix:
    """Fields from the common packet header needed to identify a datagram."""

    packet_format: int
    game_year: int
    game_major_version: int
    game_minor_version: int
    packet_version: int
    packet_id: int


@dataclass
class ProbeStats:
    """Aggregate observations without retaining telemetry payloads."""

    packet_counts: Counter[int] = field(default_factory=Counter)
    format_counts: Counter[tuple[int, int]] = field(default_factory=Counter)
    format_packet_counts: Counter[tuple[int, int]] = field(default_factory=Counter)
    malformed_datagrams: int = 0
    total_bytes: int = 0
    last_sender: tuple[str, int] | None = None

    def observe(self, data: bytes, address: tuple[str, int]) -> HeaderPrefix | None:
        """Record one datagram and return its header, if it is long enough."""
        self.total_bytes += len(data)
        self.last_sender = address
        header = parse_header_prefix(data)

        if header is None:
            self.malformed_datagrams += 1
            return None

        self.packet_counts[header.packet_id] += 1
        self.format_counts[(header.packet_format, header.game_year)] += 1
        self.format_packet_counts[(header.packet_format, header.packet_id)] += 1
        return header


def parse_header_prefix(data: bytes) -> HeaderPrefix | None:
    """Parse the common identifying fields, or return ``None`` if truncated."""
    if len(data) < HEADER_PREFIX.size:
        return None

    return HeaderPrefix(*HEADER_PREFIX.unpack_from(data))


def packet_count_summary(stats: ProbeStats) -> str:
    """Return a compact running summary of observed packet IDs."""
    if not stats.packet_counts:
        return "no complete packet headers"

    return ", ".join(
        f"{PACKET_NAMES.get(packet_id, f'Unknown {packet_id}')}: {count}"
        for packet_id, count in sorted(stats.packet_counts.items())
    )


def print_final_report(stats: ProbeStats) -> None:
    """Print counts and the milestone checks from the project handoff."""
    print("\nFinal probe report:")
    print(f"  Datagrams with complete headers: {sum(stats.packet_counts.values())}")
    print(f"  Truncated datagrams: {stats.malformed_datagrams}")
    print(f"  UDP payload bytes: {stats.total_bytes}")

    if stats.last_sender is not None:
        print(f"  Last sender: {stats.last_sender[0]}:{stats.last_sender[1]}")

    if stats.format_counts:
        print("  Formats:")
        for (packet_format, game_year), count in sorted(stats.format_counts.items()):
            print(f"    format={packet_format}, game_year={game_year}: {count}")

    print("  Packet counts:")
    if not stats.packet_counts:
        print("    No packets received.")
    else:
        for packet_id, count in sorted(stats.packet_counts.items()):
            name = PACKET_NAMES.get(packet_id, f"Unknown {packet_id}")
            print(f"    {name} ({packet_id}): {count}")

    saw_f1_25 = any(
        packet_format == F1_25_PACKET_FORMAT
        for packet_format, _game_year in stats.format_counts
    )
    print("  Milestone checks:")
    print(f"    [{'OK' if saw_f1_25 else 'MISSING'}] packet format 2025")
    for packet_id in (2, 6, 0):
        name = PACKET_NAMES[packet_id]
        status = (
            "OK"
            if stats.format_packet_counts[(F1_25_PACKET_FORMAT, packet_id)]
            else "MISSING"
        )
        qualifier = " (preferred)" if packet_id == 0 else ""
        print(f"    [{status}] {name}{qualifier}")


def run_probe(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    report_interval: float = DEFAULT_REPORT_INTERVAL_SECONDS,
) -> ProbeStats:
    """Listen until interrupted and return the observed packet statistics."""
    stats = ProbeStats()

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_socket:
        try:
            udp_socket.bind((host, port))
        except OSError as error:
            raise SystemExit(
                f"Could not listen on UDP {host}:{port}. "
                "Make sure Pits n' Giggles is completely closed.\n"
                f"Original error: {error}"
            ) from error

        print(f"Listening for F1 telemetry on UDP {host}:{port}...")
        print("Press Control+C to stop.\n")
        last_report = time.monotonic()

        try:
            while True:
                data, address = udp_socket.recvfrom(65535)
                header = stats.observe(data, address)
                now = time.monotonic()

                if now - last_report >= report_interval:
                    sender = address[0]
                    if header is None:
                        identity = "truncated datagram"
                    else:
                        identity = (
                            f"format={header.packet_format} | year={header.game_year}"
                        )
                    print(
                        f"From {sender} | {identity} | {packet_count_summary(stats)}",
                        flush=True,
                    )
                    last_report = now
        except KeyboardInterrupt:
            pass

    print_final_report(stats)
    return stats


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Verify that F1 25 UDP telemetry reaches this computer."
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"local interface to bind (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"UDP port configured in F1 25 (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--report-interval",
        type=float,
        default=DEFAULT_REPORT_INTERVAL_SECONDS,
        help="seconds between running summaries (default: 2)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Run the command-line probe."""
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    if args.report_interval <= 0:
        raise SystemExit("--report-interval must be greater than zero")

    run_probe(args.host, args.port, args.report_interval)


if __name__ == "__main__":
    main()
