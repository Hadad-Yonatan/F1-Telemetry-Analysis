import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from f1.packets import (
    PacketCarSetupData,
    PacketCarStatusData,
    PacketLapData,
    PacketParticipantsData,
    PacketSessionData,
    PacketSessionHistoryData,
    PacketTyreSetsData,
)

from f1_telemetry.analysis_output import analysis_document
from f1_telemetry.capture_format import CaptureWriter
from f1_telemetry.context_decode import decode_car_setup
from f1_telemetry.lap_analysis import LapTrace, analyze_capture
from f1_telemetry.report import render_markdown_report
from f1_telemetry.session_context import ContextCollector, context_document, setup_fingerprint
from f1_telemetry.validation import _observations
from tests.test_decode_normalize import make_telemetry_packet, populate_header, record
from tests.test_lap_analysis import make_sample


def packet(packet_type, packet_id, frame, *, rewound_frame=None, uid=123, player=0):
    value = packet_type()
    populate_header(
        value.header, packet_id, session_uid=uid, player_car_index=player,
        overall_frame_identifier=frame,
        frame_identifier=frame if rewound_frame is None else rewound_frame,
    )
    return value


def session_packet(frame, *, weather=0, **kwargs):
    value = packet(PacketSessionData, 1, frame, **kwargs)
    value.session_type = 18
    value.game_mode = 5
    value.track_temperature = 33
    value.air_temperature = 24
    value.weather = weather
    value.track_length = 100
    value.track_id = 98
    value.sector2_lap_distance_start = 33.3
    value.sector3_lap_distance_start = 66.7
    return value


def setup_packet(frame, wing, *, next_wing=0.0, **kwargs):
    value = packet(PacketCarSetupData, 5, frame, **kwargs)
    setup = value.car_setup_data[value.header.player_car_index]
    setup.front_wing = wing
    setup.front_camber = -3.2
    setup.fuel_load = 5.0
    value.next_front_wing_value = next_wing
    return value


def add(collector, value, timestamp=None):
    frame = value.header.overall_frame_identifier
    collector.accept(record(value.pack(), frame if timestamp is None else timestamp), value.header.packet_id)


