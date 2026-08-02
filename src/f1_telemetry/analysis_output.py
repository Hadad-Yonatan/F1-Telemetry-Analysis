"""Stable JSON, CSV, and SVG outputs for completed lap analyses."""

from __future__ import annotations

import csv
import json
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from f1_telemetry.driving_events import DrivingEvent, MatchedDrivingEvent
from f1_telemetry.lap_analysis import (
    AnalysisResult,
    LapComparison,
    LossRegion,
    PreparedSession,
)
from f1_telemetry.plot import write_comparison_svg

SvgWriter = Callable[[PreparedSession, LapComparison, Path], None]

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


def _comparison_document(comparison: LapComparison) -> dict[str, object]:
    return {
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


def _analysis_document(result: AnalysisResult) -> dict[str, object]:
    config = result.config
    discarded_by_lap = {
        lap.lap_number: lap.discarded_attempt_frames for lap in result.session.laps
    }
    return {
        "schema_version": config.schema_version,
        "capture": asdict(result.capture),
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


def _write_json(result: AnalysisResult, output: Path) -> None:
    with output.open("x", encoding="utf-8", newline="\n") as file:
        json.dump(
            _analysis_document(result),
            file,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        file.write("\n")


def _comparison_stem(comparison: LapComparison) -> str:
    return f"lap-{comparison.reference_lap:02d}-vs-{comparison.comparison_lap:02d}"


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
) -> None:
    """Atomically create a complete analysis bundle without overwriting."""
    output = Path(output_dir)
    if output.exists():
        raise FileExistsError(f"output directory already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{output.name}.tmp-",
            dir=output.parent,
        )
    )
    try:
        _write_json(result, temporary / "analysis.json")
        for comparison in result.comparisons:
            stem = _comparison_stem(comparison)
            _write_comparison_csv(comparison, temporary / f"{stem}.csv")
            svg_writer(
                result.session,
                comparison,
                temporary / f"{stem}.svg",
            )
        if output.exists():
            raise FileExistsError(f"output directory already exists: {output}")
        temporary.rename(output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
