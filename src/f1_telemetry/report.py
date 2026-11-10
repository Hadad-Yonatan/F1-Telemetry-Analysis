"""Deterministic Markdown rendering for completed telemetry analyses."""

from __future__ import annotations

from pathlib import Path

from f1_telemetry.corner_catalog import TrackDefinition, format_corner_label
from f1_telemetry.driving_events import DrivingEvent, MatchedDrivingEvent
from f1_telemetry.lap_analysis import AnalysisResult, LapComparison, LossRegion


def comparison_stem(comparison: LapComparison) -> str:
    return f"lap-{comparison.reference_lap:02d}-vs-{comparison.comparison_lap:02d}"


def _lap_time(milliseconds: int) -> str:
    minutes, remainder = divmod(milliseconds, 60_000)
    return f"{minutes}:{remainder / 1000:06.3f}"


def _event_text(label: str, event: DrivingEvent) -> str:
    throttle = (
        f"full throttle at {event.throttle_reapplication_distance:.3f} m"
        if event.throttle_reapplication_distance is not None
        else "sustained full throttle not detected"
    )
    return (
        f"- {label}: brake {event.braking_start_distance:.3f}–"
        f"{event.braking_end_distance:.3f} m "
        f"({event.braking_distance:.3f} m, peak {event.peak_brake:.3f}); "
        f"minimum speed {event.minimum_speed_kph:.3f} km/h at "
        f"{event.minimum_speed_distance:.3f} m; {throttle}; "
        f"coast distance {event.coast_distance:.3f} m."
    )


def _event_observations(match: MatchedDrivingEvent) -> list[str]:
    differences = match.differences
    statements = []
    for label in match.evidence_labels:
        if label == "braked later":
            statements.append(
                "Comparison braked "
                f"{abs(differences.braking_start_distance):.3f} m later."
            )
        elif label == "braked earlier":
            statements.append(
                "Comparison braked "
                f"{abs(differences.braking_start_distance):.3f} m earlier."
            )
        elif label == "braked over a longer distance":
            statements.append(
                "Comparison braking distance was "
                f"{abs(differences.braking_distance):.3f} m longer."
            )
        elif label == "braked over a shorter distance":
            statements.append(
                "Comparison braking distance was "
                f"{abs(differences.braking_distance):.3f} m shorter."
            )
        elif label == "coasted longer":
            statements.append(
                "Comparison coast distance was "
                f"{abs(differences.coast_distance):.3f} m longer."
            )
        elif label == "coasted less":
            statements.append(
                "Comparison coast distance was "
                f"{abs(differences.coast_distance):.3f} m shorter."
            )
        elif label == "full throttle occurred later":
            assert differences.throttle_reapplication_distance is not None
            statements.append(
                "Comparison sustained full throttle occurred "
                f"{abs(differences.throttle_reapplication_distance):.3f} m later."
            )
        elif label == "full throttle occurred earlier":
            assert differences.throttle_reapplication_distance is not None
            statements.append(
                "Comparison sustained full throttle occurred "
                f"{abs(differences.throttle_reapplication_distance):.3f} m earlier."
            )
        elif label == "lower minimum speed":
            statements.append(
                "Comparison minimum speed was "
                f"{abs(differences.minimum_speed_kph):.3f} km/h lower."
            )
        elif label == "higher minimum speed":
            statements.append(
                "Comparison minimum speed was "
                f"{abs(differences.minimum_speed_kph):.3f} km/h higher."
            )
        elif label == "event differences below thresholds":
            statements.append(
                "Matched event differences remained below the evidence thresholds."
            )
        else:
            raise ValueError(f"unsupported event evidence label: {label}")
    return statements


