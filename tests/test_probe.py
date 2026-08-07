import io
import socket
import struct
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from f1_telemetry.probe import (
    ProbeStats,
    bind_udp_socket,
    connection_ready,
    main,
    packet_count_summary,
    parse_header_prefix,
    print_connection_diagnostics,
    print_final_report,
    wait_for_connection,
)


def make_datagram(
    packet_id: int,
    packet_format: int = 2025,
    game_year: int = 25,
) -> bytes:
    header_prefix = struct.pack(
        "<HBBBBB", packet_format, game_year, 1, 0, 1, packet_id
    )
    return header_prefix + bytes(22) + b"payload"


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeSocket:
    def __init__(self, actions: list[object], clock: FakeClock | None = None) -> None:
        self.actions = list(actions)
        self.clock = clock
        self.timeout: float | None = None

    def gettimeout(self) -> float | None:
        return self.timeout

    def settimeout(self, value: float | None) -> None:
        self.timeout = value

    def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
        if not self.actions:
            if self.clock is not None:
                assert self.timeout is not None
                self.clock.advance(self.timeout)
            raise socket.timeout
        action = self.actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        assert isinstance(action, tuple)
        return action


class ParseHeaderPrefixTests(unittest.TestCase):
    def test_parses_f1_25_identifying_fields(self) -> None:
        header = parse_header_prefix(make_datagram(packet_id=6))

        self.assertIsNotNone(header)
        assert header is not None
        self.assertEqual(header.packet_format, 2025)
        self.assertEqual(header.game_year, 25)
        self.assertEqual(header.packet_version, 1)
        self.assertEqual(header.packet_id, 6)

    def test_rejects_truncated_header(self) -> None:
        self.assertIsNone(parse_header_prefix(bytes(6)))


class ProbeStatsTests(unittest.TestCase):
    def test_counts_packet_types_formats_bytes_and_truncation(self) -> None:
        stats = ProbeStats()
        stats.observe(make_datagram(2), ("192.0.2.10", 50000))
        stats.observe(make_datagram(6), ("192.0.2.10", 50000))
        stats.observe(make_datagram(6), ("192.0.2.10", 50000))
        stats.observe(b"short", ("192.0.2.10", 50000))

        self.assertEqual(stats.packet_counts, {2: 1, 6: 2})
        self.assertEqual(stats.format_counts, {(2025, 25): 3})
        self.assertEqual(stats.format_packet_counts, {(2025, 2): 1, (2025, 6): 2})
        self.assertEqual(stats.malformed_datagrams, 1)
        self.assertEqual(stats.last_sender, ("192.0.2.10", 50000))
        self.assertGreater(stats.total_bytes, 0)

    def test_summary_names_known_and_unknown_packet_ids(self) -> None:
        stats = ProbeStats()
        stats.observe(make_datagram(2), ("192.0.2.10", 50000))
        stats.observe(make_datagram(15), ("192.0.2.10", 50000))
        stats.observe(make_datagram(99), ("192.0.2.10", 50000))

        self.assertEqual(
            packet_count_summary(stats),
            "Lap Data: 1, Lap Positions: 1, Unknown 99: 1",
        )

    def test_final_report_exposes_milestone_status(self) -> None:
        stats = ProbeStats()
        stats.observe(make_datagram(2), ("192.0.2.10", 50000))
        stats.observe(make_datagram(6), ("192.0.2.10", 50000))
        stats.observe(
            make_datagram(0, packet_format=2024), ("192.0.2.10", 50000)
        )

        output = io.StringIO()
        with redirect_stdout(output):
            print_final_report(stats)

        report = output.getvalue()
        self.assertIn("[OK] packet format 2025", report)
        self.assertIn("[OK] Lap Data", report)
        self.assertIn("[OK] Car Telemetry", report)
        self.assertIn("[MISSING] Motion (preferred)", report)

    def test_connection_requires_exact_f1_25_lap_and_telemetry_packets(self) -> None:
        stats = ProbeStats()
        stats.observe(make_datagram(2), ("192.0.2.10", 50000))
        stats.observe(
            make_datagram(6, game_year=24), ("192.0.2.10", 50000)
        )
        self.assertFalse(connection_ready(stats))

        stats.observe(make_datagram(6), ("192.0.2.10", 50000))
        self.assertTrue(connection_ready(stats))


