"""Stable JSON, CSV, SVG, Markdown, and HTML analysis outputs."""

from __future__ import annotations

import csv
import json
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from f1_telemetry.bundle import write_bundle_manifest
from f1_telemetry.consistency import CONSISTENCY_FILENAME
from f1_telemetry.corner_catalog import CatalogSelection, TrackDefinition
from f1_telemetry.driving_events import DrivingEvent, MatchedDrivingEvent
from f1_telemetry.experimental_policy import comparison_context_document, lap_policy_context_document
from f1_telemetry.html_report import write_html_report
from f1_telemetry.lap_analysis import (
    AnalysisResult,
    LapComparison,
    LossRegion,
    PreparedSession,
)
from f1_telemetry.motion_analysis import MOTION_FILENAME, TRACK_MAP_FILENAME
from f1_telemetry.motion_render import track_map_svg
from f1_telemetry.plot import write_comparison_svg
from f1_telemetry.report import comparison_stem, write_markdown_report
from f1_telemetry.session_context import context_document

SvgWriter = Callable[[PreparedSession, LapComparison, Path], None]
ReportWriter = Callable[[AnalysisResult, Path], None]
HtmlWriter = Callable[[AnalysisResult, dict[str, object], Path], None]

CSV_COLUMNS = (
    "distance_m",
    "reference_elapsed_s",
    "comparison_elapsed_s",
    "delta_s",
    "reference_speed_kph",
    "comparison_speed_kph",
    "reference_throttle",
    "comparison_throttle",
    "reference_brake",
    "comparison_brake",
    "reference_steering",
    "comparison_steering",
    "reference_gear",
    "comparison_gear",
)


def _round_distance(value: float) -> float:
    return round(value, 3)


def _round_continuous(value: float) -> float:
    return round(value, 6)


def _optional_distance(value: float | None) -> float | None:
    return None if value is None else _round_distance(value)


def _event_document(event: DrivingEvent) -> dict[str, object]:
    return {
        "event_index": event.event_index,
        "braking_start_distance_m": _round_distance(
            event.braking_start_distance
        ),
        "braking_end_distance_m": _round_distance(event.braking_end_distance),
        "braking_distance_m": _round_distance(event.braking_distance),
        "peak_brake": _round_continuous(event.peak_brake),
        "minimum_speed_distance_m": _round_distance(
            event.minimum_speed_distance
        ),
        "minimum_speed_kph": _round_continuous(event.minimum_speed_kph),
        "throttle_reapplication_distance_m": _optional_distance(
            event.throttle_reapplication_distance
        ),
        "coast_distance_m": _round_distance(event.coast_distance),
        "analysis_end_distance_m": _round_distance(event.analysis_end_distance),
    }


def _event_match_document(match: MatchedDrivingEvent) -> dict[str, object]:
    differences = match.differences
    return {
        "match_index": match.match_index,
        "reference_event_index": match.reference.event_index,
        "comparison_event_index": match.comparison.event_index,
        "differences": {
            "braking_start_distance_m": _round_distance(
                differences.braking_start_distance
            ),
            "braking_end_distance_m": _round_distance(
                differences.braking_end_distance
            ),
            "braking_distance_m": _round_distance(
                differences.braking_distance
            ),
            "peak_brake": _round_continuous(differences.peak_brake),
            "minimum_speed_distance_m": _round_distance(
                differences.minimum_speed_distance
            ),
            "minimum_speed_kph": _round_continuous(
                differences.minimum_speed_kph
            ),
            "throttle_reapplication_distance_m": _optional_distance(
                differences.throttle_reapplication_distance
            ),
            "coast_distance_m": _round_distance(differences.coast_distance),
        },
        "evidence_labels": list(match.evidence_labels),
    }


