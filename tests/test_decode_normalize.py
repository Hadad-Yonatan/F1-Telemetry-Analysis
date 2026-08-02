import csv
import tempfile
import unittest
from pathlib import Path

from f1.packets import PacketCarTelemetryData, PacketLapData

from f1_telemetry.capture_format import CaptureRecord, CaptureWriter
from f1_telemetry.decode import (
    PacketDecodeError,
    decode_car_telemetry,
    decode_lap_data,
)
from f1_telemetry.normalize import (
    FrameNormalizer,
    normalize_capture,
    write_normalized_csv,
)


def populate_header(
    header: object,
    packet_id: int,
    *,
    session_uid: int = 123,
    frame_identifier: int = 10,
    overall_frame_identifier: int = 20,
    player_car_index: int = 0,
    packet_format: int = 2025,
) -> None:
    header.packet_format = packet_format
    header.game_year = 25
    header.game_major_version = 1
    header.game_minor_version = 24
    header.packet_version = 1
    header.packet_id = packet_id
    header.session_uid = session_uid
    header.session_time = 12.5
    header.frame_identifier = frame_identifier
    header.overall_frame_identifier = overall_frame_identifier
    header.player_car_index = player_car_index
    header.secondary_player_car_index = 255


def make_lap_packet(
    *,
    frame_identifier: int = 10,
    overall_frame_identifier: int = 20,
    player_car_index: int = 0,
    packet_format: int = 2025,
) -> bytes:
    packet = PacketLapData()
    populate_header(
        packet.header,
        2,
        frame_identifier=frame_identifier,
        overall_frame_identifier=overall_frame_identifier,
        player_car_index=player_car_index,
        packet_format=packet_format,
    )
    if player_car_index < 22:
        lap = packet.lap_data[player_car_index]
        lap.last_lap_time_in_ms = 91_234
        lap.current_lap_time_in_ms = 12_345
        lap.lap_distance = -4.5
        lap.total_distance = 6_995.5
        lap.current_lap_num = 3
        lap.sector = 1
        lap.current_lap_invalid = 1
        lap.pit_status = 0
        lap.driver_status = 4
        lap.result_status = 2
    return packet.pack()


def make_telemetry_packet(
    *,
    frame_identifier: int = 10,
    overall_frame_identifier: int = 20,
    player_car_index: int = 0,
    packet_format: int = 2025,
    throttle: float = 0.75,
) -> bytes:
    packet = PacketCarTelemetryData()
    populate_header(
        packet.header,
        6,
        frame_identifier=frame_identifier,
        overall_frame_identifier=overall_frame_identifier,
        player_car_index=player_car_index,
        packet_format=packet_format,
    )
    if player_car_index < 22:
        telemetry = packet.car_telemetry_data[player_car_index]
        telemetry.speed = 287
        telemetry.throttle = throttle
        telemetry.brake = 0.25
        telemetry.steer = -0.5
        telemetry.clutch = 10
        telemetry.gear = 7
        telemetry.engine_rpm = 11_800
        telemetry.drs = 1
    return packet.pack()


def record(payload: bytes, timestamp: int = 1_000) -> CaptureRecord:
    return CaptureRecord(timestamp, "192.0.2.10", 20_777, payload)


