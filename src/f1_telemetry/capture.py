"""Losslessly record incoming F1 UDP datagrams before attempting decoding."""

from __future__ import annotations

import argparse
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from f1_telemetry.capture_format import CaptureWriter
from f1_telemetry.probe import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    ProbeStats,
    print_final_report,
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
) -> ProbeStats:
    """Capture datagrams until Control+C, saving bytes before inspecting them."""
    stats = ProbeStats()

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_socket:
        try:
            udp_socket.bind((host, port))
        except OSError as error:
            raise SystemExit(
                f"Could not listen on UDP {host}:{port}. Close other telemetry "
                f"receivers first.\nOriginal error: {error}"
            ) from error

        with CaptureWriter(output_path, sync_interval=sync_interval) as writer:
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
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65_535:
        raise SystemExit("--port must be between 1 and 65535")
    if args.sync_interval < 0:
        raise SystemExit("--sync-interval cannot be negative")

    output = default_capture_path() if args.output is None else args.output
    try:
        run_capture(
            output,
            host=args.host,
            port=args.port,
            sync_interval=args.sync_interval,
        )
    except FileExistsError:
        raise SystemExit(f"Refusing to overwrite existing capture: {output}") from None


if __name__ == "__main__":
    main()