def _region_document(region: LossRegion) -> dict[str, object]:
    return {
        "start_distance_m": _round_distance(region.start_distance),
        "end_distance_m": _round_distance(region.end_distance),
        "time_loss_s": _round_continuous(region.time_loss_s),
        "sector": region.sector,
        "matched_event_index": region.matched_event_index,
        "unmatched_reference_event_indices": list(
            region.unmatched_reference_event_indices
        ),
        "unmatched_comparison_event_indices": list(
            region.unmatched_comparison_event_indices
        ),
        "corner_context": (
            {
                "primary_turn_id": region.corner_context.primary_turn_id,
                "overlapping_turn_ids": list(
                    region.corner_context.overlapping_turn_ids
                ),
                "complex_ids": list(region.corner_context.complex_ids),
            }
            if region.corner_context is not None
            else None
        ),
        "evidence": {
            "labels": list(region.evidence_labels),
            "reference_min_speed_kph": _round_continuous(
                region.reference_min_speed_kph
            ),
            "comparison_min_speed_kph": _round_continuous(
                region.comparison_min_speed_kph
            ),
            "minimum_speed_difference_kph": _round_continuous(
                region.minimum_speed_difference_kph
            ),
            "reference_mean_throttle": _round_continuous(
                region.reference_mean_throttle
            ),
            "comparison_mean_throttle": _round_continuous(
                region.comparison_mean_throttle
            ),
            "mean_throttle_difference": _round_continuous(
                region.mean_throttle_difference
            ),
            "reference_mean_brake": _round_continuous(
                region.reference_mean_brake
            ),
            "comparison_mean_brake": _round_continuous(
                region.comparison_mean_brake
            ),
            "mean_brake_difference": _round_continuous(
                region.mean_brake_difference
            ),
        },
    }


def _track_document(track: TrackDefinition) -> dict[str, object]:
    return {
        "track_id": track.track_id,
        "display_name": track.display_name,
        "track_length_m": track.track_length_m,
        "turns": [
            {
                "turn_id": turn.turn_id,
                "number": turn.number,
                "display_name": turn.display_name,
                "direction": turn.direction,
                "entry_distance_m": _round_distance(turn.entry_distance_m),
                "apex_distance_m": _round_distance(turn.apex_distance_m),
                "exit_distance_m": _round_distance(turn.exit_distance_m),
            }
            for turn in track.turns
        ],
        "complexes": [
            {
                "complex_id": complex_value.complex_id,
                "display_name": complex_value.display_name,
                "turn_ids": list(complex_value.turn_ids),
            }
            for complex_value in track.complexes
        ],
    }


def _catalog_document(selection: CatalogSelection) -> dict[str, object]:
    metadata = selection.metadata
    return {
        "status": selection.status,
        "schema_version": metadata.schema_version if metadata is not None else None,
        "catalog_id": metadata.catalog_id if metadata is not None else None,
        "packet_format": metadata.packet_format if metadata is not None else None,
        "game_year": metadata.game_year if metadata is not None else None,
        "sha256": metadata.sha256 if metadata is not None else None,
        "selected_track": (
            _track_document(selection.track)
            if selection.track is not None
            else None
        ),
    }


def _comparison_document(comparison: LapComparison) -> dict[str, object]:
    document: dict[str, object] = {
        "reference_lap": comparison.reference_lap,
        "comparison_lap": comparison.comparison_lap,
        "reference_discarded_attempt_frames": (
            comparison.reference_discarded_attempt_frames
        ),
        "comparison_discarded_attempt_frames": (
            comparison.comparison_discarded_attempt_frames
        ),
        "sector_deltas_s": [
            _round_continuous(value) for value in comparison.sector_deltas_s
        ],
        "official_delta_s": _round_continuous(comparison.official_delta_s),
        "calculated_finish_delta_s": _round_continuous(
            comparison.calculated_final_delta_s
        ),
        "finish_delta_error_s": _round_continuous(
            comparison.final_delta_error_s
        ),
        "finish_delta_tolerance_s": 0.000000001,
        "finish_delta_verified": abs(comparison.final_delta_error_s) <= 1e-9,
        "driving_events": {
            "reference": [
                _event_document(event) for event in comparison.reference_events
            ],
            "comparison": [
                _event_document(event) for event in comparison.comparison_events
            ],
            "matches": [
                _event_match_document(match) for match in comparison.event_matches
            ],
            "unmatched_reference_event_indices": list(
                comparison.unmatched_reference_event_indices
            ),
            "unmatched_comparison_event_indices": list(
                comparison.unmatched_comparison_event_indices
            ),
        },
        "loss_regions": [
            _region_document(region) for region in comparison.regions
        ],
    }
    if comparison.policy_context is not None:
        document["policy_context"] = comparison_context_document(comparison.policy_context)
    return document