class DecoderTests(unittest.TestCase):
    def test_decodes_lap_data_and_allows_negative_distance(self) -> None:
        lap = decode_lap_data(make_lap_packet())

        self.assertEqual(lap.identity.frame_key, (123, 10, 20, 0))
        self.assertEqual(lap.lap_number, 3)
        self.assertEqual(lap.lap_distance, -4.5)
        self.assertEqual(lap.current_lap_time_ms, 12_345)
        self.assertEqual(lap.last_lap_time_ms, 91_234)
        self.assertTrue(lap.lap_invalid)

    def test_decodes_official_car_telemetry_fields(self) -> None:
        telemetry = decode_car_telemetry(make_telemetry_packet())

        self.assertEqual(telemetry.speed_kph, 287)
        self.assertAlmostEqual(telemetry.throttle, 0.75)
        self.assertAlmostEqual(telemetry.brake, 0.25)
        self.assertAlmostEqual(telemetry.steering, -0.5)
        self.assertEqual(telemetry.gear, 7)
        self.assertEqual(telemetry.engine_rpm, 11_800)
        self.assertTrue(telemetry.drs)

    def test_rejects_wrong_size_format_and_player_index(self) -> None:
        with self.assertRaisesRegex(PacketDecodeError, "has 1284 bytes"):
            decode_lap_data(make_lap_packet()[:-1])
        with self.assertRaisesRegex(PacketDecodeError, "unsupported packet format"):
            decode_lap_data(make_lap_packet(packet_format=2024))
        with self.assertRaisesRegex(PacketDecodeError, "outside 0..21"):
            decode_lap_data(make_lap_packet(player_car_index=255))

    def test_rejects_values_outside_the_official_telemetry_range(self) -> None:
        with self.assertRaisesRegex(PacketDecodeError, "throttle value"):
            decode_car_telemetry(make_telemetry_packet(throttle=1.5))


class NormalizerTests(unittest.TestCase):
    def test_joins_out_of_order_packets_by_full_frame_identity(self) -> None:
        normalizer = FrameNormalizer()

        self.assertIsNone(normalizer.accept(record(make_telemetry_packet(), 2_000)))
        sample = normalizer.accept(record(make_lap_packet(), 1_000))
        normalizer.finish()

        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertEqual(sample.captured_unix_ns, 2_000)
        self.assertEqual(sample.frame_identifier, 10)
        self.assertEqual(sample.overall_frame_identifier, 20)
        self.assertEqual(sample.speed_kph, 287)
        self.assertEqual(sample.lap_distance, -4.5)
        self.assertEqual(normalizer.stats.samples_joined, 1)
        self.assertEqual(normalizer.stats.unmatched_lap_packets, 0)
        self.assertEqual(normalizer.stats.unmatched_telemetry_packets, 0)

    def test_does_not_join_different_overall_frame_identifiers(self) -> None:
        normalizer = FrameNormalizer()
        normalizer.accept(record(make_lap_packet(overall_frame_identifier=20)))
        normalizer.accept(record(make_telemetry_packet(overall_frame_identifier=21)))
        normalizer.finish()

        self.assertEqual(normalizer.stats.samples_joined, 0)
        self.assertEqual(normalizer.stats.unmatched_lap_packets, 1)
        self.assertEqual(normalizer.stats.unmatched_telemetry_packets, 1)

    def test_counts_other_formats_decode_errors_and_duplicates(self) -> None:
        normalizer = FrameNormalizer()
        normalizer.accept(record(make_lap_packet()))
        normalizer.accept(record(make_lap_packet()))
        normalizer.accept(record(make_lap_packet(packet_format=2024)))
        normalizer.accept(record(make_lap_packet()[:-1]))
        normalizer.accept(record(b"tiny"))
        normalizer.finish()

        self.assertEqual(normalizer.stats.duplicate_lap_packets, 1)
        self.assertEqual(normalizer.stats.unsupported_packet_formats, 1)
        self.assertEqual(normalizer.stats.decode_errors, 1)
        self.assertEqual(normalizer.stats.malformed_packet_headers, 1)

    def test_capture_to_csv_integration_uses_the_documented_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            capture_path = Path(temp_dir) / "input.f1cap"
            csv_path = Path(temp_dir) / "output.csv"
            with CaptureWriter(capture_path) as writer:
                writer.write_datagram(make_lap_packet(), ("192.0.2.10", 20_777))
                writer.write_datagram(
                    make_telemetry_packet(), ("192.0.2.10", 20_777)
                )

            result = normalize_capture(capture_path)
            write_normalized_csv(result, csv_path)
            with csv_path.open(newline="", encoding="utf-8") as file:
                rows = list(csv.DictReader(file))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_uid"], "123")
        self.assertEqual(rows[0]["lap_number"], "3")
        self.assertEqual(rows[0]["speed_kph"], "287")
        self.assertEqual(rows[0]["lap_invalid"], "True")


if __name__ == "__main__":
    unittest.main()