def _region_lines(
    comparison: LapComparison,
    region: LossRegion,
    rank: int,
    maximum_match_distance: float,
    track: TrackDefinition | None,
) -> list[str]:
    corner_prefix = (
        f"{format_corner_label(track, region.corner_context)}, "
        if track is not None and region.corner_context is not None
        else ""
    )
    lines = [
        (
            f"#### {rank}. {corner_prefix}Sector {region.sector}, "
            f"{region.start_distance:.3f}–{region.end_distance:.3f} m "
            f"(+{region.time_loss_s:.3f} s)"
        ),
        "",
        f"- Aggregate evidence: {', '.join(region.evidence_labels)}.",
        (
            "- Minimum speed: reference "
            f"{region.reference_min_speed_kph:.3f} km/h, comparison "
            f"{region.comparison_min_speed_kph:.3f} km/h, difference "
            f"{region.minimum_speed_difference_kph:+.3f} km/h."
        ),
        (
            "- Mean throttle: reference "
            f"{region.reference_mean_throttle:.3f}, comparison "
            f"{region.comparison_mean_throttle:.3f}, difference "
            f"{region.mean_throttle_difference:+.3f}."
        ),
        (
            "- Mean brake: reference "
            f"{region.reference_mean_brake:.3f}, comparison "
            f"{region.comparison_mean_brake:.3f}, difference "
            f"{region.mean_brake_difference:+.3f}."
        ),
    ]

    if region.matched_event_index is not None:
        match = comparison.event_matches[region.matched_event_index]
        lines.extend(
            (
                _event_text("Reference event", match.reference),
                _event_text("Comparison event", match.comparison),
                "- Event observations:",
            )
        )
        lines.extend(f"  - {statement}" for statement in _event_observations(match))
    else:
        associated = False
        reference_by_index = {
            event.event_index: event for event in comparison.reference_events
        }
        comparison_by_index = {
            event.event_index: event for event in comparison.comparison_events
        }
        for event_index in region.unmatched_reference_event_indices:
            associated = True
            lines.append(
                _event_text(
                    "Reference-only event",
                    reference_by_index[event_index],
                )
            )
            lines.append(
                "- No comparison event matched within "
                f"{maximum_match_distance:.3f} m."
            )
        for event_index in region.unmatched_comparison_event_indices:
            associated = True
            lines.append(
                _event_text(
                    "Comparison-only event",
                    comparison_by_index[event_index],
                )
            )
            lines.append(
                "- No reference event matched within "
                f"{maximum_match_distance:.3f} m."
            )
        if not associated:
            lines.append("- No qualifying braking event overlaps this region.")
    lines.append("")
    return lines


