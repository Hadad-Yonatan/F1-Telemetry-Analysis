import json
import tempfile
import unittest
from pathlib import Path

from f1.packets import PacketCarStatusData, PacketLapData, PacketParticipantsData, PacketSessionHistoryData

from f1_telemetry.analysis_output import analysis_document, write_analysis_bundle
from f1_telemetry.bundle import verify_bundle
from f1_telemetry.capture_format import CaptureWriter
from f1_telemetry.lap_analysis import analyze_capture, prepare_session
from f1_telemetry.library import import_bundles, init_library, verify_library
from f1_telemetry.report import render_markdown_report
from tests.test_composite_motion import motion_ex_packet, motion_packet
from tests.test_decode_normalize import make_telemetry_packet
from tests.test_session_context import packet, session_packet, setup_packet


def write_experimental(
    path: Path, mode: int = 1, *, exclusions=False, setup=True, motion=False,
    foreign_lap=None, session_modes=None,
):
    sender = ("192.0.2.10", 20777)
    count = 13 if exclusions else 3
    history = packet(PacketSessionHistoryData, 11, 10_000)
    history.num_laps = count
    times = [3000, 3100, 2800] + [3200] * (count - 3)
    with CaptureWriter(path, created_unix_ns=42) as writer:
        def write(value, frame):
            writer.write_datagram(value.pack(), sender, captured_unix_ns=frame)

        for number, official in enumerate(times, 1):
            base = number * 100
            session = session_packet(base + 1, weather=1 if number == 3 else 0)
            session.session_type = (session_modes or {}).get(number, mode)
            session.gearbox_assist = 2
            write(session, base + 1)
            participant = packet(PacketParticipantsData, 4, base + 2)
            participant.num_active_cars = 1
            participant.participants[0].team_id = 3
            participant.participants[0].name = b"Driver"
            write(participant, base + 2)
            if setup:
                write(setup_packet(base + 3, 30 if number == 3 else 25), base + 3)
            status = packet(PacketCarStatusData, 7, base + 4)
            status.car_status_data[0].actual_tyre_compound = 16
            status.car_status_data[0].visual_tyre_compound = 16
            status.car_status_data[0].fuel_in_tank = 30.0 - number
            write(status, base + 4)

            record = history.lap_history_data[number - 1]
            record.lap_time_in_ms = official
            record.sector1_time_ms_part = 900
            record.sector2_time_ms_part = 900
            record.sector3_time_ms_part = 0 if number == 9 else official - 1800
            record.lap_valid_bit_flags = 2 if number == 8 else 15

            for ordinal, distance, elapsed in (
                (10, 11 if number == 10 else 0, 1),
                (20, 50, 2000),
                (30, 80 if number == 12 else 99, 1000 if number == 7 else official - 10),
            ):
                frame = base + ordinal
                game_frame = frame - 5 if number == 13 and ordinal >= 20 else frame
                player = int(number == foreign_lap)
                value = packet(PacketLapData, 2, frame, rewound_frame=game_frame, player=player)
                lap = value.lap_data[player]
                lap.current_lap_num = number
                lap.lap_distance = distance
                lap.current_lap_time_in_ms = elapsed
                lap.driver_status = {4: 2, 5: 3, 6: 0}.get(number, 1)
                write(value, frame)
                writer.write_datagram(
                    make_telemetry_packet(
                        frame_identifier=game_frame, overall_frame_identifier=frame, player_car_index=player,
                    ),
                    sender, captured_unix_ns=frame,
                )
                if motion:
                    identity = {"frame_identifier": game_frame, "overall_frame_identifier": frame}
                    write(motion_packet(position=distance, **identity), frame)
                    write(motion_ex_packet(**identity), frame)
                if number == 11 and ordinal == 20:
                    change = session_packet(frame + 1, weather=2)
                    change.session_type = mode
                    change.gearbox_assist = 2
                    write(change, frame + 1)
        write(history, 10_000)


class ExperimentalPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "session.f1cap"

    def test_practice_and_qualifying_require_opt_in_and_use_one_cohort(self):
        for mode, name in ((1, "practice"), (5, "qualifying"), (14, "qualifying")):
            with self.subTest(mode=mode):
                path = self.root / f"session-{mode}.f1cap"
                write_experimental(path, mode)
                with self.assertRaisesRegex(ValueError, "policy is experimental"):
                    analyze_capture(path, session_policy=name)
                result = analyze_capture(path, allow_experimental_session_mode=True)
                self.assertEqual((result.session.policy.name, result.session.policy.status), (name, "experimental"))
                # Lap 3 is fastest but has no same-condition partner.
                self.assertEqual(result.reference_lap, 1)
                self.assertEqual(tuple(row.comparison_lap for row in result.comparisons), (2,))
                self.assertEqual(result.comparisons[0].official_delta_s, 0.1)
                self.assertFalse(result.comparisons[0].policy_context.cross_context)
                self.assertEqual([lap.lap_number for lap in result.consistency.laps], [1, 2, 3])

    def test_cross_context_requires_both_laps_and_records_fields(self):
        write_experimental(self.path)
        common = {"allow_experimental_session_mode": True, "comparison_laps": (3,)}
        with self.assertRaisesRegex(ValueError, "explicitly select both laps"):
            analyze_capture(self.path, reference_lap=1, **common)
        with self.assertRaisesRegex(ValueError, "explicitly select both laps"):
            analyze_capture(self.path, allow_cross_context=True, **common)
        result = analyze_capture(self.path, reference_lap=1, allow_cross_context=True, **common)
        document = analysis_document(result)
        condition = document["comparisons"][0]["policy_context"]
        self.assertTrue(condition["cross_context"])
        differences = {row["field"]: (row["reference"], row["comparison"]) for row in condition["differences"]}
        self.assertEqual(differences["session.weather"], (0, 1))
        self.assertEqual(differences["setup.values.front_wing"], (25, 30))
        self.assertEqual(differences["car_status.fuel_in_tank_kg"], (29.0, 27.0))
        self.assertNotIn("car_status.fuel_in_tank_kg", {row["field"] for row in condition["cohort_differences"]})
        self.assertIn("cross-context comparison", render_markdown_report(result))

    def test_missing_evidence_is_not_a_verified_cohort(self):
        write_experimental(self.path, setup=False)
        common = {"allow_experimental_session_mode": True}
        with self.assertRaisesRegex(ValueError, "complete condition cohort"):
            analyze_capture(self.path, **common)
        result = analyze_capture(self.path, reference_lap=1, comparison_laps=(2,), allow_cross_context=True, **common)
        self.assertTrue(result.comparisons[0].policy_context.cross_context)
        self.assertIn("setup.fingerprint", result.comparisons[0].policy_context.reference_missing_fields)

    def test_packet_timeline_exclusions_are_explicit(self):
        write_experimental(self.path, exclusions=True)
        session = prepare_session(self.path, allow_experimental_session_mode=True)
        self.assertEqual([trace.lap_number for trace in session.laps], [1, 2, 3])
        reasons = {lap.lap_number: lap.reason for lap in session.validations if not lap.eligible}
        self.assertEqual(reasons, {
            4: "pit-entry lap", 5: "pit-exit lap", 6: "installation or garage lap",
            7: "interrupted lap", 8: "officially invalid", 9: "incomplete",
            10: "incomplete timing-line start", 11: "condition changed during lap",
            12: "incomplete timed trace",
            13: "interrupted lap",
        })
        self.assertEqual([lap.lap_number for lap in session.context.laps], [1, 2, 3])

    def test_cross_context_cannot_compare_another_players_lap(self):
        write_experimental(self.path, foreign_lap=2)
        session = prepare_session(self.path, allow_experimental_session_mode=True)
        self.assertEqual([lap.lap_number for lap in session.laps], [1, 3])
        self.assertEqual(session.validations[1].reason, "lap telemetry does not match the selected player")
        with self.assertRaisesRegex(ValueError, "selected player"):
            analyze_capture(
                self.path, reference_lap=1, comparison_laps=(2,),
                allow_experimental_session_mode=True, allow_cross_context=True,
            )

    def test_mixed_session_modes_do_not_relabel_earlier_laps(self):
        write_experimental(self.path, session_modes={1: 5, 2: 5})
        session = prepare_session(self.path, session_policy="practice", allow_experimental_session_mode=True)
        self.assertEqual([trace.lap_number for trace in session.laps], [3])
        self.assertTrue(all("does not match the selected policy" in row.reason for row in session.validations[:2]))

    def test_unselected_cross_context_motion_is_not_compared_or_indexed(self):
        write_experimental(self.path, motion=True)
        result = analyze_capture(self.path, allow_experimental_session_mode=True, motion="auto")
        self.assertEqual([row.lap_number for row in result.motion.comparisons], [2])
        unpaired = next(lap for lap in result.motion.laps if lap.lap_number == 3)
        heading = next(metric for metric in unpaired.metrics if metric.metric == "heading_difference_rad")
        self.assertTrue(all(value is None for value in heading.values))
        bundle = self.root / "bundle"
        write_analysis_bundle(result, bundle)
        self.assertEqual(verify_bundle(bundle).document, json.loads(json.dumps(analysis_document(result))))
        database = self.root / "library.sqlite3"
        init_library(database)
        self.assertEqual(import_bundles(database, [bundle]), (1, 0))
        self.assertEqual(verify_library(database), 1)
