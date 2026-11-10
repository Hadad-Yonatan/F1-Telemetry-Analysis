import json
import io
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from f1.packets import PacketCarStatusData, PacketCarTelemetryData, PacketLapData, PacketParticipantsData, PacketSessionHistoryData

from f1_telemetry.analysis_output import write_analysis_bundle
from f1_telemetry.bundle import BundleError, verify_bundle, write_bundle_manifest
from f1_telemetry.capture_format import CaptureWriter
from f1_telemetry.experiment import main, run_experiment
from f1_telemetry.experiment_model import ExperimentError, parse_manifest
from f1_telemetry.lap_analysis import analyze_capture
from f1_telemetry.library import import_bundles, init_library, rebuild_library, verify_library
from f1_telemetry.library_experiments import read_experiment_artifact
from f1_telemetry.library_service import LibraryService
from tests.test_analysis_output import matching_catalog
from tests.test_composite_motion import motion_ex_packet, motion_packet
from tests.test_session_context import packet, session_packet, setup_packet


def write_trial_bundle(
    root: Path, name: str, *, wing=25, fuel=10.0, weather=0, air=24, track=33, offset=0.0,
    caution=0, damage=0, wing_during_lap=None, motion=True, reference=None, shared_capture=None,
    uid=None, malformed_setup=False, running_bias=0,
):
    capture = shared_capture or root / f"{name}.f1cap"
    if shared_capture is None:
        uid = int.from_bytes(sha256(name.encode()).digest()[:8], "big") if uid is None else uid
        history = packet(PacketSessionHistoryData, 11, 1_000, uid=uid)
        history.num_laps = 3
        sender = ("192.0.2.10", 20777)
        with CaptureWriter(capture, created_unix_ns=42) as writer:
            def write(value, frame):
                writer.write_datagram(value.pack(), sender, captured_unix_ns=frame)

            for number in range(1, 4):
                base = number * 100
                official = 3000 + number * 100 + (100 if wing != 25 else 0)
                record = history.lap_history_data[number - 1]
                record.lap_time_in_ms = official
                record.sector1_time_ms_part = 900
                record.sector2_time_ms_part = 900
                record.sector3_time_ms_part = official - 1800
                record.lap_valid_bit_flags = 15
                session = session_packet(base + 1, weather=weather, uid=uid)
                session.track_id = 10
                session.air_temperature = air
                session.track_temperature = track
                session.gearbox_assist = 2
                session.safety_car_status = caution
                session.car_damage = damage
                write(session, base + 1)
                participant = packet(PacketParticipantsData, 4, base + 2, uid=uid)
                participant.num_active_cars = 1
                participant.participants[0].team_id = 3
                participant.participants[0].name = b"Driver"
                write(participant, base + 2)
                write(setup_packet(base + 3, wing, uid=uid), base + 3)
                status = packet(PacketCarStatusData, 7, base + 4, uid=uid)
                status.car_status_data[0].actual_tyre_compound = 16
                status.car_status_data[0].visual_tyre_compound = 16
                status.car_status_data[0].fuel_in_tank = fuel
                status.car_status_data[0].front_brake_bias = running_bias
                write(status, base + 4)
                for ordinal, distance in enumerate(range(0, 101, 5), 10):
                    frame = base + ordinal
                    lap_value = packet(PacketLapData, 2, frame, uid=uid)
                    lap = lap_value.lap_data[0]
                    lap.current_lap_num = number
                    lap.current_lap_time_in_ms = max(1, round(official * distance / 100))
                    lap.lap_distance = distance
                    lap.driver_status = 1
                    write(lap_value, frame)
                    telemetry = packet(PacketCarTelemetryData, 6, frame, uid=uid)
                    car = telemetry.car_telemetry_data[0]
                    car.speed = 140 + abs(distance - 30)
                    car.throttle = 0.1 if distance < 40 else 1.0
                    car.brake = 0.8 if 10 <= distance <= 25 else 0.0
                    car.gear = 4
                    write(telemetry, frame)
                    if motion:
                        identity = {"frame_identifier": frame, "overall_frame_identifier": frame, "session_uid": uid}
                        moving = motion_packet(position=offset, **identity)
                        moving.car_motion_data[0].world_position_z = distance
                        write(moving, frame)
                        extended = motion_ex_packet(**identity)
                        extended.wheel_slip_ratio[0] = offset / 4
                        write(extended, frame)
                    if number == 1 and distance == 50 and wing_during_lap is not None:
                        setup = setup_packet(frame, wing_during_lap, uid=uid)
                        if malformed_setup:
                            writer.write_datagram(setup.pack()[:-1], sender, captured_unix_ns=frame + 1)
                        else:
                            write(setup, frame + 1)
            write(history, 1_000)
    result = analyze_capture(
        capture, corner_catalog=matching_catalog(), reference_lap=reference, motion="auto" if motion else "off",
    )
    bundle = root / name
    write_analysis_bundle(result, bundle)
    return bundle