def analysis_document(result: AnalysisResult) -> dict[str, object]:
    """Return the canonical, deterministically rounded analysis document."""
    raw = result.consistency.json_bytes() if result.consistency is not None else None
    return _analysis_document(result, raw, _motion_artifacts(result))


def _motion_artifacts(result: AnalysisResult) -> tuple[bytes, bytes] | None:
    if result.motion is None:
        return None
    return result.motion.json_bytes(), track_map_svg(result.motion, {comparison.comparison_lap for comparison in result.comparisons})


def _analysis_document(
    result: AnalysisResult, consistency: bytes | None, motion: tuple[bytes, bytes] | None,
) -> dict[str, object]:
    config = result.config
    discarded_by_lap = {
        lap.lap_number: lap.discarded_attempt_frames for lap in result.session.laps
    }
    document: dict[str, object] = {
        "schema_version": config.schema_version,
        "session_context": (
            context_document(result.session_context) if result.session_context is not None else None
        ),
        "capture": asdict(result.capture),
        "corner_catalog": _catalog_document(result.catalog_selection),
        "algorithm": {
            "version": config.algorithm_version,
            "distance_step_m": _round_distance(config.distance_step_m),
            "loss_bin_size_m": _round_distance(config.loss_bin_size_m),
            "minimum_bin_loss_s": _round_continuous(
                config.minimum_bin_loss_s
            ),
            "minimum_region_loss_s": _round_continuous(
                config.minimum_region_loss_s
            ),
            "evidence_thresholds": {
                "minimum_speed_difference_kph": _round_continuous(
                    config.minimum_speed_difference_kph
                ),
                "minimum_throttle_difference": _round_continuous(
                    config.minimum_throttle_difference
                ),
                "minimum_brake_difference": _round_continuous(
                    config.minimum_brake_difference
                ),
            },
            "driving_event_parameters": {
                "brake_on_threshold": _round_continuous(
                    config.brake_on_threshold
                ),
                "brake_on_sustain_m": _round_distance(
                    config.brake_on_sustain_m
                ),
                "brake_off_threshold": _round_continuous(
                    config.brake_off_threshold
                ),
                "brake_off_sustain_m": _round_distance(
                    config.brake_off_sustain_m
                ),
                "brake_gap_merge_m": _round_distance(
                    config.brake_gap_merge_m
                ),
                "minimum_braking_event_length_m": _round_distance(
                    config.minimum_braking_event_length_m
                ),
                "minimum_peak_brake": _round_continuous(
                    config.minimum_peak_brake
                ),
                "minimum_speed_search_m": _round_distance(
                    config.minimum_speed_search_m
                ),
                "full_throttle_threshold": _round_continuous(
                    config.full_throttle_threshold
                ),
                "full_throttle_sustain_m": _round_distance(
                    config.full_throttle_sustain_m
                ),
                "coast_throttle_threshold": _round_continuous(
                    config.coast_throttle_threshold
                ),
                "coast_brake_threshold": _round_continuous(
                    config.coast_brake_threshold
                ),
                "coast_minimum_speed_kph": _round_continuous(
                    config.coast_minimum_speed_kph
                ),
                "minimum_coast_length_m": _round_distance(
                    config.minimum_coast_length_m
                ),
                "maximum_event_match_distance_m": _round_distance(
                    config.maximum_event_match_distance_m
                ),
                "event_exit_extension_m": _round_distance(
                    config.event_exit_extension_m
                ),
                "minimum_braking_point_difference_m": _round_distance(
                    config.minimum_braking_point_difference_m
                ),
                "minimum_braking_distance_difference_m": _round_distance(
                    config.minimum_braking_distance_difference_m
                ),
                "minimum_coast_difference_m": _round_distance(
                    config.minimum_coast_difference_m
                ),
                "minimum_throttle_reapplication_difference_m": (
                    _round_distance(
                        config.minimum_throttle_reapplication_difference_m
                    )
                ),
            },
        },
        "session": {
            "session_uid": result.session.session_uid,
            "track_id": result.session.track_id,
            "track_length_m": result.session.track_length,
            "sector2_start_distance_m": _round_distance(
                result.session.sector2_distance
            ),
            "sector3_start_distance_m": _round_distance(
                result.session.sector3_distance
            ),
            "normalization": asdict(result.session.normalization),
        },
        "lap_eligibility": [
            {
                "lap_number": lap.lap_number,
                "official_lap_time_ms": lap.official_lap_time_ms,
                "sector_times_ms": list(lap.sector_times_ms),
                "validity_flags": lap.validity_flags,
                "officially_valid": lap.officially_valid,
                "structurally_complete": lap.structurally_complete,
                "eligible": lap.eligible,
                "reason": lap.reason,
                "discarded_attempt_frames": discarded_by_lap.get(
                    lap.lap_number,
                    0,
                ),
            }
            for lap in result.session.validations
        ],
        "selected_reference_lap": result.reference_lap,
        "comparisons": [
            _comparison_document(comparison)
            for comparison in result.comparisons
        ],
    }
    if consistency is not None and result.consistency is not None:
        document["consistency"] = result.consistency.descriptor(consistency)
    if result.session.policy is not None:
        document["session_policy"] = asdict(result.session.policy)
    if result.session.lap_policy_context:
        document["lap_policy_context"] = [lap_policy_context_document(row) for row in result.session.lap_policy_context]
    if motion is not None and result.motion is not None:
        document["motion"] = result.motion.descriptor(*motion)
    return document


