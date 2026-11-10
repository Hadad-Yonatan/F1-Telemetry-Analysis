import json
import math
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from f1_telemetry.analysis_output import write_analysis_bundle
from f1_telemetry.bundle import BundleError, verify_bundle, write_bundle_manifest
from f1_telemetry.capture_format import CaptureWriter
from f1_telemetry.composite_motion import CompositeNormalizationResult, CompositeSample
from f1_telemetry.lap_analysis import analyze_capture, build_parser
from f1_telemetry.library import init_library, import_bundles, rebuild_library, verify_library
from f1_telemetry.library_service import LibraryService
from f1_telemetry.motion_analysis import MotionInputs, _line_metrics, analyze_motion, require_motion, retain_motion_inputs
from f1_telemetry.motion_decode import decode_motion, decode_motion_ex
from f1_telemetry.motion_render import track_map_svg
from f1_telemetry.normalize import NormalizationResult, NormalizationStats
from tests.test_analysis_output import make_sample, prepared_session
from tests.test_composite_motion import composite, motion_ex_packet, motion_packet


def base_motion(x, z, *, lateral_g=2.0, forward=True):
    packet = motion_packet(position=x)
    car = packet.car_motion_data[0]
    car.world_position_z = z
    car.world_forward_dir_x = 32767 if forward else 0
    car.world_forward_dir_z = 0
    car.g_force_lateral = lateral_g
    return decode_motion(packet.pack()).motion


def fixture_motion(session, reference_lap, distances, *, gap=False):
    """Synthetic trajectory for artifact and browser tests, no live recording."""
    ex = decode_motion_ex(motion_ex_packet().pack()).motion_ex
    groups = {}
    traces = []
    for trace in session.laps:
        number = trace.lap_number
        samples = []
        for index, distance in enumerate(range(0, session.track_length + 1, 5)):
            sample = make_sample(100 * number + index, number, distance, 10 * distance, 100)
            samples.append(sample)
            if not gap or number == reference_lap or distance <= 5 or distance >= 20:
                groups[id(sample)] = (
                    base_motion(10 + distance, 20 - (number - reference_lap) * 2, lateral_g=2 + number),
                    ex,
                )
        traces.append(replace(trace, samples=tuple(samples)))
    core_count = sum(len(trace.samples) for trace in traces)
    coverage = composite().coverage
    stream = replace(
        coverage.motion, packets=len(groups), joined_samples=len(groups), missing_samples=core_count - len(groups),
    )
    coverage = replace(coverage, joined_both=len(groups), core_samples=core_count, motion=stream, motion_ex=stream)
    inputs = MotionInputs(coverage, groups, (10.0, 20.0))
    return analyze_motion(replace(session, laps=tuple(traces)), inputs, reference_lap, distances)


def motion_bundle(root: Path, name: str, *, include_motion: bool = True):
    capture = root / "fixture.f1cap"
    if not capture.exists():
        with CaptureWriter(capture, created_unix_ns=123):
            pass
    with patch("f1_telemetry.lap_analysis.prepare_session", return_value=prepared_session()):
        result = analyze_capture(capture, corner_catalog=None)
    if include_motion:
        motion = fixture_motion(result.session, result.reference_lap, result.comparisons[0].reference.distance)
        result = replace(result, motion=motion)
    path = root / name
    write_analysis_bundle(result, path)
    return path


