"""Small UDP probe for verifying that F1 25 telemetry reaches this computer."""

from __future__ import annotations

import argparse
import socket
import struct
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Literal, Sequence

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 20777
DEFAULT_REPORT_INTERVAL_SECONDS = 2.0
DEFAULT_CONNECTION_TIMEOUT_SECONDS = 15.0
F1_25_PACKET_FORMAT = 2025
F1_25_GAME_YEAR = 25
REQUIRED_PACKET_IDS = (2, 6)

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
    contract_packet_counts: Counter[tuple[int, int, int]] = field(
        default_factory=Counter
    )
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
        self.contract_packet_counts[
            (header.packet_format, header.game_year, header.packet_id)
        ] += 1
        return header


@dataclass(frozen=True)
class BufferedDatagram:
    """One datagram retained while capture connectivity is established."""

    payload: bytes
    sender: tuple[str, int]
    captured_unix_ns: int


@dataclass(frozen=True)
class ConnectionTestResult:
    """Outcome and observations from a bounded telemetry connection test."""

    status: Literal["connected", "timed_out", "interrupted"]
    stats: ProbeStats
    buffered_datagrams: tuple[BufferedDatagram, ...] = ()

    @property
    def connected(self) -> bool:
        return self.status == "connected"


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


def connection_ready(stats: ProbeStats) -> bool:
    """Return whether the exact required F1 25 streams were observed."""
    return all(
        stats.contract_packet_counts[
            (F1_25_PACKET_FORMAT, F1_25_GAME_YEAR, packet_id)
        ]
        for packet_id in REQUIRED_PACKET_IDS
    )


def bind_udp_socket(host: str, port: int) -> socket.socket:
    """Create and bind a UDP socket with one actionable failure message."""
    udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        udp_socket.bind((host, port))
    except OSError as error:
        udp_socket.close()
        raise SystemExit(
            f"Could not listen on UDP {host}:{port}. Close other telemetry "
            f"receivers first.\nOriginal error: {error}"
        ) from error
    return udp_socket


def wait_for_connection(
    udp_socket: socket.socket,
    *,
    timeout: float,
    report_interval: float = DEFAULT_REPORT_INTERVAL_SECONDS,
    buffer_datagrams: bool = False,
    monotonic: Callable[[], float] = time.monotonic,
    unix_time_ns: Callable[[], int] = time.time_ns,
) -> ConnectionTestResult:
    """Wait for required F1 25 streams without losing preflight datagrams."""
    if timeout <= 0:
        raise ValueError("connection-test timeout must be greater than zero")
    if report_interval <= 0:
        raise ValueError("report interval must be greater than zero")

    stats = ProbeStats()
    buffered: list[BufferedDatagram] = []
    started = monotonic()
    deadline = started + timeout
    next_report = min(started + report_interval, deadline)
    previous_socket_timeout = udp_socket.gettimeout()

    try:
        while True:
            now = monotonic()
            if now >= deadline:
                return ConnectionTestResult(
                    status="timed_out",
                    stats=stats,
                    buffered_datagrams=tuple(buffered),
                )

            wait_until = min(deadline, next_report)
            udp_socket.settimeout(max(wait_until - now, 0.001))
            try:
                payload, sender = udp_socket.recvfrom(65_535)
            except socket.timeout:
                pass
            except KeyboardInterrupt:
                return ConnectionTestResult(
                    status="interrupted",
                    stats=stats,
                    buffered_datagrams=tuple(buffered),
                )
            else:
                captured_unix_ns = unix_time_ns()
                stats.observe(payload, sender)
                if buffer_datagrams:
                    buffered.append(
                        BufferedDatagram(
                            payload=payload,
                            sender=sender,
                            captured_unix_ns=captured_unix_ns,
                        )
                    )
                if connection_ready(stats):
                    return ConnectionTestResult(
                        status="connected",
                        stats=stats,
                        buffered_datagrams=tuple(buffered),
                    )

            now = monotonic()
            if now >= next_report and now < deadline:
                remaining = max(0.0, deadline - now)
                print(
                    f"Still waiting: {remaining:.0f}s remaining | "
                    f"{packet_count_summary(stats)}",
                    flush=True,
                )
                next_report = min(next_report + report_interval, deadline)
    finally:
        udp_socket.settimeout(previous_socket_timeout)