def _write_json(document: dict[str, object], output: Path) -> None:
    with output.open("x", encoding="utf-8", newline="\n") as file:
        json.dump(
            document,
            file,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        file.write("\n")


def _write_comparison_csv(comparison: LapComparison, output: Path) -> None:
    reference = comparison.reference
    compared = comparison.comparison
    with output.open("x", encoding="utf-8", newline="") as file:
        writer = csv.writer(file, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)
        for index, distance in enumerate(reference.distance):
            writer.writerow(
                (
                    f"{distance:.3f}",
                    f"{reference.elapsed_s[index]:.6f}",
                    f"{compared.elapsed_s[index]:.6f}",
                    f"{comparison.delta_s[index]:.6f}",
                    f"{reference.speed_kph[index]:.6f}",
                    f"{compared.speed_kph[index]:.6f}",
                    f"{reference.throttle[index]:.6f}",
                    f"{compared.throttle[index]:.6f}",
                    f"{reference.brake[index]:.6f}",
                    f"{compared.brake[index]:.6f}",
                    f"{reference.steering[index]:.6f}",
                    f"{compared.steering[index]:.6f}",
                    reference.gear[index],
                    compared.gear[index],
                )
            )


def write_analysis_bundle(
    result: AnalysisResult,
    output_dir: str | Path,
    *,
    svg_writer: SvgWriter = write_comparison_svg,
    report_writer: ReportWriter = write_markdown_report,
    html_writer: HtmlWriter = write_html_report,
) -> None:
    """Atomically create a complete analysis bundle without overwriting."""
    output = Path(output_dir)
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"output directory already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{output.name}.tmp-",
            dir=output.parent,
        )
    )
    try:
        consistency = result.consistency.json_bytes() if result.consistency is not None else None
        motion = _motion_artifacts(result)
        document = _analysis_document(result, consistency, motion)
        _write_json(document, temporary / "analysis.json")
        if consistency is not None:
            with (temporary / CONSISTENCY_FILENAME).open("xb") as file:
                file.write(consistency)
        if motion is not None:
            for name, raw in zip((MOTION_FILENAME, TRACK_MAP_FILENAME), motion, strict=True):
                with (temporary / name).open("xb") as file:
                    file.write(raw)
        report_writer(result, temporary / "report.md")
        html_writer(result, document, temporary / "report.html")
        for comparison in result.comparisons:
            stem = comparison_stem(comparison)
            _write_comparison_csv(comparison, temporary / f"{stem}.csv")
            svg_writer(
                result.session,
                comparison,
                temporary / f"{stem}.svg",
            )
        write_bundle_manifest(temporary)
        if output.exists() or output.is_symlink():
            raise FileExistsError(f"output directory already exists: {output}")
        temporary.rename(output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
