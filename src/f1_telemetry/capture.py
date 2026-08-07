"""Losslessly record incoming F1 UDP datagrams before attempting decoding."""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from f1_telemetry.capture_format import CaptureWriter
from f1_telemetry.probe import (
    DEFAULT_CONNECTION_TIMEOUT_SECONDS,
    DEFAULT_HOST,
    DEFAULT_PORT,
    ProbeStats,
    bind_udp_socket,
    print_connection_diagnostics,
    print_final_report,
    wait_for_connection,
)


def default_capture_path() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("data/captures") / f"f1-telemetry-{timestamp}.f1cap"


def run_capture(
    output_path: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    sync_interval: float = 1.0,
    connection_timeout: float = DEFAULT_CONNECTION_TIMEOUT_SECONDS,
) -> ProbeStats:
    """Capture datagrams until Control+C, saving bytes before inspecting them."""
    if output_path.exists():
        raise FileExistsError(output_path)

    capture_started_unix_ns = time.time_ns()

    with bind_udp_socket(host, port) as udp_socket:
        if connection_timeout > 0:
            print(
                f"Testing F1 25 telemetry on UDP {host}:{port} for up to "
                f"{connection_timeout:g} seconds before recording..."
            )
            print("The car must already be on track. Press Control+C to cancel.\n")
            connection = wait_for_connection(
                udp_socket,
                timeout=connection_timeout,
                buffer_datagrams=True,
            )
            stats = connection.stats
            if not connection.connected:
                if connection.status == "interrupted":
                    print("\nConnection test cancelled; no capture was created.")
                    raise SystemExit(130)
                print_final_report(stats)
                print_connection_diagnostics(stats, host, port)
                print("No capture was created.")
                raise SystemExit(1)
            buffered_datagrams = connection.buffered_datagrams
            print("Connection verified: Lap Data and Car Telemetry received.")
        else:
            stats = ProbeStats()
            buffered_datagrams = ()

        with CaptureWriter(
            output_path,
            sync_interval=sync_interval,
            created_unix_ns=capture_started_unix_ns,
        ) as writer:
            for datagram in buffered_datagrams:
                writer.write_datagram(
                    datagram.payload,
                    datagram.sender,
                    captured_unix_ns=datagram.captured_unix_ns,
                )
            print(f"Recording UDP {host}:{port} to {output_path}...")
            print("Press Control+C to stop safely.\n")

            try:
                while True:
                    payload, sender = udp_socket.recvfrom(65_535)
                    # Preservation deliberately happens before header parsing.
                    writer.write_datagram(payload, sender)
                    stats.observe(payload, sender)
            except KeyboardInterrupt:
                pass

    print(f"\nCapture saved safely: {output_path}")
    print_final_report(stats)
    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Losslessly record F1 UDP datagrams to an F1CAP file."
    )
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=None,
        help="new .f1cap file (default: timestamped file under data/captures)",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--sync-interval",
        type=float,
        default=1.0,
        help="seconds between durable disk flushes (default: 1)",
    )
    parser.add_argument(
        "--connection-timeout",
        type=float,
        default=DEFAULT_CONNECTION_TIMEOUT_SECONDS,
        help=(
            "seconds to verify F1 25 telemetry before creating the capture; "
            "0 disables the test (default: 15)"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65_535:
        raise SystemExit("--port must be between 1 and 65535")
    if args.sync_interval < 0:
        raise SystemExit("--sync-interval cannot be negative")
    if args.connection_timeout < 0:
        raise SystemExit("--connection-timeout cannot be negative")

    output = default_capture_path() if args.output is None else args.output
    try:
        run_capture(
            output,
            host=args.host,
            port=args.port,
            sync_interval=args.sync_interval,
            connection_timeout=args.connection_timeout,
        )
    except FileExistsError:
        raise SystemExit(f"Refusing to overwrite existing capture: {output}") from None


if __name__ == "__main__":
    main()
