import tempfile
import unittest
from pathlib import Path

from f1.packets import PacketLapData, PacketSessionHistoryData

from f1_telemetry.analysis_output import analysis_document
from f1_telemetry.capture_format import CaptureWriter
from f1_telemetry.decode import DecodedLapHistory
from f1_telemetry.lap_analysis import analyze_capture, build_parser
from f1_telemetry.report import render_markdown_report
from f1_telemetry.session_policy import ConditionCohort, ContextDifference, TimeTrialPolicy, select_session_policy
from f1_telemetry.validation import _observations
from tests.test_decode_normalize import make_telemetry_packet
from tests.test_session_context import packet, session_packet


def write_timeline(path: Path, session_type: int) -> None:
    session = session_packet(1)
    session.session_type = session_type
    history = packet(PacketSessionHistoryData, 11, 100)
    history.num_laps = 2
    for index, lap in enumerate(history.lap_history_data[:2]):
        lap.lap_time_in_ms = 3000 + index * 100
        lap.sector1_time_ms_part = 1000
        lap.sector2_time_ms_part = 1000
        lap.sector3_time_ms_part = 1000 + index * 100
        lap.lap_valid_bit_flags = 15
    sender = ("192.0.2.10", 20777)
    with CaptureWriter(path, created_unix_ns=42) as writer:
        writer.write_datagram(session.pack(), sender, captured_unix_ns=1)
        for frame, number, distance, elapsed in (
            (5, 1, 0, 1), (6, 1, 99, 2990), (15, 2, 0, 1), (16, 2, 99, 3090),
        ):
            value = packet(PacketLapData, 2, frame)
            lap = value.lap_data[0]
            lap.current_lap_num = number
            lap.lap_distance = distance
            lap.current_lap_time_in_ms = elapsed
            lap.driver_status = 1
            writer.write_datagram(value.pack(), sender, captured_unix_ns=frame)
            writer.write_datagram(
                make_telemetry_packet(frame_identifier=frame, overall_frame_identifier=frame),
                sender, captured_unix_ns=frame,
            )
        writer.write_datagram(history.pack(), sender, captured_unix_ns=100)


class SessionPolicyTests(unittest.TestCase):
    def test_time_trial_decision_preserves_history_gate_and_reason_precedence(self):
        policy = TimeTrialPolicy()
        for history, eligible, reason in (
            (DecodedLapHistory(1, 3000, 1000, 1000, 1000, 15), True, "eligible"),
            (DecodedLapHistory(2, 3000, 1000, 1000, 1000, 2), False, "officially invalid"),
            (DecodedLapHistory(3, 3000, 1000, 1000, 0, 15), False, "incomplete"),
            (DecodedLapHistory(4, 3000, 1000, 1000, 0, 2), False, "incomplete"),
        ):
            decision = policy.decide_history(history)
            self.assertEqual((decision.lap_number, decision.eligible, decision.reason), (history.lap_number, eligible, reason))
        self.assertEqual(policy.driver_statuses, (1,))

    def test_selection_is_explicit_and_unknown_type_does_not_fall_back(self):
        self.assertIsInstance(select_session_policy(18), TimeTrialPolicy)
        self.assertIsInstance(select_session_policy(18, "time-trial"), TimeTrialPolicy)
        with self.assertRaisesRegex(ValueError, "unsupported F1 25 session type 0"):
            select_session_policy(0)
        with self.assertRaisesRegex(ValueError, "not requested time-trial"):
            select_session_policy(1, "time-trial")
        for code, mode in ((1, "practice"), (5, "qualifying"), (15, "race")):
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, f"{mode} policy is experimental"):
                select_session_policy(code)

    def test_conditions_report_stable_field_level_differences(self):
        first = ConditionCohort.from_mapping("practice", {"weather": 0, "setup": "a"})
        second = ConditionCohort.from_mapping("qualifying", {"tyre": 16, "weather": 0, "setup": "b"})
        self.assertEqual(first.differences(second), (
            ContextDifference("session_mode", "practice", "qualifying"),
            ContextDifference("setup", "a", "b"),
            ContextDifference("tyre", None, 16),
        ))
        self.assertEqual(first.differences(first), ())

    def test_capture_pipeline_records_policy_but_preserves_time_trial_results(self):
        with tempfile.TemporaryDirectory() as scratch:
            capture = Path(scratch) / "tt.f1cap"
            write_timeline(capture, 18)
            auto = analyze_capture(capture)
            explicit = analyze_capture(capture, session_policy="time-trial")
            self.assertEqual(auto, explicit)
            document = analysis_document(auto)
            self.assertEqual(document["algorithm"]["version"], "1.1.0")
            self.assertEqual(document["session_policy"], {
                "schema_version": 1, "method_version": "1.0.0", "name": "time-trial",
                "session_type": 18, "status": "validated",
            })
            self.assertEqual(auto.reference_lap, 1)
            self.assertEqual(auto.comparisons[0].official_delta_s, 0.1)
            self.assertIn("Session policy: time-trial (validated", render_markdown_report(auto))
            self.assertEqual(_observations(auto).session_policy, auto.session.policy)
            self.assertEqual(build_parser().parse_args([str(capture)]).session_policy, "auto")
            unknown = Path(scratch) / "unknown.f1cap"
            write_timeline(unknown, 0)
            with self.assertRaisesRegex(ValueError, "no session policy applies"):
                analyze_capture(unknown)
