import hashlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from f1_telemetry.session_policy import PolicySelection
from f1_telemetry.validation import (
    QualityGates,
    SuiteValidationResult,
    ValidationInputError,
    calculate_validation_metrics,
    evaluate_case,
    load_validation_suite,
    main,
    parse_validation_suite,
    validate_suite,
    write_validation_bundle,
)


def manifest_case(**changes: object) -> dict[str, object]:
    document: dict[str, object] = {
        "case_id": "test-track",
        "capture_file": "test.f1cap",
        "sha256": hashlib.sha256(b"capture").hexdigest(),
        "size_bytes": len(b"capture"),
        "track_id": 10,
        "track_length_m": 100,
        "expected_capture_datagrams": 20,
        "expected_normalized_samples": 12,
        "expected_eligible_laps": [1, 2],
        "expected_reference_lap": 1,
        "expected_comparison_laps": [2],
    }
    document.update(changes)
    return document


def manifest_document(*cases: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "suite_id": "test-suite",
        "quality_gates": {
            "minimum_matched_region_coverage": 0.65,
            "minimum_event_evidence_coverage": 0.80,
            "maximum_unmatched_event_rate": 0.30,
        },
        "cases": list(cases or (manifest_case(),)),
    }


def region(*, matched: bool = False, one_sided: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        matched_event_index=0 if matched else None,
        unmatched_reference_event_indices=(0,) if one_sided else (),
        unmatched_comparison_event_indices=(),
    )


def analysis_result(
    *,
    regions: tuple[SimpleNamespace, ...] = (
        region(matched=True),
        region(matched=True),
        region(one_sided=True),
    ),
    reference_events: int = 2,
    comparison_events: int = 2,
    unmatched_reference: tuple[int, ...] = (),
    unmatched_comparison: tuple[int, ...] = (0,),
    finish_error: float = 0.0,
) -> SimpleNamespace:
    comparison = SimpleNamespace(
        comparison_lap=2,
        regions=regions,
        reference_events=tuple(range(reference_events)),
        comparison_events=tuple(range(comparison_events)),
        unmatched_reference_event_indices=unmatched_reference,
        unmatched_comparison_event_indices=unmatched_comparison,
        final_delta_error_s=finish_error,
    )
    normalization = SimpleNamespace(
        capture_datagrams=20,
        samples_joined=12,
        decode_errors=0,
        unmatched_lap_packets=0,
        unmatched_telemetry_packets=0,
    )
    validations = (
        SimpleNamespace(lap_number=1, eligible=True),
        SimpleNamespace(lap_number=2, eligible=True),
    )
    return SimpleNamespace(
        config=SimpleNamespace(algorithm_version="1.1.0"),
        session_context=None,
        capture=SimpleNamespace(
            sha256=hashlib.sha256(b"capture").hexdigest(),
            size_bytes=len(b"capture"),
        ),
        session=SimpleNamespace(
            track_id=10,
            track_length=100,
            normalization=normalization,
            validations=validations,
            policy=PolicySelection("time-trial", "validated", "1.0.0", 18),
        ),
        reference_lap=1,
        comparisons=(comparison,),
    )


