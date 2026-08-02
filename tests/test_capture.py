import struct
import tempfile
import unittest
from pathlib import Path

from f1_telemetry.capture_format import (
    CAPTURE_MAGIC,
    FILE_HEADER,
    CaptureFormatError,
    CaptureWriter,
    TruncatedCaptureError,
    UnsupportedCaptureVersionError,
    iter_capture,
    read_capture_header,
)
from f1_telemetry.replay import replay_capture


def make_packet(
    packet_id: int,
    *,
    packet_format: int = 2025,
    suffix: bytes = b"",
) -> bytes:
    return struct.pack("<HBBBBB", packet_format, 25, 1, 0, 1, packet_id) + suffix


class CaptureFormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "test.f1cap"

    def test_round_trip_preserves_every_payload_byte_and_metadata(self) -> None:
        payloads = [
            b"",
            bytes(range(256)) * 4,
            make_packet(6, suffix=b"telemetry\x00\xff"),
        ]
        with CaptureWriter(self.path, created_unix_ns=100) as writer:
            for index, payload in enumerate(payloads):
                writer.write_datagram(
                    payload,
                    ("192.0.2.10", 40_000 + index),
                    captured_unix_ns=1_000 + index,
                )

        records = list(iter_capture(self.path))
        self.assertEqual([record.payload for record in records], payloads)
        self.assertEqual(
            [record.captured_unix_ns for record in records], [1_000, 1_001, 1_002]
        )
        self.assertEqual([record.sender_ip for record in records], ["192.0.2.10"] * 3)
        self.assertEqual(
            [record.sender_port for record in records], [40_000, 40_001, 40_002]
        )

        with self.path.open("rb") as file:
            header = read_capture_header(file)
        self.assertEqual(header.version, 1)
        self.assertEqual(header.created_unix_ns, 100)

    def test_short_unknown_and_unsupported_packets_are_preserved(self) -> None:
        payloads = [b"short", make_packet(99), make_packet(2, packet_format=2024)]
        with CaptureWriter(self.path) as writer:
            for payload in payloads:
                writer.write_datagram(payload, ("198.51.100.4", 20_777))

        self.assertEqual(
            [record.payload for record in iter_capture(self.path)], payloads
        )

    def test_context_manager_closes_cleanly_during_keyboard_interrupt(self) -> None:
        try:
            with CaptureWriter(self.path) as writer:
                writer.write_datagram(b"preserved", ("203.0.113.8", 20_777))
                raise KeyboardInterrupt
        except KeyboardInterrupt:
            pass

        self.assertEqual(
            [record.payload for record in iter_capture(self.path)], [b"preserved"]
        )

    def test_refuses_to_overwrite_an_existing_capture(self) -> None:
        with CaptureWriter(self.path):
            pass

        with self.assertRaises(FileExistsError):
            CaptureWriter(self.path)

    def test_truncated_final_record_does_not_hide_complete_records(self) -> None:
        with CaptureWriter(self.path) as writer:
            writer.write_datagram(b"complete", ("192.0.2.1", 1))
            writer.write_datagram(b"will be truncated", ("192.0.2.1", 2))

        self.path.write_bytes(self.path.read_bytes()[:-3])
        iterator = iter_capture(self.path)
        self.assertEqual(next(iterator).payload, b"complete")
        with self.assertRaises(TruncatedCaptureError):
            next(iterator)

        recovered = list(iter_capture(self.path, allow_truncated_tail=True))
        self.assertEqual([record.payload for record in recovered], [b"complete"])

    def test_checksum_detects_payload_corruption(self) -> None:
        with CaptureWriter(self.path) as writer:
            writer.write_datagram(b"original", ("192.0.2.1", 20_777))

        damaged = bytearray(self.path.read_bytes())
        damaged[-1] ^= 0xFF
        self.path.write_bytes(damaged)

        with self.assertRaisesRegex(CaptureFormatError, "checksum mismatch"):
            list(iter_capture(self.path))

    def test_rejects_unsupported_capture_version(self) -> None:
        self.path.write_bytes(
            FILE_HEADER.pack(CAPTURE_MAGIC, 99, FILE_HEADER.size, 123)
        )

        with self.assertRaises(UnsupportedCaptureVersionError):
            list(iter_capture(self.path))


class ReplayTests(unittest.TestCase):
    def test_offline_replay_counts_packets_without_requiring_decoding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "replay.f1cap"
            payloads = [
                make_packet(2),
                make_packet(6),
                make_packet(99),
                make_packet(2, packet_format=2024),
                b"tiny",
            ]
            with CaptureWriter(path) as writer:
                for payload in payloads:
                    writer.write_datagram(payload, ("192.0.2.10", 20_777))

            summary = replay_capture(path)

        self.assertEqual(summary.datagrams, 5)
        self.assertEqual(summary.payload_bytes, sum(map(len, payloads)))
        self.assertEqual(summary.truncated_packet_headers, 1)
        self.assertEqual(summary.packet_counts, {2: 2, 6: 1, 99: 1})
        self.assertEqual(summary.format_counts, {(2025, 25): 3, (2024, 25): 1})


if __name__ == "__main__":
    unittest.main()

