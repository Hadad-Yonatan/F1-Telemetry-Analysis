"""Deterministic cross-circuit validation for recorded telemetry captures."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from f1_telemetry.lap_analysis import AnalysisResult, analyze_capture


SCHEMA_VERSION = 1
FINISH_DELTA_TOLERANCE_S = 1e-9
_CASE_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class ValidationInputError(ValueError):
    """An input error that prevents the validation suite from running."""


@dataclass(frozen=True)
class QualityGates:
    minimum_matched_region_coverage: float
    minimum_event_evidence_coverage: float
    maximum_unmatched_event_rate: float


@dataclass(frozen=True)
class ValidationCase:
    case_id: str
    capture_file: str
    sha256: str
    size_bytes: int
    track_id: int
    track_length_m: int
    expected_capture_datagrams: int
    expected_normalized_samples: int
    expected_eligible_laps: tuple[int, ...]
    expected_reference_lap: int
    expected_comparison_laps: tuple[int, ...]


@dataclass(frozen=True)
class ValidationSuite:
    schema_version: int
    suite_id: str
    quality_gates: QualityGates
    cases: tuple[ValidationCase, ...]


@dataclass(frozen=True)
class ValidationMetrics:
    loss_region_count: int
    matched_region_count: int
    matched_region_coverage: float
    event_evidence_region_count: int
    event_evidence_coverage: float
    total_event_count: int
    unmatched_event_count: int
    unmatched_event_rate: float
    unassociated_region_count: int


@dataclass(frozen=True)
class ValidationFailure:
    category: str
    check: str
    expected: object
    actual: object
    message: str


@dataclass(frozen=True)
class CaseObservations:
    capture_sha256: str
    capture_size_bytes: int
    track_id: int
    track_length_m: int
    capture_datagrams: int
    normalized_samples: int
    eligible_laps: tuple[int, ...]
    reference_lap: int
    comparison_laps: tuple[int, ...]
    decode_errors: int
    unmatched_lap_packets: int
    unmatched_telemetry_packets: int
    finish_delta_errors_s: tuple[float, ...]


@dataclass(frozen=True)
class CaseValidationResult:
    case: ValidationCase
    observations: CaseObservations
    metrics: ValidationMetrics
    algorithm_version: str
    failures: tuple[ValidationFailure, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class SuiteValidationResult:
    suite: ValidationSuite
    cases: tuple[CaseValidationResult, ...]

    @property
    def passed(self) -> bool:
        return all(case.passed for case in self.cases)


_SUITE_FIELDS = {"schema_version", "suite_id", "quality_gates", "cases"}
_GATE_FIELDS = {
    "minimum_matched_region_coverage",
    "minimum_event_evidence_coverage",
    "maximum_unmatched_event_rate",
}
_CASE_FIELDS = {
    "case_id",
    "capture_file",
    "sha256",
    "size_bytes",
    "track_id",
    "track_length_m",
    "expected_capture_datagrams",
    "expected_normalized_samples",
    "expected_eligible_laps",
    "expected_reference_lap",
    "expected_comparison_laps",
}


def _strict_object(value: object, fields: set[str], context: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValidationInputError(f"{context} must be an object")
    keys = set(value)
    unknown = sorted(keys - fields)
    missing = sorted(fields - keys)
    if unknown:
        raise ValidationInputError(f"{context} has unknown fields: {', '.join(unknown)}")
    if missing:
        raise ValidationInputError(f"{context} is missing fields: {', '.join(missing)}")
    return value


def _string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationInputError(f"{context} must be a non-empty string")
    return value


def _integer(value: object, context: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValidationInputError(f"{context} must be an integer >= {minimum}")
    return value


def _rate(value: object, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationInputError(f"{context} must be a number between 0 and 1")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ValidationInputError(f"{context} must be a number between 0 and 1")
    return result


def _lap_list(value: object, context: str) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ValidationInputError(f"{context} must be an array")
    laps = tuple(_integer(item, context, minimum=1) for item in value)
    if len(laps) != len(set(laps)):
        raise ValidationInputError(f"{context} cannot contain duplicates")
    if laps != tuple(sorted(laps)):
        raise ValidationInputError(f"{context} must be sorted")
    return laps


def _parse_case(value: object, index: int) -> ValidationCase:
    context = f"cases[{index}]"
    document = _strict_object(value, _CASE_FIELDS, context)
    case_id = _string(document["case_id"], f"{context}.case_id")
    if not _CASE_ID_PATTERN.fullmatch(case_id):
        raise ValidationInputError(
            f"{context}.case_id must contain only lowercase letters, digits, and hyphens"
        )
    capture_file = _string(document["capture_file"], f"{context}.capture_file")
    if capture_file in {".", ".."} or Path(capture_file).name != capture_file:
        raise ValidationInputError(f"{context}.capture_file must be a safe basename")
    if "/" in capture_file or "\\" in capture_file:
        raise ValidationInputError(f"{context}.capture_file must be a safe basename")
    sha256 = _string(document["sha256"], f"{context}.sha256")
    if not _SHA256_PATTERN.fullmatch(sha256):
        raise ValidationInputError(f"{context}.sha256 must be 64 lowercase hex characters")
    eligible = _lap_list(document["expected_eligible_laps"], f"{context}.expected_eligible_laps")
    comparisons = _lap_list(
        document["expected_comparison_laps"],
        f"{context}.expected_comparison_laps",
    )
    reference = _integer(
        document["expected_reference_lap"],
        f"{context}.expected_reference_lap",
        minimum=1,
    )
    if reference not in eligible:
        raise ValidationInputError(f"{context}.expected_reference_lap must be eligible")
    if reference in comparisons:
        raise ValidationInputError(f"{context}.expected_comparison_laps includes the reference")
    if tuple(lap for lap in eligible if lap != reference) != comparisons:
        raise ValidationInputError(
            f"{context}.expected_comparison_laps must contain every non-reference eligible lap"
        )
    return ValidationCase(
        case_id=case_id,
        capture_file=capture_file,
        sha256=sha256,
        size_bytes=_integer(document["size_bytes"], f"{context}.size_bytes", minimum=1),
        track_id=_integer(document["track_id"], f"{context}.track_id"),
        track_length_m=_integer(
            document["track_length_m"], f"{context}.track_length_m", minimum=1
        ),
        expected_capture_datagrams=_integer(
            document["expected_capture_datagrams"],
            f"{context}.expected_capture_datagrams",
            minimum=1,
        ),
        expected_normalized_samples=_integer(
            document["expected_normalized_samples"],
            f"{context}.expected_normalized_samples",
            minimum=1,
        ),
        expected_eligible_laps=eligible,
        expected_reference_lap=reference,
        expected_comparison_laps=comparisons,
    )


def parse_validation_suite(document: object) -> ValidationSuite:
    """Parse and strictly validate a manifest document."""
    root = _strict_object(document, _SUITE_FIELDS, "manifest")
    schema_version = _integer(root["schema_version"], "manifest.schema_version", minimum=1)
    if schema_version != SCHEMA_VERSION:
        raise ValidationInputError(f"unsupported manifest schema version: {schema_version}")
    suite_id = _string(root["suite_id"], "manifest.suite_id")
    gates_document = _strict_object(root["quality_gates"], _GATE_FIELDS, "quality_gates")
    gates = QualityGates(
        minimum_matched_region_coverage=_rate(
            gates_document["minimum_matched_region_coverage"],
            "quality_gates.minimum_matched_region_coverage",
        ),
        minimum_event_evidence_coverage=_rate(
            gates_document["minimum_event_evidence_coverage"],
            "quality_gates.minimum_event_evidence_coverage",
        ),
        maximum_unmatched_event_rate=_rate(
            gates_document["maximum_unmatched_event_rate"],
            "quality_gates.maximum_unmatched_event_rate",
        ),
    )
    raw_cases = root["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValidationInputError("manifest.cases must be a non-empty array")
    cases = tuple(
        sorted(
            (
                _parse_case(value, index)
                for index, value in enumerate(raw_cases)
            ),
            key=lambda case: case.case_id,
        )
    )
    case_ids = [case.case_id for case in cases]
    capture_files = [case.capture_file for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValidationInputError("manifest contains duplicate case IDs")
    if len(capture_files) != len(set(capture_files)):
        raise ValidationInputError("manifest contains duplicate capture filenames")
    return ValidationSuite(schema_version, suite_id, gates, cases)


def load_validation_suite(path: str | Path) -> ValidationSuite:
    """Load a strict validation manifest from disk."""
    try:
        with Path(path).open(encoding="utf-8") as file:
            document = json.load(
                file,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValidationInputError(f"invalid JSON number: {value}")
                ),
            )
    except ValidationInputError:
        raise
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationInputError(f"cannot read manifest: {error}") from error
    return parse_validation_suite(document)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise ValidationInputError(f"cannot read capture {path.name}: {error}") from error
    return digest.hexdigest()


def preflight_captures(suite: ValidationSuite, capture_dir: str | Path) -> tuple[Path, ...]:
    """Resolve all inputs and verify immutable capture identity before analysis."""
    directory = Path(capture_dir)
    paths = []
    for case in suite.cases:
        path = directory / case.capture_file
        if not path.is_file():
            raise ValidationInputError(f"capture is missing: {case.capture_file}")
        actual_sha256 = _sha256(path)
        if actual_sha256 != case.sha256:
            raise ValidationInputError(
                f"capture hash mismatch for {case.capture_file}: "
                f"expected {case.sha256}, got {actual_sha256}"
            )
        try:
            size = path.stat().st_size
        except OSError as error:
            raise ValidationInputError(
                f"cannot inspect capture {case.capture_file}: {error}"
            ) from error
        if size != case.size_bytes:
            raise ValidationInputError(
                f"capture size mismatch for {case.capture_file}: "
                f"expected {case.size_bytes}, got {size}"
            )
        paths.append(path)
    return tuple(paths)


def calculate_validation_metrics(result: AnalysisResult) -> ValidationMetrics:
    """Calculate suite quality metrics from an already-computed analysis."""
    regions = tuple(region for comparison in result.comparisons for region in comparison.regions)
    matched = sum(region.matched_event_index is not None for region in regions)
    evidenced = sum(
        region.matched_event_index is not None
        or bool(region.unmatched_reference_event_indices)
        or bool(region.unmatched_comparison_event_indices)
        for region in regions
    )
    region_count = len(regions)
    total_events = sum(
        len(comparison.reference_events) + len(comparison.comparison_events)
        for comparison in result.comparisons
    )
    unmatched_events = sum(
        len(comparison.unmatched_reference_event_indices)
        + len(comparison.unmatched_comparison_event_indices)
        for comparison in result.comparisons
    )
    return ValidationMetrics(
        loss_region_count=region_count,
        matched_region_count=matched,
        matched_region_coverage=matched / region_count if region_count else 1.0,
        event_evidence_region_count=evidenced,
        event_evidence_coverage=evidenced / region_count if region_count else 1.0,
        total_event_count=total_events,
        unmatched_event_count=unmatched_events,
        unmatched_event_rate=unmatched_events / total_events if total_events else 0.0,
        unassociated_region_count=region_count - evidenced,
    )


def _observations(result: AnalysisResult) -> CaseObservations:
    normalization = result.session.normalization
    return CaseObservations(
        capture_sha256=result.capture.sha256,
        capture_size_bytes=result.capture.size_bytes,
        track_id=result.session.track_id,
        track_length_m=result.session.track_length,
        capture_datagrams=normalization.capture_datagrams,
        normalized_samples=normalization.samples_joined,
        eligible_laps=tuple(
            sorted(
                lap.lap_number
                for lap in result.session.validations
                if lap.eligible
            )
        ),
        reference_lap=result.reference_lap,
        comparison_laps=tuple(comparison.comparison_lap for comparison in result.comparisons),
        decode_errors=normalization.decode_errors,
        unmatched_lap_packets=normalization.unmatched_lap_packets,
        unmatched_telemetry_packets=normalization.unmatched_telemetry_packets,
        finish_delta_errors_s=tuple(
            comparison.final_delta_error_s
            for comparison in result.comparisons
        ),
    )


def _failure(category: str, check: str, expected: object, actual: object) -> ValidationFailure:
    stable_actual = (
        "non-finite"
        if isinstance(actual, float) and not math.isfinite(actual)
        else actual
    )
    return ValidationFailure(
        category=category,
        check=check,
        expected=expected,
        actual=stable_actual,
        message=f"{check}: expected {expected!r}, got {stable_actual!r}",
    )


def evaluate_case(
    case: ValidationCase,
    gates: QualityGates,
    result: AnalysisResult,
) -> CaseValidationResult:
    """Evaluate exact invariants and quality gates for one completed analysis."""
    observed = _observations(result)
    metrics = calculate_validation_metrics(result)
    failures = []
    exact_checks = (
        ("capture_sha256", case.sha256, observed.capture_sha256),
        ("capture_size_bytes", case.size_bytes, observed.capture_size_bytes),
        ("track_id", case.track_id, observed.track_id),
        ("track_length_m", case.track_length_m, observed.track_length_m),
        ("capture_datagrams", case.expected_capture_datagrams, observed.capture_datagrams),
        ("normalized_samples", case.expected_normalized_samples, observed.normalized_samples),
        ("eligible_laps", case.expected_eligible_laps, observed.eligible_laps),
        ("reference_lap", case.expected_reference_lap, observed.reference_lap),
        ("comparison_laps", case.expected_comparison_laps, observed.comparison_laps),
        ("decode_errors", 0, observed.decode_errors),
        ("unmatched_lap_packets", 0, observed.unmatched_lap_packets),
        ("unmatched_telemetry_packets", 0, observed.unmatched_telemetry_packets),
    )
    for check, expected, actual in exact_checks:
        if actual != expected:
            failures.append(_failure("core", check, expected, actual))
    for index, error in enumerate(observed.finish_delta_errors_s):
        if not math.isfinite(error) or abs(error) > FINISH_DELTA_TOLERANCE_S:
            failures.append(
                _failure(
                    "core",
                    f"finish_delta_error_s[{index}]",
                    f"absolute value <= {FINISH_DELTA_TOLERANCE_S}",
                    error,
                )
            )
    quality_checks = (
        (
            "matched_region_coverage",
            f">= {gates.minimum_matched_region_coverage}",
            metrics.matched_region_coverage,
            metrics.matched_region_coverage >= gates.minimum_matched_region_coverage,
        ),
        (
            "event_evidence_coverage",
            f">= {gates.minimum_event_evidence_coverage}",
            metrics.event_evidence_coverage,
            metrics.event_evidence_coverage >= gates.minimum_event_evidence_coverage,
        ),
        (
            "unmatched_event_rate",
            f"<= {gates.maximum_unmatched_event_rate}",
            metrics.unmatched_event_rate,
            metrics.unmatched_event_rate <= gates.maximum_unmatched_event_rate,
        ),
    )
    for check, expected, actual, passed in quality_checks:
        if not passed:
            failures.append(_failure("quality", check, expected, actual))
    return CaseValidationResult(
        case=case,
        observations=observed,
        metrics=metrics,
        algorithm_version=result.config.algorithm_version,
        failures=tuple(failures),
    )


def validate_suite(
    suite: ValidationSuite,
    capture_dir: str | Path,
    *,
    analyzer: Callable[[Path], AnalysisResult] | None = None,
) -> SuiteValidationResult:
    """Analyze and validate every case after capture identity preflight."""
    paths = preflight_captures(suite, capture_dir)
    analyze = analyzer or analyze_capture
    results = []
    for case, path in zip(suite.cases, paths):
        try:
            analysis = analyze(path)
        except (OSError, ValueError) as error:
            raise ValidationInputError(
                f"cannot analyze capture {case.capture_file}: {error}"
            ) from error
        if analysis.capture.sha256 != case.sha256:
            raise ValidationInputError(
                f"capture hash changed during analysis for {case.capture_file}"
            )
        if analysis.capture.size_bytes != case.size_bytes:
            raise ValidationInputError(
                f"capture size changed during analysis for {case.capture_file}"
            )
        results.append(evaluate_case(case, suite.quality_gates, analysis))
    return SuiteValidationResult(suite=suite, cases=tuple(results))


def candidate_case(path: str | Path, case_id: str) -> ValidationCase:
    """Analyze one capture and return a candidate manifest case."""
    if not _CASE_ID_PATTERN.fullmatch(case_id):
        raise ValidationInputError(
            "case ID must contain only lowercase letters, digits, and hyphens"
        )
    capture_path = Path(path)
    if not capture_path.is_file():
        raise ValidationInputError(f"capture is missing: {capture_path}")
    try:
        result = analyze_capture(capture_path)
    except (OSError, ValueError) as error:
        raise ValidationInputError(
            f"cannot analyze capture {capture_path.name}: {error}"
        ) from error
    observed = _observations(result)
    return ValidationCase(
        case_id=case_id,
        capture_file=capture_path.name,
        sha256=result.capture.sha256,
        size_bytes=result.capture.size_bytes,
        track_id=result.session.track_id,
        track_length_m=result.session.track_length,
        expected_capture_datagrams=observed.capture_datagrams,
        expected_normalized_samples=observed.normalized_samples,
        expected_eligible_laps=observed.eligible_laps,
        expected_reference_lap=observed.reference_lap,
        expected_comparison_laps=observed.comparison_laps,
    )


def _case_document(case: ValidationCase) -> dict[str, object]:
    return {
        "case_id": case.case_id,
        "capture_file": case.capture_file,
        "sha256": case.sha256,
        "size_bytes": case.size_bytes,
        "track_id": case.track_id,
        "track_length_m": case.track_length_m,
        "expected_capture_datagrams": case.expected_capture_datagrams,
        "expected_normalized_samples": case.expected_normalized_samples,
        "expected_eligible_laps": list(case.expected_eligible_laps),
        "expected_reference_lap": case.expected_reference_lap,
        "expected_comparison_laps": list(case.expected_comparison_laps),
    }


def _metrics_document(metrics: ValidationMetrics) -> dict[str, object]:
    return {
        "loss_region_count": metrics.loss_region_count,
        "matched_region_count": metrics.matched_region_count,
        "matched_region_coverage": round(metrics.matched_region_coverage, 6),
        "event_evidence_region_count": metrics.event_evidence_region_count,
        "event_evidence_coverage": round(metrics.event_evidence_coverage, 6),
        "total_event_count": metrics.total_event_count,
        "unmatched_event_count": metrics.unmatched_event_count,
        "unmatched_event_rate": round(metrics.unmatched_event_rate, 6),
        "unassociated_region_count": metrics.unassociated_region_count,
    }


def _rounded_finite(value: float, digits: int) -> float | str:
    return round(value, digits) if math.isfinite(value) else "non-finite"


def validation_document(result: SuiteValidationResult) -> dict[str, object]:
    """Return the stable machine-readable validation document."""
    return {
        "schema_version": result.suite.schema_version,
        "suite_id": result.suite.suite_id,
        "passed": result.passed,
        "finish_delta_tolerance_s": FINISH_DELTA_TOLERANCE_S,
        "quality_gates": {
            "minimum_matched_region_coverage": (
                result.suite.quality_gates.minimum_matched_region_coverage
            ),
            "minimum_event_evidence_coverage": (
                result.suite.quality_gates.minimum_event_evidence_coverage
            ),
            "maximum_unmatched_event_rate": result.suite.quality_gates.maximum_unmatched_event_rate,
        },
        "cases": [
            {
                "case_id": case_result.case.case_id,
                "capture_file": case_result.case.capture_file,
                "passed": case_result.passed,
                "algorithm_version": case_result.algorithm_version,
                "expected": _case_document(case_result.case),
                "actual": {
                    "capture_sha256": case_result.observations.capture_sha256,
                    "capture_size_bytes": case_result.observations.capture_size_bytes,
                    "track_id": case_result.observations.track_id,
                    "track_length_m": case_result.observations.track_length_m,
                    "capture_datagrams": case_result.observations.capture_datagrams,
                    "normalized_samples": case_result.observations.normalized_samples,
                    "eligible_laps": list(case_result.observations.eligible_laps),
                    "reference_lap": case_result.observations.reference_lap,
                    "comparison_laps": list(case_result.observations.comparison_laps),
                    "decode_errors": case_result.observations.decode_errors,
                    "unmatched_lap_packets": case_result.observations.unmatched_lap_packets,
                    "unmatched_telemetry_packets": (
                        case_result.observations.unmatched_telemetry_packets
                    ),
                    "finish_delta_errors_s": [
                        _rounded_finite(value, 12)
                        for value in case_result.observations.finish_delta_errors_s
                    ],
                },
                "metrics": _metrics_document(case_result.metrics),
                "failures": [
                    {
                        "category": failure.category,
                        "check": failure.check,
                        "expected": failure.expected,
                        "actual": (
                            _rounded_finite(failure.actual, 12)
                            if isinstance(failure.actual, float)
                            else failure.actual
                        ),
                        "message": failure.message,
                    }
                    for failure in case_result.failures
                ],
            }
            for case_result in result.cases
        ],
    }


def render_validation_markdown(result: SuiteValidationResult) -> str:
    """Render the deterministic human-readable suite report."""
    lines = [
        "# Cross-Circuit Validation",
        "",
        f"Suite: `{result.suite.suite_id}`",
        f"Overall result: **{'PASS' if result.passed else 'FAIL'}**",
        "",
        "## Quality gates",
        "",
        "- Matched-region coverage: at least "
        f"{result.suite.quality_gates.minimum_matched_region_coverage:.2f}",
        "- Event-evidence coverage: at least "
        f"{result.suite.quality_gates.minimum_event_evidence_coverage:.2f}",
        "- Unmatched-event rate: at most "
        f"{result.suite.quality_gates.maximum_unmatched_event_rate:.2f}",
        f"- Finish-delta absolute error: at most {FINISH_DELTA_TOLERANCE_S:.9f} s",
        "",
        "## Cases",
        "",
        "| Case | Track | Eligible laps | Reference | Comparisons | Result |",
        "| --- | ---: | --- | ---: | --- | --- |",
    ]
    for case_result in result.cases:
        observed = case_result.observations
        lines.append(
            f"| {case_result.case.case_id} | {observed.track_id} | "
            f"{', '.join(map(str, observed.eligible_laps))} | {observed.reference_lap} | "
            f"{', '.join(map(str, observed.comparison_laps))} | "
            f"{'PASS' if case_result.passed else 'FAIL'} |"
        )
    for case_result in result.cases:
        observed = case_result.observations
        metrics = case_result.metrics
        maximum_finish_error_value = max(
            (abs(value) for value in observed.finish_delta_errors_s),
            default=0.0,
        )
        maximum_finish_error = (
            "non-finite"
            if any(not math.isfinite(value) for value in observed.finish_delta_errors_s)
            else f"{maximum_finish_error_value:.12f} s"
        )
        lines.extend(
            [
                "",
                f"### {case_result.case.case_id}",
                "",
                f"- Capture: `{case_result.case.capture_file}`",
                f"- Capture identity: `{observed.capture_sha256}`, "
                f"{observed.capture_size_bytes} bytes",
                f"- Track: ID {observed.track_id}, {observed.track_length_m} m",
                "- Datagrams / normalized samples: "
                f"{observed.capture_datagrams} / {observed.normalized_samples}",
                "- Decode / unmatched lap / unmatched telemetry packets: "
                f"{observed.decode_errors} / {observed.unmatched_lap_packets} / "
                f"{observed.unmatched_telemetry_packets}",
                f"- Maximum finish-delta error: {maximum_finish_error}",
                f"- Loss regions: {metrics.loss_region_count}; matched coverage "
                f"{metrics.matched_region_coverage:.6f}; event-evidence coverage "
                f"{metrics.event_evidence_coverage:.6f}",
                f"- Events: {metrics.unmatched_event_count} unmatched of "
                f"{metrics.total_event_count}; rate "
                f"{metrics.unmatched_event_rate:.6f}",
                f"- Unassociated regions: {metrics.unassociated_region_count}",
                f"- Algorithm version: `{case_result.algorithm_version}`",
            ]
        )
        if case_result.failures:
            lines.extend(["", "Failures:", ""])
            lines.extend(
                f"- [{failure.category}] {failure.message}"
                for failure in case_result.failures
            )
        else:
            lines.extend(["", "All configured checks passed."])
    return "\n".join(lines) + "\n"


MarkdownRenderer = Callable[[SuiteValidationResult], str]


def write_validation_bundle(
    result: SuiteValidationResult,
    output_dir: str | Path,
    *,
    markdown_renderer: MarkdownRenderer = render_validation_markdown,
) -> None:
    """Atomically create validation.json and validation.md without overwriting."""
    output = Path(output_dir)
    if output.exists():
        raise FileExistsError(f"output directory already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
    try:
        with (temporary / "validation.json").open("x", encoding="utf-8", newline="\n") as file:
            json.dump(validation_document(result), file, allow_nan=False, indent=2, sort_keys=True)
            file.write("\n")
        markdown = markdown_renderer(result)
        with (temporary / "validation.md").open("x", encoding="utf-8", newline="\n") as file:
            file.write(markdown)
        if output.exists():
            raise FileExistsError(f"output directory already exists: {output}")
        temporary.rename(output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate telemetry captures across circuits")
    subparsers = parser.add_subparsers(dest="command", required=True)
    baseline = subparsers.add_parser("baseline", help="print a candidate manifest case")
    baseline.add_argument("capture", type=Path)
    baseline.add_argument("--case-id", required=True)
    run = subparsers.add_parser("run", help="run every case in a validation manifest")
    run.add_argument("manifest", type=Path)
    run.add_argument("--capture-dir", required=True, type=Path)
    run.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "baseline":
            case = candidate_case(args.capture, args.case_id)
            json.dump(_case_document(case), sys.stdout, allow_nan=False, indent=2, sort_keys=True)
            sys.stdout.write("\n")
            return 0
        if args.output_dir.exists():
            raise ValidationInputError(f"output directory already exists: {args.output_dir}")
        suite = load_validation_suite(args.manifest)
        result = validate_suite(suite, args.capture_dir)
        write_validation_bundle(result, args.output_dir)
        return 0 if result.passed else 1
    except (ValidationInputError, FileExistsError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
