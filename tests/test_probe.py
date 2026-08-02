import io
import struct
import unittest
from contextlib import redirect_stdout

from f1_telemetry.probe import (
    ProbeStats,
    packet_count_summary,
    parse_header_prefix,
    print_final_report,
)


def make_datagram(packet_id: int, packet_format: int = 2025) -> bytes:
    header_prefix = struct.pack("<HBBBBB", packet_format, 25, 1, 0, 1, packet_id)
    return header_prefix + bytes(22) + b"payload"


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


if __name__ == "__main__":
    unittest.main()