def manifest_document(control: Path, candidate: Path, *, label="Candidate"):
    return {
        "schema_version": 1,
        "control": {"bundle_id": verify_bundle(control).bundle_id, "label": "Control", "laps": [3, 1, 2]},
        "candidate": {"bundle_id": verify_bundle(candidate).bundle_id, "label": label, "laps": [1, 2, 3]},
        "changed_setup_fields": ["front_wing"],
    }


class SetupExperimentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundles = self.root / "bundles"
        self.bundles.mkdir()
        self.control = write_trial_bundle(self.bundles, "control")
        self.candidate = write_trial_bundle(self.bundles, "candidate", wing=30, fuel=10.25, air=26, track=36, offset=2)
        self.db = self.root / "library.sqlite3"
        init_library(self.db)
        import_bundles(self.db, [self.control, self.candidate])

    def test_deterministic_artifacts_and_shared_metric_inputs(self):
        manifest = parse_manifest(manifest_document(self.control, self.candidate, label="<Candidate>"))
        self.assertEqual(manifest.control.laps, (1, 2, 3))
        first, second = self.root / "first", self.root / "second"
        experiment_id = run_experiment(manifest, self.db, first)
        self.assertEqual(run_experiment(manifest, self.db, second), experiment_id)
        for file in first.iterdir():
            self.assertEqual(file.read_bytes(), (second / file.name).read_bytes())
        result = json.loads((first / "experiment.json").read_bytes())
        self.assertEqual(result["setup_changes"], [{"field": "front_wing", "control": 25, "candidate": 30}])
        metrics = {row["metric"]: row for row in result["metrics"]}
        self.assertAlmostEqual(metrics["pace.lap_time"]["candidate_minus_control"], 0.1)
        self.assertAlmostEqual(metrics["pace.lap_time"]["control"]["mad"], 0.1)
        self.assertEqual(metrics["pace.lap_time"]["control"]["count"], 3)
        self.assertEqual(metrics["dynamics.slip_ratio_rear_left"]["candidate_minus_control"], 0.5)
        self.assertEqual(metrics["line.absolute_lateral_deviation"]["candidate_minus_control"], 2.0)
        self.assertEqual(metrics["consistency.test-t01.duration"]["control"]["count"], 3)
        self.assertEqual(metrics["event.test-t01.minimum_speed_kph"]["control"]["count"], 3)
        self.assertIn("&lt;Candidate&gt;", (first / "report.html").read_text())
        self.assertEqual(verify_library(self.db), 2)
        service = LibraryService(self.db)
        self.assertEqual(len(service.list_experiments()), 1)
        snapshot = service.get_experiment(experiment_id)
        self.assertEqual(rebuild_library(self.db, self.bundles), (2, 0))
        self.assertEqual(service.get_experiment(experiment_id), snapshot)

    def test_manifest_is_strict_and_fuel_cannot_be_a_setup_change(self):
        original = manifest_document(self.control, self.candidate)
        for changes, message in (
            ({"extra": 1}, "exactly"),
            ({"changed_setup_fields": ["fuel_load"]}, "fuel_load cannot"),
            ({"changed_setup_fields": ["front_wing", "front_wing"]}, "duplicates"),
            ({"control": {**original["control"], "laps": [1, 1, 2]}}, "three distinct"),
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                parse_manifest({**original, **changes})

    def test_controls_reject_unmatched_or_unstable_conditions(self):
        cases = (
            ({"fuel": 10.251}, "lap_start_fuel_kg"),
            ({"air": 27}, "air_temperature_c"),
            ({"track": 37}, "track_temperature_c"),
            ({"weather": 1}, "weather"),
            ({"damage": 1}, "damage-free"),
            ({"caution": 1}, "caution periods"),
            ({"wing_during_lap": 28}, "setup changed"),
            ({"wing_during_lap": 28, "malformed_setup": True}, "malformed setup evidence"),
            ({"running_bias": 65}, "running brake bias"),
        )
        for index, (options, message) in enumerate(cases):
            with self.subTest(options=options):
                bundle = write_trial_bundle(self.bundles, f"invalid-{index}", wing=30, motion=False, **options)
                import_bundles(self.db, [bundle])
                manifest = parse_manifest(manifest_document(self.control, bundle))
                output = self.root / f"out-{index}"
                with self.assertRaisesRegex(ExperimentError, message):
                    run_experiment(manifest, self.db, output)
                self.assertFalse(output.exists())
        self.assertEqual(LibraryService(self.db).list_experiments(), ())

    def test_reanalyzed_same_capture_laps_cannot_overlap(self):
        alternate = write_trial_bundle(
            self.bundles, "alternate", shared_capture=self.bundles / "control.f1cap", reference=2,
        )
        import_bundles(self.db, [alternate])
        with self.assertRaisesRegex(ExperimentError, "overlapping laps from the same capture"):
            run_experiment(parse_manifest(manifest_document(self.control, alternate)), self.db, self.root / "out")

    def test_another_capture_of_the_same_player_session_cannot_repeat_laps(self):
        uid = int(verify_bundle(self.control).document["session_context"]["session_uid"])
        alternate = write_trial_bundle(self.bundles, "duplicate-session", wing=30, motion=False, uid=uid)
        import_bundles(self.db, [alternate])
        with self.assertRaisesRegex(ExperimentError, "overlapping laps from the same player session"):
            run_experiment(parse_manifest(manifest_document(self.control, alternate)), self.db, self.root / "out")

    def test_undeclared_setup_changes_are_rejected(self):
        document = manifest_document(self.control, self.candidate)
        document["changed_setup_fields"] = ["rear_wing"]
        with self.assertRaisesRegex(ExperimentError, "undeclared setup differences: front_wing"):
            run_experiment(parse_manifest(document), self.db, self.root / "out")

    def test_hash_valid_source_cannot_hide_an_in_lap_setup_change(self):
        bundle = write_trial_bundle(self.bundles, "hidden-change", wing=30, wing_during_lap=28, motion=False)
        path = bundle / "analysis.json"
        document = json.loads(path.read_bytes())
        self.assertTrue(document["session_context"]["laps"][0]["change_indices"])
        document["session_context"]["laps"][0]["change_indices"] = []
        path.write_text(json.dumps(document), encoding="utf-8")
        (bundle / "bundle.json").unlink()
        write_bundle_manifest(bundle)
        import_bundles(self.db, [bundle])
        with self.assertRaisesRegex(ExperimentError, "do not match the context timeline"):
            run_experiment(parse_manifest(manifest_document(self.control, bundle)), self.db, self.root / "out")

    def test_missing_motion_is_typed_unavailable_not_zero(self):
        candidate = write_trial_bundle(self.bundles, "no-motion", wing=30, motion=False)
        import_bundles(self.db, [candidate])
        run_experiment(parse_manifest(manifest_document(self.control, candidate)), self.db, self.root / "out")
        result = json.loads((self.root / "out" / "experiment.json").read_bytes())
        metric = next(row for row in result["metrics"] if row["metric"] == "line.absolute_lateral_deviation")
        self.assertIsNone(metric["candidate_minus_control"])
        self.assertEqual(metric["candidate"]["count"], 0)
        self.assertEqual(metric["candidate"]["samples"][0]["reason"], "motion_not_available_in_both_bundles")

    def test_migration_and_publish_roll_back_on_failure_or_interruption(self):
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("DROP TABLE experiment_metrics")
            connection.execute("DROP TABLE experiments")
            connection.execute("PRAGMA user_version=3")
        manifest = parse_manifest(manifest_document(self.control, self.candidate))
        for injected in (sqlite3.OperationalError("injected"), KeyboardInterrupt()):
            with patch("f1_telemetry.experiment.index_experiment", side_effect=injected):
                with self.assertRaises(type(injected)):
                    run_experiment(manifest, self.db, self.root / "out")
            self.assertFalse((self.root / "out").exists())
            self.assertEqual(list(self.root.glob(".out.tmp-*")), [])
            with closing(sqlite3.connect(self.db)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone(), (3,))
        run_experiment(manifest, self.db, self.root / "out")
        self.assertEqual(verify_library(self.db), 2)

    def test_tampered_or_symlinked_artifact_blocks_rebuild(self):
        manifest = parse_manifest(manifest_document(self.control, self.candidate))
        output = self.root / "out"
        run_experiment(manifest, self.db, output)
        report = output / "report.html"
        report.write_bytes(report.read_bytes() + b"tampered")
        before = self.db.read_bytes()
        with self.assertRaisesRegex(BundleError, "SHA-256 changed"):
            rebuild_library(self.db, self.bundles)
        self.assertEqual(self.db.read_bytes(), before)
        report.rename(self.root / "external.html")
        report.symlink_to(self.root / "external.html")
        with self.assertRaisesRegex(BundleError, "unsafe artifact"):
            verify_library(self.db)

    def test_interrupt_after_rename_rolls_back_index_and_owned_output(self):
        out = self.root / "out"
        manifest = parse_manifest(manifest_document(self.control, self.candidate))
        rename = Path.rename

        def rename_then_interrupt(source, target):
            rename(source, target)
            raise KeyboardInterrupt

        with patch.object(Path, "rename", rename_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_experiment(manifest, self.db, out)
        self.assertFalse(out.exists())
        self.assertEqual(LibraryService(self.db).list_experiments(), ())
        self.assertEqual(list(self.root.glob(".out.tmp-*")), [])
        self.assertTrue(run_experiment(manifest, self.db, out))

    def test_cli_and_hash_valid_but_inconsistent_summary(self):
        manifest = self.root / "request.json"
        manifest.write_text(json.dumps(manifest_document(self.control, self.candidate)), encoding="utf-8")
        out = self.root / "out"
        with redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main(["run", str(manifest), "--database", str(self.db), "--output-dir", str(out)]), 0)
        self.assertIn("Experiment", stdout.getvalue())
        path = out / "experiment.json"
        document = json.loads(path.read_bytes())
        document["metrics"][0]["control"]["mad"] = 123.0
        raw = json.dumps(document).encode()
        path.write_bytes(raw)
        with self.assertRaisesRegex(BundleError, "mad does not match"):
            read_experiment_artifact(out.resolve(), sha256(raw).hexdigest(), sha256((out / "report.html").read_bytes()).hexdigest())