class ManifestTests(unittest.TestCase):
    def test_parses_strict_manifest_and_orders_cases(self) -> None:
        second = manifest_case(
            case_id="z-track",
            capture_file="z.f1cap",
            sha256="1" * 64,
        )
        first = manifest_case(
            case_id="a-track",
            capture_file="a.f1cap",
            sha256="2" * 64,
        )

        suite = parse_validation_suite(manifest_document(second, first))

        self.assertEqual([case.case_id for case in suite.cases], ["a-track", "z-track"])
        self.assertEqual(suite.quality_gates.minimum_matched_region_coverage, 0.65)

    def test_rejects_unknown_fields(self) -> None:
        document = manifest_document()
        document["unexpected"] = True
        with self.assertRaisesRegex(ValidationInputError, "unknown fields"):
            parse_validation_suite(document)

    def test_rejects_duplicate_case_ids_and_capture_files(self) -> None:
        duplicate_id = manifest_case(
            capture_file="other.f1cap",
            sha256="3" * 64,
        )
        with self.assertRaisesRegex(ValidationInputError, "duplicate case IDs"):
            parse_validation_suite(manifest_document(manifest_case(), duplicate_id))

        duplicate_file = manifest_case(case_id="other-track")
        with self.assertRaisesRegex(ValidationInputError, "duplicate capture filenames"):
            parse_validation_suite(manifest_document(manifest_case(), duplicate_file))

    def test_rejects_unsafe_capture_filenames(self) -> None:
        for filename in ("../test.f1cap", "nested/test.f1cap", "nested\\test.f1cap"):
            with self.subTest(filename=filename):
                with self.assertRaisesRegex(ValidationInputError, "safe basename"):
                    parse_validation_suite(
                        manifest_document(manifest_case(capture_file=filename))
                    )

    def test_load_rejects_non_finite_json_number(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            path.write_text(
                json.dumps(manifest_document()).replace("0.65", "NaN"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationInputError, "invalid JSON number"):
                load_validation_suite(path)


class ValidationMetricTests(unittest.TestCase):
    def test_calculates_region_and_event_metrics(self) -> None:
        metrics = calculate_validation_metrics(analysis_result())

        self.assertEqual(metrics.loss_region_count, 3)
        self.assertEqual(metrics.matched_region_count, 2)
        self.assertAlmostEqual(metrics.matched_region_coverage, 2 / 3)
        self.assertEqual(metrics.event_evidence_region_count, 3)
        self.assertEqual(metrics.event_evidence_coverage, 1.0)
        self.assertEqual(metrics.unmatched_event_rate, 0.25)
        self.assertEqual(metrics.unassociated_region_count, 0)

    def test_zero_denominators_have_neutral_values(self) -> None:
        metrics = calculate_validation_metrics(
            analysis_result(
                regions=(),
                reference_events=0,
                comparison_events=0,
                unmatched_comparison=(),
            )
        )

        self.assertEqual(metrics.matched_region_coverage, 1.0)
        self.assertEqual(metrics.event_evidence_coverage, 1.0)
        self.assertEqual(metrics.unmatched_event_rate, 0.0)

    def test_gate_boundaries_are_inclusive(self) -> None:
        result = analysis_result(
            regions=(region(matched=True), region(matched=True), region(one_sided=True)),
            reference_events=5,
            comparison_events=5,
            unmatched_reference=(0, 1),
            unmatched_comparison=(0,),
        )
        case = parse_validation_suite(manifest_document()).cases[0]
        gates = QualityGates(2 / 3, 1.0, 0.3)

        evaluated = evaluate_case(case, gates, result)

        self.assertTrue(evaluated.passed)

    def test_records_exact_and_quality_failures(self) -> None:
        result = analysis_result(regions=(region(),), finish_error=2e-9)
        result.session.track_id = 11
        case = parse_validation_suite(manifest_document()).cases[0]

        evaluated = evaluate_case(case, QualityGates(0.65, 0.8, 0.3), result)

        checks = {failure.check for failure in evaluated.failures}
        self.assertIn("track_id", checks)
        self.assertIn("finish_delta_error_s[0]", checks)
        self.assertIn("matched_region_coverage", checks)
        self.assertIn("event_evidence_coverage", checks)


class ValidationRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.capture = self.root / "test.f1cap"
        self.capture.write_bytes(b"capture")
        self.suite = parse_validation_suite(manifest_document())

    def test_missing_file_and_hash_mismatch_abort_before_analysis(self) -> None:
        missing = self.root / "missing"
        with self.assertRaisesRegex(ValidationInputError, "capture is missing"):
            validate_suite(self.suite, missing, analyzer=lambda _path: analysis_result())

        self.capture.write_bytes(b"changed")
        with self.assertRaisesRegex(ValidationInputError, "hash mismatch"):
            validate_suite(self.suite, self.root, analyzer=lambda _path: analysis_result())

    def test_suite_writes_byte_identical_ordered_bundles(self) -> None:
        result = validate_suite(
            self.suite,
            self.root,
            analyzer=lambda _path: analysis_result(),
        )
        first = self.root / "first"
        second = self.root / "second"

        write_validation_bundle(result, first)
        write_validation_bundle(result, second)

        self.assertEqual(
            {path.name for path in first.iterdir()},
            {"validation.json", "validation.md"},
        )
        for name in ("validation.json", "validation.md"):
            self.assertEqual((first / name).read_bytes(), (second / name).read_bytes())
            self.assertTrue((first / name).read_bytes().endswith(b"\n"))
        document = json.loads((first / "validation.json").read_text(encoding="utf-8"))
        self.assertTrue(document["passed"])
        self.assertEqual(document["cases"][0]["metrics"]["loss_region_count"], 3)
        self.assertEqual(document["cases"][0]["actual"]["session_policy"]["status"], "validated")
        self.assertNotIn("timestamp", (first / "validation.json").read_text(encoding="utf-8"))

    def test_output_failure_removes_temporary_and_final_directories(self) -> None:
        result = validate_suite(
            self.suite,
            self.root,
            analyzer=lambda _path: analysis_result(),
        )
        output = self.root / "failed"

        def fail(_result: SuiteValidationResult) -> str:
            raise RuntimeError("injected report failure")

        with self.assertRaisesRegex(RuntimeError, "injected report failure"):
            write_validation_bundle(result, output, markdown_renderer=fail)

        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".failed.tmp-*")), [])

    def test_refuses_to_overwrite_existing_bundle(self) -> None:
        result = validate_suite(
            self.suite,
            self.root,
            analyzer=lambda _path: analysis_result(),
        )
        output = self.root / "existing"
        output.mkdir()

        with self.assertRaisesRegex(FileExistsError, "already exists"):
            write_validation_bundle(result, output)

    def test_baseline_prints_candidate_case(self) -> None:
        candidate = self.suite.cases[0]
        stdout = io.StringIO()
        with (
            patch("f1_telemetry.validation.candidate_case", return_value=candidate),
            patch("sys.stdout", stdout),
        ):
            exit_code = main(
                ["baseline", str(self.capture), "--case-id", "test-track"]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["case_id"], "test-track")
        self.assertTrue(stdout.getvalue().endswith("\n"))

    def test_main_exit_codes_and_bundle_behavior(self) -> None:
        manifest = self.root / "manifest.json"
        manifest.write_text(
            json.dumps(manifest_document(), indent=2) + "\n",
            encoding="utf-8",
        )
        passing = validate_suite(
            self.suite,
            self.root,
            analyzer=lambda _path: analysis_result(),
        )
        failing_case = replace(
            passing.cases[0],
            failures=passing.cases[0].failures
            + (
                SimpleNamespace(
                    category="core",
                    check="test",
                    expected=0,
                    actual=1,
                    message="test failure",
                ),
            ),
        )
        failing = SuiteValidationResult(passing.suite, (failing_case,))

        with patch("f1_telemetry.validation.validate_suite", return_value=passing):
            self.assertEqual(
                main(
                    [
                        "run",
                        str(manifest),
                        "--capture-dir",
                        str(self.root),
                        "--output-dir",
                        str(self.root / "passing"),
                    ]
                ),
                0,
            )
        with patch("f1_telemetry.validation.validate_suite", return_value=failing):
            self.assertEqual(
                main(
                    [
                        "run",
                        str(manifest),
                        "--capture-dir",
                        str(self.root),
                        "--output-dir",
                        str(self.root / "failing"),
                    ]
                ),
                1,
            )
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            self.assertEqual(
                main(
                    [
                        "run",
                        str(self.root / "missing.json"),
                        "--capture-dir",
                        str(self.root),
                        "--output-dir",
                        str(self.root / "not-created"),
                    ]
                ),
                2,
            )
        self.assertFalse((self.root / "not-created").exists())


if __name__ == "__main__":
    unittest.main()
