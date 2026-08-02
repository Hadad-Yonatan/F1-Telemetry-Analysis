import unittest

from f1.packets import PacketSessionData, PacketSessionHistoryData

from f1_telemetry.decode import decode_session_data, decode_session_history
from f1_telemetry.lap_analysis import (
    LapTrace,
    LapValidation,
    _resolve_trace,
    compare_laps,
    resample_lap,
)
from f1_telemetry.normalize import NormalizedSample


def populate_header(header: object, packet_id: int) -> None:
    header.packet_format = 2025
    header.game_year = 25
    header.game_major_version = 1
    header.game_minor_version = 24
    header.packet_version = 1
    header.packet_id = packet_id
    header.session_uid = 123
    header.session_time = 10.0
    header.frame_identifier = 100
    header.overall_frame_identifier = 200
    header.player_car_index = 0
    header.secondary_player_car_index = 255


def make_sample(
    overall_frame: int,
    distance: float,
    elapsed_ms: int,
    *,
    speed: int = 200,
) -> NormalizedSample:
    return NormalizedSample(
        captured_unix_ns=overall_frame,
        session_uid=123,
        frame_identifier=overall_frame,
        overall_frame_identifier=overall_frame,
        session_time=elapsed_ms / 1000,
        player_car_index=0,
        lap_number=1,
        lap_distance=distance,
        total_distance=distance,
        current_lap_time_ms=elapsed_ms,
        last_lap_time_ms=0,
        sector=0,
        speed_kph=speed,
        throttle=1.0,
        brake=0.0,
        steering=0.0,
        clutch=0,
        gear=5,
        engine_rpm=10_000,
        drs=False,
        lap_invalid=False,
        pit_status=0,
        driver_status=1,
        result_status=2,
    )


class SessionDecoderTests(unittest.TestCase):
    def test_decodes_track_metadata(self) -> None:
        packet = PacketSessionData()
        populate_header(packet.header, 1)
        packet.track_length = 7_007
        packet.track_id = 10
        packet.session_type = 18
        packet.sector2_lap_distance_start = 2_323.75
        packet.sector3_lap_distance_start = 5_065.0

        decoded = decode_session_data(packet.pack())

        self.assertEqual(decoded.track_length, 7_007)
        self.assertEqual(decoded.track_id, 10)
        self.assertAlmostEqual(decoded.sector2_distance, 2_323.75)

    def test_decodes_official_validity_and_structural_completeness(self) -> None:
        packet = PacketSessionHistoryData()
        populate_header(packet.header, 11)
        packet.car_idx = 0
        packet.num_laps = 3
        for index, flags in enumerate((15, 2, 14)):
            lap = packet.lap_history_data[index]
            lap.lap_valid_bit_flags = flags
        complete = packet.lap_history_data[0]
        complete.lap_time_in_ms = 108_001
        complete.sector1_time_ms_part = 32_000
        complete.sector2_time_ms_part = 47_000
        complete.sector3_time_ms_part = 29_000

        decoded = decode_session_history(packet.pack())

        self.assertTrue(decoded.laps[0].officially_valid)
        self.assertTrue(decoded.laps[0].structurally_complete)
        self.assertFalse(decoded.laps[1].officially_valid)
        self.assertFalse(decoded.laps[2].structurally_complete)


class LapReconstructionTests(unittest.TestCase):
    def test_keeps_only_final_timed_attempt_after_restart(self) -> None:
        samples = [
            make_sample(1, 0, 0),
            make_sample(2, 50, 1_000),
            make_sample(3, 0, 0),
            make_sample(4, 50, 1_000),
            make_sample(5, 99.5, 2_000),
        ]
        validation = LapValidation(
            lap_number=1,
            official_lap_time_ms=2_100,
            sector_times_ms=(700, 700, 700),
            validity_flags=15,
            officially_valid=True,
            structurally_complete=True,
            eligible=True,
            reason="eligible",
        )

        trace = _resolve_trace(samples, validation, 100)

        self.assertEqual(trace.discarded_attempt_frames, 2)
        self.assertEqual(
            [sample.overall_frame_identifier for sample in trace.samples],
            [3, 4, 5],
        )

    def test_resampled_final_delta_matches_official_difference_exactly(self) -> None:
        reference = LapTrace(
            lap_number=1,
            official_lap_time_ms=2_000,
            samples=(make_sample(1, 0, 0), make_sample(2, 99, 1_900)),
            discarded_attempt_frames=0,
        )
        comparison = LapTrace(
            lap_number=2,
            official_lap_time_ms=2_500,
            samples=(make_sample(3, 0, 0), make_sample(4, 99, 2_400, speed=180)),
            discarded_attempt_frames=0,
        )

        result = compare_laps(
            resample_lap(reference, 100),
            resample_lap(comparison, 100),
        )

        self.assertAlmostEqual(result.official_delta_s, 0.5)
        self.assertAlmostEqual(result.calculated_final_delta_s, 0.5)
        self.assertAlmostEqual(result.final_delta_error_s, 0.0)


if __name__ == "__main__":
    unittest.main()