def render_markdown_report(result: AnalysisResult) -> str:
    """Render every comparison and loss region from an existing result."""
    session = result.session
    lines = [
        "# F1 Telemetry Driver Report",
        "",
        f"- Capture: `{result.capture.basename}`",
        f"- Capture size: {result.capture.size_bytes} bytes",
        f"- Capture SHA-256: `{result.capture.sha256}`",
        f"- Capture created: {result.capture.created_unix_ns} ns since Unix epoch",
        f"- Session UID: {session.session_uid}",
        f"- Track ID: {session.track_id}",
        f"- Track length: {session.track_length} m",
        f"- Algorithm version: {result.config.algorithm_version}",
    ]
    if session.policy is not None:
        policy = session.policy
        lines.append(f"- Session policy: {policy.name} ({policy.status}, method {policy.method_version})")
    lines.extend((
        "",
        "## Lap eligibility",
        "",
        "| Lap | Official time | Sector 1 | Sector 2 | Sector 3 | Status |",
        "| ---: | :--- | :--- | :--- | :--- | :--- |",
    ))
    for lap in session.validations:
        time = _lap_time(lap.official_lap_time_ms) if lap.official_lap_time_ms else "--"
        sectors = [
            _lap_time(value) if value else "--" for value in lap.sector_times_ms
        ]
        lines.append(
            f"| {lap.lap_number} | {time} | {sectors[0]} | {sectors[1]} | "
            f"{sectors[2]} | {lap.reason} |"
        )

    reference = result.comparisons[0].reference
    context = result.session_context
    if context is not None:
        lines.extend(("", "## Session context", ""))
        if context.settings is not None:
            settings = context.settings
            lines.append(
                f"Session type {settings.session_type}, weather {settings.weather}, "
                f"track {settings.track_temperature_c} °C, air {settings.air_temperature_c} °C."
            )
        else:
            lines.append("Session settings unavailable.")
        lines.append(
            f"Optional context decode errors: {sum(row.decode_errors for row in context.diagnostics)}."
        )
        lines.extend(("", "| Lap | Conditions stable | Missing at start | Changes |",
                      "| ---: | :--- | :--- | ---: |"))
        for lap in context.laps:
            stable = {True: "Yes", False: "No", None: "Unknown"}[lap.stable_during_lap]
            missing = lap.unavailable_reason or ", ".join(lap.missing_at_start) or "None"
            lines.append(f"| {lap.lap_number} | {stable} | {missing} | {len(lap.change_indices)} |")
    lines.extend(
        (
            "",
            "## Reference lap",
            "",
            f"Lap {reference.lap_number}: {_lap_time(reference.official_lap_time_ms)}.",
            "",
        )
    )

    for comparison in result.comparisons:
        stem = comparison_stem(comparison)
        lines.extend(
            (
                (
                    f"## Lap {comparison.comparison_lap} versus "
                    f"lap {comparison.reference_lap}"
                ),
                "",
                (
                    f"Official delta: {comparison.official_delta_s:+.3f} s. "
                    f"Finish verification error: "
                    f"{comparison.final_delta_error_s:+.6f} s."
                ),
                "",
                "| Sector | Delta |",
                "| ---: | ---: |",
            )
        )
        for sector, delta in enumerate(comparison.sector_deltas_s, start=1):
            lines.append(f"| {sector} | {delta:+.3f} s |")
        lines.extend(
            (
                "",
                f"Files: [aligned trace]({stem}.csv) · [six-panel plot]({stem}.svg)",
                "",
                "### Loss regions",
                "",
            )
        )
        for rank, region in enumerate(comparison.regions, start=1):
            lines.extend(
                _region_lines(
                    comparison,
                    region,
                    rank,
                    result.config.maximum_event_match_distance_m,
                    result.catalog_selection.track,
                )
            )

    config = result.config
    lines.extend(
        (
            "## Method and thresholds",
            "",
            f"- Distance grid: {config.distance_step_m:.3f} m",
            f"- Loss bin: {config.loss_bin_size_m:.3f} m",
            f"- Minimum bin loss: {config.minimum_bin_loss_s:.6f} s",
            f"- Minimum region loss: {config.minimum_region_loss_s:.6f} s",
            (
                f"- Brake detection: {config.brake_on_threshold:.3f} for "
                f"{config.brake_on_sustain_m:.3f} m; release "
                f"{config.brake_off_threshold:.3f} for "
                f"{config.brake_off_sustain_m:.3f} m"
            ),
            (
                f"- Accepted brake event: at least "
                f"{config.minimum_braking_event_length_m:.3f} m and peak "
                f"{config.minimum_peak_brake:.3f}"
            ),
            (
                f"- Sustained full throttle: {config.full_throttle_threshold:.3f} "
                f"for {config.full_throttle_sustain_m:.3f} m"
            ),
            (
                f"- Coasting: throttle at most "
                f"{config.coast_throttle_threshold:.3f}, brake at most "
                f"{config.coast_brake_threshold:.3f}, speed at least "
                f"{config.coast_minimum_speed_kph:.3f} km/h, length at least "
                f"{config.minimum_coast_length_m:.3f} m"
            ),
            (
                f"- Event matching distance: "
                f"{config.maximum_event_match_distance_m:.3f} m"
            ),
            (
                "- Evidence differences: braking point "
                f"{config.minimum_braking_point_difference_m:.3f} m, braking "
                f"distance {config.minimum_braking_distance_difference_m:.3f} m, "
                f"coasting {config.minimum_coast_difference_m:.3f} m, throttle "
                "reapplication "
                f"{config.minimum_throttle_reapplication_difference_m:.3f} m, "
                f"minimum speed {config.minimum_speed_difference_kph:.3f} km/h"
            ),
            "",
            "## Interpretation caveat",
            "",
            (
                "These observations describe measured differences under fixed "
                "thresholds. They do not establish causation or prescribe a "
                "driving input."
            ),
            "",
        )
    )
    return "\n".join(lines)


def write_markdown_report(result: AnalysisResult, output: Path) -> None:
    with output.open("x", encoding="utf-8", newline="\n") as file:
        file.write(render_markdown_report(result))