class ConnectionTestTests(unittest.TestCase):
    def test_succeeds_early_and_buffers_every_received_datagram(self) -> None:
        sender = ("192.0.2.10", 50000)
        udp_socket = FakeSocket(
            [
                (b"short", sender),
                (make_datagram(2), sender),
                (make_datagram(6), sender),
            ]
        )
        timestamps = iter((100, 200, 300))

        result = wait_for_connection(
            udp_socket,  # type: ignore[arg-type]
            timeout=15,
            buffer_datagrams=True,
            unix_time_ns=lambda: next(timestamps),
        )

        self.assertTrue(result.connected)
        self.assertEqual(result.status, "connected")
        self.assertEqual(
            [datagram.payload for datagram in result.buffered_datagrams],
            [b"short", make_datagram(2), make_datagram(6)],
        )
        self.assertEqual(
            [datagram.captured_unix_ns for datagram in result.buffered_datagrams],
            [100, 200, 300],
        )
        self.assertIsNone(udp_socket.timeout)

    def test_times_out_with_visible_progress_when_no_data_arrives(self) -> None:
        clock = FakeClock()
        udp_socket = FakeSocket([], clock)
        output = io.StringIO()

        with redirect_stdout(output):
            result = wait_for_connection(
                udp_socket,  # type: ignore[arg-type]
                timeout=5,
                report_interval=2,
                monotonic=clock,
            )

        self.assertEqual(result.status, "timed_out")
        self.assertEqual(result.stats.total_bytes, 0)
        self.assertIn("Still waiting: 3s remaining", output.getvalue())
        self.assertIn("Still waiting: 1s remaining", output.getvalue())

    def test_interruption_returns_observations_without_claiming_success(self) -> None:
        sender = ("192.0.2.10", 50000)
        udp_socket = FakeSocket([(make_datagram(2), sender), KeyboardInterrupt()])

        result = wait_for_connection(
            udp_socket,  # type: ignore[arg-type]
            timeout=15,
        )

        self.assertEqual(result.status, "interrupted")
        self.assertFalse(connection_ready(result.stats))

    def test_diagnostics_distinguish_silence_wrong_contract_and_missing_stream(self) -> None:
        cases = []

        silent = ProbeStats()
        cases.append((silent, "no UDP datagrams"))

        malformed = ProbeStats()
        malformed.observe(b"short", ("192.0.2.10", 50000))
        cases.append((malformed, "complete F1 packet header"))

        wrong_contract = ProbeStats()
        wrong_contract.observe(
            make_datagram(2, packet_format=2024), ("192.0.2.10", 50000)
        )
        cases.append((wrong_contract, "no exact F1 25 packets"))

        incomplete = ProbeStats()
        incomplete.observe(make_datagram(2), ("192.0.2.10", 50000))
        cases.append((incomplete, "required streams were missing: Car Telemetry"))

        for stats, message in cases:
            with self.subTest(message=message):
                output = io.StringIO()
                with redirect_stdout(output):
                    print_connection_diagnostics(stats, "0.0.0.0", 20777)
                self.assertIn(message, output.getvalue())
                self.assertIn("UDP Telemetry is On", output.getvalue())

    def test_bind_failure_closes_socket_and_reports_competing_receiver(self) -> None:
        class FailingSocket:
            closed = False

            def bind(self, _address: tuple[str, int]) -> None:
                raise OSError("address in use")

            def close(self) -> None:
                self.closed = True

        udp_socket = FailingSocket()
        with patch("f1_telemetry.probe.socket.socket", return_value=udp_socket):
            with self.assertRaisesRegex(SystemExit, "Close other telemetry receivers"):
                bind_udp_socket("0.0.0.0", 20777)
        self.assertTrue(udp_socket.closed)

    def test_probe_main_uses_failure_exit_only_for_bounded_mode(self) -> None:
        with patch("f1_telemetry.probe.run_probe", return_value=ProbeStats()):
            with self.assertRaises(SystemExit) as raised:
                main(["--timeout", "1"])
            self.assertEqual(raised.exception.code, 1)

            main(["--timeout", "0"])


if __name__ == "__main__":
    unittest.main()