def print_connection_diagnostics(stats: ProbeStats, host: str, port: int) -> None:
    """Explain why a connection test failed and what to check next."""
    if stats.total_bytes == 0:
        print(f"\nConnection test failed: no UDP datagrams reached {host}:{port}.")
    elif not stats.packet_counts:
        print(
            "\nConnection test failed: UDP datagrams arrived, but none contained "
            "a complete F1 packet header."
        )
    elif not stats.format_counts[(F1_25_PACKET_FORMAT, F1_25_GAME_YEAR)]:
        observed = ", ".join(
            f"format={packet_format}, game_year={game_year}"
            for packet_format, game_year in sorted(stats.format_counts)
        )
        print(
            "\nConnection test failed: traffic arrived, but no exact F1 25 "
            f"packets were observed. Observed: {observed}."
        )
    else:
        missing = ", ".join(
            PACKET_NAMES[packet_id]
            for packet_id in REQUIRED_PACKET_IDS
            if not stats.contract_packet_counts[
                (F1_25_PACKET_FORMAT, F1_25_GAME_YEAR, packet_id)
            ]
        )
        print(
            "\nConnection test failed: F1 25 traffic arrived, but required "
            f"streams were missing: {missing}."
        )

    print("Check:")
    print("  1. UDP Telemetry is On in F1 25.")
    print("  2. The game's UDP IP Address is this Mac's current LAN IPv4 address.")
    print(f"  3. The game's UDP Port matches {port}.")
    print("  4. The Xbox and Mac are on the same non-guest network.")
    print("  5. macOS firewall or VPN software allows incoming UDP traffic.")
    print("  6. No other telemetry receiver is using the same UDP port.")
    print("  7. The car is on track and sending Lap Data and Car Telemetry.")


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

    saw_f1_25 = bool(
        stats.format_counts[(F1_25_PACKET_FORMAT, F1_25_GAME_YEAR)]
    )
    print("  Milestone checks:")
    print(f"    [{'OK' if saw_f1_25 else 'MISSING'}] packet format 2025")
    for packet_id in (2, 6, 0):
        name = PACKET_NAMES[packet_id]
        status = (
            "OK"
            if stats.contract_packet_counts[
                (F1_25_PACKET_FORMAT, F1_25_GAME_YEAR, packet_id)
            ]
            else "MISSING"
        )
        qualifier = " (preferred)" if packet_id == 0 else ""
        print(f"    [{status}] {name}{qualifier}")


def run_probe(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    report_interval: float = DEFAULT_REPORT_INTERVAL_SECONDS,
    timeout: float = DEFAULT_CONNECTION_TIMEOUT_SECONDS,
) -> ProbeStats:
    """Run a bounded connection test, or listen until interrupted at timeout zero."""
    with bind_udp_socket(host, port) as udp_socket:
        if timeout > 0:
            print(
                f"Testing F1 25 telemetry on UDP {host}:{port} for up to "
                f"{timeout:g} seconds..."
            )
            print("The car must already be on track. Press Control+C to cancel.\n")
            result = wait_for_connection(
                udp_socket,
                timeout=timeout,
                report_interval=report_interval,
            )
            stats = result.stats
            if result.connected:
                print("Connection verified: Lap Data and Car Telemetry received.")
            elif result.status == "interrupted":
                print("\nConnection test cancelled.")
        else:
            stats = ProbeStats()
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
                                f"format={header.packet_format} | "
                                f"year={header.game_year}"
                            )
                        print(
                            f"From {sender} | {identity} | "
                            f"{packet_count_summary(stats)}",
                            flush=True,
                        )
                        last_report = now
            except KeyboardInterrupt:
                pass

    print_final_report(stats)
    if timeout > 0 and not connection_ready(stats):
        print_connection_diagnostics(stats, host, port)
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
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_CONNECTION_TIMEOUT_SECONDS,
        help="connection-test timeout in seconds; 0 listens until stopped (default: 15)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Run the command-line probe."""
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    if args.report_interval <= 0:
        raise SystemExit("--report-interval must be greater than zero")
    if args.timeout < 0:
        raise SystemExit("--timeout cannot be negative")

    stats = run_probe(args.host, args.port, args.report_interval, args.timeout)
    if args.timeout > 0 and not connection_ready(stats):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