class SessionContextTests(unittest.TestCase):
    def test_start_and_changes_follow_overall_frame_then_capture_timestamp(self):
        collector = ContextCollector(123, 0)
        add(collector, setup_packet(16, 35, rewound_frame=3), 400)
        add(collector, setup_packet(9, 20), 500)
        add(collector, setup_packet(12, 28), 600)
        add(collector, setup_packet(12, 29), 700)
        add(collector, setup_packet(12, 29), 701)
        add(collector, setup_packet(12, 25), 650)
        add(collector, setup_packet(22, 99), 900)
        add(collector, setup_packet(12, 100, uid=999), 800)
        add(collector, setup_packet(12, 100, player=1), 801)
        add(collector, session_packet(10))
        add(collector, session_packet(18, weather=2, rewound_frame=6))
        lap = LapTrace(
            lap_number=1, official_lap_time_ms=3000,
            samples=(make_sample(11, 0, 0), make_sample(12, 5, 100), make_sample(21, 100, 3000)),
            discarded_attempt_frames=4,
        )
        context = collector.finish([lap])
        lap_context = context.laps[0]
        self.assertEqual(lap_context.start_frame, 12)
        self.assertEqual(context.at_lap_start(lap_context, "setup").values.front_wing, 29)
        self.assertIsNone(context.at_lap_start(lap_context, "tyres"))
        self.assertEqual(
            [context.timeline[index].overall_frame_identifier for index in lap_context.change_indices],
            [16, 18],
        )
        self.assertFalse(lap_context.stable_during_lap)
        stats = next(row for row in context.diagnostics if row.kind == "setup")
        self.assertEqual((stats.accepted, stats.ignored, stats.duplicates, stats.conflicts), (7, 2, 1, 2))
        self.assertEqual(context_document(context)["timeline"][2]["captured_unix_ns"], "701")

    def test_stable_lap_preserves_fuel_changes_and_ignores_other_cars_tyres(self):
        collector = ContextCollector(123, 0)
        add(collector, session_packet(1))
        participant = packet(PacketParticipantsData, 4, 2)
        participant.num_active_cars = 1
        participant.participants[0].name = b"Driver"
        add(collector, participant)
        add(collector, setup_packet(3, 25))
        for frame, fuel in ((4, 6.0), (12, 5.875)):
            status = packet(PacketCarStatusData, 7, frame)
            status.car_status_data[0].fuel_in_tank = fuel
            status.car_status_data[0].actual_tyre_compound = 16
            add(collector, status)
        tyres = packet(PacketTyreSetsData, 12, 13)
        tyres.car_idx = 1
        add(collector, tyres)
        lap = LapTrace(
            lap_number=1, official_lap_time_ms=3000,
            samples=(make_sample(5, 0, 1), make_sample(20, 100, 3000)),
            discarded_attempt_frames=0,
        )
        context = collector.finish([lap])
        lap_context = context.laps[0]
        self.assertTrue(lap_context.stable_during_lap)
        self.assertEqual(lap_context.missing_at_start, ("tyres", "time_trial"))
        self.assertEqual(context.at_lap_start(lap_context, "car_status").fuel_in_tank_kg, 6.0)
        self.assertEqual(len(lap_context.change_indices), 1)
        self.assertEqual(context.timeline[lap_context.change_indices[0]].value.fuel_in_tank_kg, 5.875)
        self.assertEqual(next(row for row in context.diagnostics if row.kind == "tyres").ignored, 1)
        missing = ContextCollector(123, 0).finish([lap])
        self.assertIsNone(missing.laps[0].stable_during_lap)
        self.assertIsNone(missing.settings)

    def test_setup_fingerprint_preserves_binary32_and_ignores_next_wing(self):
        initial = decode_car_setup(setup_packet(3, 25).pack()).setup
        repeated = decode_car_setup(setup_packet(9, 25, next_wing=11.0).pack()).setup
        self.assertEqual(
            setup_fingerprint(initial),
            "704e4d1660f3abbbaefbdc53370b027b2fe0530db381567418a0ef57b45651b2",
        )
        self.assertEqual(setup_fingerprint(initial), setup_fingerprint(repeated))
        self.assertNotEqual(setup_fingerprint(initial), setup_fingerprint(replace(initial, front_wing=26)))
        self.assertNotEqual(
            setup_fingerprint(replace(initial, front_toe=-0.0)),
            setup_fingerprint(replace(initial, front_toe=0.0)),
        )

    def test_first_midlap_observation_does_not_prove_a_change(self):
        collector = ContextCollector(123, 0)
        add(collector, setup_packet(12, 25))
        lap = LapTrace(
            lap_number=1, official_lap_time_ms=3000,
            samples=(make_sample(5, 0, 1), make_sample(20, 100, 3000)),
            discarded_attempt_frames=0,
        )
        context = collector.finish([lap])
        self.assertIsNone(context.laps[0].stable_during_lap)
        self.assertEqual(len(context.laps[0].change_indices), 1)
        add(collector, setup_packet(13, 30))
        self.assertFalse(collector.finish([lap]).laps[0].stable_during_lap)

    def test_mixed_player_trace_cannot_inherit_another_players_setup(self):
        collector = ContextCollector(123, 0)
        add(collector, setup_packet(1, 25))
        lap = LapTrace(
            lap_number=1, official_lap_time_ms=3000,
            samples=(
                make_sample(5, 0, 0),
                replace(make_sample(6, 4, 1), player_car_index=1),
                replace(make_sample(20, 100, 3000), player_car_index=1),
            ),
            discarded_attempt_frames=0,
        )
        context = collector.finish([lap])
        lap_context = context.laps[0]
        self.assertIsNone(context.at_lap_start(lap_context, "setup"))
        self.assertIsNone(lap_context.stable_during_lap)
        self.assertEqual(lap_context.change_indices, ())
        self.assertIn("selected session and player", lap_context.unavailable_reason)

    def test_real_analysis_path_reports_missing_and_malformed_optional_context(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.f1cap"
            session = session_packet(1)
            bad_setup = setup_packet(3, 25)
            bad_setup.car_setup_data[0].front_camber = float("nan")
            history = packet(PacketSessionHistoryData, 11, 22)
            history.num_laps = 2
            for i in range(2):
                lap = history.lap_history_data[i]
                lap.lap_valid_bit_flags = 15
                lap.lap_time_in_ms = 3000 + i * 100
                lap.sector1_time_ms_part = 1000
                lap.sector2_time_ms_part = 1000
                lap.sector3_time_ms_part = 1000 + i * 100
            with CaptureWriter(path, created_unix_ns=42) as writer:
                writer.write_datagram(session.pack(), ("192.0.2.10", 20777), captured_unix_ns=1)
                writer.write_datagram(bad_setup.pack(), ("192.0.2.10", 20777), captured_unix_ns=3)
                for frame, number, distance, elapsed in (
                    (5, 1, 0, 1), (6, 1, 99, 2990), (15, 2, 0, 1), (16, 2, 99, 3090)
                ):
                    value = packet(PacketLapData, 2, frame)
                    lap = value.lap_data[0]
                    lap.current_lap_num = number
                    lap.lap_distance = distance
                    lap.current_lap_time_in_ms = elapsed
                    lap.driver_status = 1
                    writer.write_datagram(value.pack(), ("192.0.2.10", 20777), captured_unix_ns=frame)
                    writer.write_datagram(
                        make_telemetry_packet(frame_identifier=frame, overall_frame_identifier=frame),
                        ("192.0.2.10", 20777), captured_unix_ns=frame,
                    )
                writer.write_datagram(history.pack(), ("192.0.2.10", 20777), captured_unix_ns=22)
            result = analyze_capture(path)
            doc = analysis_document(result)
            self.assertEqual((result.reference_lap, result.comparisons[0].official_delta_s), (1, 0.1))
            self.assertEqual(doc["schema_version"], 1)
            self.assertEqual(doc["algorithm"]["version"], "1.1.0")
            self.assertEqual(doc["session_context"]["session_uid"], "123")
            self.assertEqual(doc["session_context"]["settings"]["track_temperature_c"], 33)
            self.assertIsNone(doc["session_context"]["laps"][0]["start_indices"]["setup"])
            self.assertIn("## Session context", render_markdown_report(result))
            setup_diagnostics = next(row for row in _observations(result).context_diagnostics if row.kind == "setup")
            self.assertEqual(setup_diagnostics.decode_errors, 1)
            self.assertEqual(result.session.normalization.decode_errors, 0)