class MotionAnalysisTests(unittest.TestCase):
    def test_rotation_deviation_and_shared_valid_dynamics(self):
        session = replace(prepared_session(), track_length=25)
        motion = fixture_motion(session, 3, tuple(float(i) for i in range(26)), gap=True)
        reference, candidate = motion.laps[1], motion.laps[2]
        self.assertEqual(reference.positions_m[0], (0.0, 0.0))
        self.assertEqual(reference.positions_m[5], (0.0, 5.0))
        self.assertEqual(candidate.positions_m[5], (4.0, 5.0))
        self.assertIsNone(candidate.positions_m[6])
        self.assertEqual(candidate.positions_m[20], (4.0, 20.0))
        metrics = {metric.metric: metric for metric in motion.comparisons[-1].shared_metrics}
        self.assertEqual(metrics["lateral_deviation_m"].count, 12)
        self.assertEqual(metrics["lateral_deviation_m"].comparison.median, 4.0)
        self.assertEqual(metrics["heading_difference_rad"].comparison.median, 0.0)
        self.assertEqual(metrics["lateral_g"].reference.median, 5.0)
        self.assertEqual(metrics["lateral_g"].difference.median, 2.0)
        self.assertEqual(metrics["slip_ratio_rear_left"].count, 12)
        self.assertEqual(metrics["slip_ratio_rear_left"].comparison.median, -0.25)
        require_motion(motion, {1, 5})

    def test_heading_wraps_and_tangent_is_independent_of_vehicle_heading(self):
        direction = lambda degrees: (math.sin(math.radians(degrees)), math.cos(math.radians(degrees)))
        a, b = (0.0, 0.0), (2.0, 0.0)
        lateral, heading = _line_metrics(a, b, (0.0, 1.0), direction(179.0), direction(-179.0))
        self.assertEqual(lateral, 2.0)
        self.assertAlmostEqual(heading, math.radians(2.0))
        self.assertEqual(_line_metrics(a, None, (0.0, 1.0), direction(0.0), None), (None, None))

    def test_invalid_forward_does_not_anchor_heading_interpolation(self):
        session = prepared_session()
        groups = {}
        traces = []
        for trace in session.laps:
            number = trace.lap_number
            points = tuple(make_sample(100 * number + d, number, d, 10 * d, 100) for d in (0, 5))
            groups[id(points[0])] = (base_motion(10, 20, forward=number != 5), None)
            groups[id(points[1])] = (base_motion(15, 20), None)
            traces.append(replace(trace, samples=points))
        result = analyze_motion(
            replace(session, laps=tuple(traces)), MotionInputs(composite().coverage, groups, (10, 20)),
            3, tuple(float(d) for d in range(6)),
        )
        series = {item.metric: item.values for item in result.laps[-1].metrics}
        self.assertEqual(series["heading_difference_rad"], (None, None, None, None, None, 0.0))
        self.assertEqual(series["lateral_g"], (2.0,) * 6)
        self.assertEqual(result.laps[-1].positions_m[1], (0.0, 1.0))

    def test_coarse_output_grid_does_not_create_or_hide_raw_motion_gaps(self):
        grid = tuple(float(d) for d in range(0, 101, 20))
        result = fixture_motion(prepared_session(), 3, grid, gap=True)
        reference, candidate = result.laps[1:]
        self.assertEqual(reference.break_before, (True, False, False, False, False, False))
        self.assertEqual(candidate.break_before, (True, True, False, False, False, False))
        self.assertEqual(candidate.positions_m[1], (4.0, 20.0))
        metric = next(item for item in result.comparisons[-1].shared_metrics if item.metric == "lateral_deviation_m")
        self.assertEqual(metric.count, len(grid))
        self.assertEqual(metric.comparison.median, 4.0)
        svg = track_map_svg(result, {5})
        self.assertIn(b"M4.000000,-0.000000 M4.000000,-20.000000 L4.000000,-40.000000", svg)
        self.assertIn(b"M0.000000,-0.000000 L0.000000,-20.000000", svg)

    def test_start_line_interpolation_uses_total_distance_and_rejects_flashback(self):
        before = replace(make_sample(9, 2, 999, 100, 90), total_distance=101, session_time=1.0)
        after = replace(make_sample(10, 3, 1, 110, 90), total_distance=103, session_time=1.1)
        trace = replace(prepared_session().laps[1], samples=(after,))
        session = replace(prepared_session(), laps=(trace,))
        rows = (CompositeSample(before, base_motion(30, 40), None), CompositeSample(after, base_motion(34, 44), None))
        result = CompositeNormalizationResult(
            NormalizationResult((before, after), NormalizationStats()), rows, composite().coverage,
        )
        self.assertEqual(retain_motion_inputs(session, result, 3).timing_line, (32.0, 42.0))
        rewound = replace(before, frame_identifier=2)
        result = replace(result, samples=(replace(rows[0], core=rewound), rows[1]))
        self.assertIsNone(retain_motion_inputs(session, result, 3).timing_line)

    def test_unavailable_has_null_summaries_and_required_errors(self):
        session = prepared_session()
        result = analyze_motion(session, MotionInputs(composite().coverage, {}, None), 3, (0.0, 100.0))
        self.assertEqual(result.status, "unavailable")
        self.assertTrue(all(metric.count == 0 and metric.difference is None for metric in result.comparisons[0].shared_metrics))
        with self.assertRaisesRegex(ValueError, "required motion"):
            require_motion(result, {1})
        self.assertEqual(build_parser().parse_args(["example.f1cap"]).motion, "auto")
        self.assertEqual(build_parser().parse_args(["example.f1cap", "--motion", "off"]).motion, "off")


class MotionBundleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = motion_bundle(self.root, "one")
        self.db = self.root / "library.sqlite3"
        self.verified = verify_bundle(self.bundle)

    def test_motion_artifacts_are_bound_and_repeatable(self):
        second = motion_bundle(self.root, "two")
        self.assertEqual({p.name: p.read_bytes() for p in self.bundle.iterdir()},
                         {p.name: p.read_bytes() for p in second.iterdir()})
        self.assertIn(b"Reference timing line", (self.bundle / "track-map.svg").read_bytes())
        self.assertEqual(self.verified.document["motion"]["status"], "available")
        map_artifact = self.bundle / "track-map.svg"
        map_artifact.write_bytes(map_artifact.read_bytes() + b" ")
        (self.bundle / "bundle.json").unlink()
        write_bundle_manifest(self.bundle)
        with self.assertRaisesRegex(BundleError, "track map SHA-256"):
            verify_bundle(self.bundle)

    def test_v2_to_v3_migration_preserves_backfills_and_rebuilds(self):
        init_library(self.db)
        import_bundles(self.db, [self.bundle])
        service = LibraryService(self.db)
        expected = service.get_motion(self.verified.bundle_id)
        self.assertEqual(expected["comparisons"], self.verified.motion["comparisons"])
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("DROP TABLE motion_metrics")
            connection.execute("DROP TABLE motion_runs")
            connection.execute("PRAGMA user_version=2")
        init_library(self.db)
        self.assertEqual(service.get_motion(self.verified.bundle_id), expected)
        self.assertEqual(verify_library(self.db), 1)
        self.assertEqual(rebuild_library(self.db, self.root), (1, 0))
        self.assertEqual(service.get_motion(self.verified.bundle_id), expected)

    def replace_motion_artifact(self, motion):
        path = self.bundle / "motion.json"
        raw = json.dumps(motion).encode()
        path.write_bytes(raw)
        analysis = self.verified.document
        analysis["motion"]["sha256"] = sha256(raw).hexdigest()
        (self.bundle / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
        (self.bundle / "bundle.json").unlink()
        write_bundle_manifest(self.bundle)

    def test_malformed_sample_count_cannot_enter_index(self):
        motion = self.verified.motion
        motion["comparisons"][0]["shared_metrics"][0]["count"] -= 1
        self.replace_motion_artifact(motion)
        init_library(self.db)
        with self.assertRaisesRegex(BundleError, "invalid shared motion summary"):
            import_bundles(self.db, [self.bundle])
        self.assertEqual(LibraryService(self.db).list_sessions().total, 0)

    def test_missing_coverage_and_nullable_summary_fields_are_rejected(self):
        motion = self.verified.motion
        coverage = motion["coverage"]
        motion["coverage"] = {}
        self.replace_motion_artifact(motion)
        init_library(self.db)
        with self.assertRaisesRegex(BundleError, "motion coverage fields"):
            import_bundles(self.db, [self.bundle])
        motion["coverage"] = coverage
        for lap in motion["laps"]:
            series = lap["metrics"][0]
            series["values"] = [None] * len(series["values"])
        for comparison in motion["comparisons"]:
            summary = comparison["shared_metrics"][0]
            summary.update({"count": 0, "reference": None, "comparison": None, "difference": None})
        del motion["comparisons"][0]["shared_metrics"][0]["difference"]
        self.replace_motion_artifact(motion)
        with self.assertRaisesRegex(BundleError, "unexpected shared motion metric"):
            import_bundles(self.db, [self.bundle])
        self.assertEqual(LibraryService(self.db).list_sessions().total, 0)
