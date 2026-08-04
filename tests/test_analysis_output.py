import csv
import contextlib
import io
import json
import re
import tempfile
import unittest
from dataclasses import replace
from importlib import resources
from pathlib import Path
from unittest.mock import patch

from f1_telemetry.analysis_output import (
    CSV_COLUMNS,
    analysis_document,
    write_analysis_bundle,
)
from f1_telemetry.capture_format import CaptureWriter
from f1_telemetry.corner_catalog import (
    CatalogMetadata,
    CornerCatalog,
    CornerComplex,
    TrackDefinition,
    TurnDefinition,
    load_bundled_corner_catalog,
)
from f1_telemetry.driving_events import (
    DrivingEvent,
    EventDifferences,
    MatchedDrivingEvent,
)
from f1_telemetry.lap_analysis import (
    LapTrace,
    LapValidation,
    NormalizationSummary,
    PreparedSession,
    analyze_capture,
    print_analysis,
)
from f1_telemetry.normalize import NormalizedSample
from f1_telemetry.report import render_markdown_report


def html_payload(rendered: str) -> dict[str, object]:
    match = re.search(
        r'<script id="report-data" type="application/json">(.*?)</script>',
        rendered,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError("report payload not found")
    return json.loads(match.group(1))


def make_sample(
    frame: int,
    lap_number: int,
    distance: float,
    elapsed_ms: int,
    speed: int,
) -> NormalizedSample:
    return NormalizedSample(
        captured_unix_ns=frame,
        session_uid=123,
        frame_identifier=frame,
        overall_frame_identifier=frame,
        session_time=elapsed_ms / 1000,
        player_car_index=0,
        lap_number=lap_number,
        lap_distance=distance,
        total_distance=distance,
        current_lap_time_ms=elapsed_ms,
        last_lap_time_ms=0,
        sector=0,
        speed_kph=speed,
        throttle=0.75,
        brake=0.10,
        steering=-0.25,
        clutch=0,
        gear=4,
        engine_rpm=10_000,
        drs=False,
        lap_invalid=False,
        pit_status=0,
        driver_status=1,
        result_status=2,
    )


def validation(
    lap_number: int,
    lap_time_ms: int,
    sectors: tuple[int, int, int],
    *,
    valid: bool = True,
    complete: bool = True,
) -> LapValidation:
    eligible = valid and complete
    if eligible:
        reason = "eligible"
    elif not complete:
        reason = "incomplete"
    else:
        reason = "officially invalid"
    return LapValidation(
        lap_number=lap_number,
        official_lap_time_ms=lap_time_ms,
        sector_times_ms=sectors,
        validity_flags=15 if valid else 2,
        officially_valid=valid,
        structurally_complete=complete,
        eligible=eligible,
        reason=reason,
    )


def trace(
    lap_number: int,
    lap_time_ms: int,
    sectors: tuple[int, int, int],
    speed: int,
    discarded: int = 0,
) -> LapTrace:
    return LapTrace(
        lap_number=lap_number,
        official_lap_time_ms=lap_time_ms,
        samples=(
            make_sample(lap_number * 10, lap_number, 0, 0, speed),
            make_sample(
                lap_number * 10 + 1,
                lap_number,
                99,
                lap_time_ms - 100,
                speed,
            ),
        ),
        discarded_attempt_frames=discarded,
        sector_times_ms=sectors,
    )


def prepared_session() -> PreparedSession:
    lap1 = validation(1, 2_000, (600, 700, 700))
    lap2 = validation(2, 1_900, (600, 600, 700), valid=False)
    lap3 = validation(3, 1_500, (400, 500, 600))
    lap4 = validation(4, 0, (0, 0, 0), complete=False)
    lap5 = validation(5, 2_500, (800, 800, 900))
    return PreparedSession(
        session_uid=123,
        track_length=100,
        track_id=10,
        sector2_distance=33.333,
        sector3_distance=66.667,
        validations=(lap1, lap2, lap3, lap4, lap5),
        laps=(
            trace(1, 2_000, lap1.sector_times_ms, 190),
            trace(3, 1_500, lap3.sector_times_ms, 220, discarded=2),
            trace(5, 2_500, lap5.sector_times_ms, 180, discarded=4),
        ),
        normalization=NormalizationSummary(
            capture_datagrams=100,
            samples_joined=20,
        ),
    )


def matching_catalog() -> CornerCatalog:
    turns = (
        TurnDefinition(
            turn_id="test-t01",
            number=1,
            display_name="First",
            direction="left",
            entry_distance_m=0.0,
            apex_distance_m=25.0,
            exit_distance_m=50.0,
        ),
        TurnDefinition(
            turn_id="test-t02",
            number=2,
            display_name="Second",
            direction="right",
            entry_distance_m=50.0,
            apex_distance_m=75.0,
            exit_distance_m=100.0,
        ),
    )
    return CornerCatalog(
        metadata=CatalogMetadata(
            schema_version=1,
            catalog_id="test-corners-v1",
            packet_format=2025,
            game_year=25,
            sha256="a" * 64,
        ),
        tracks=(
            TrackDefinition(
                track_id=10,
                display_name="Test Circuit",
                track_length_m=100,
                turns=turns,
                complexes=(
                    CornerComplex(
                        complex_id="test-chicane",
                        display_name="Test Chicane",
                        turn_ids=("test-t01", "test-t02"),
                    ),
                ),
            ),
        ),
    )


class AnalysisSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.capture = Path(self.temporary.name) / "session.f1cap"
        with CaptureWriter(self.capture, created_unix_ns=123_456_789):
            pass
        self.session = prepared_session()

    def analyze(self, **options: object):
        catalog = options.pop("corner_catalog", None)
        with patch(
            "f1_telemetry.lap_analysis.prepare_session",
            return_value=self.session,
        ):
            return analyze_capture(
                self.capture,
                corner_catalog=catalog,
                **options,
            )

    def test_defaults_to_fastest_eligible_lap_against_all_others(self) -> None:
        result = self.analyze()

        self.assertEqual(result.reference_lap, 3)
        self.assertEqual(
            [comparison.comparison_lap for comparison in result.comparisons],
            [1, 5],
        )
        self.assertEqual(result.comparisons[0].sector_deltas_s, (0.2, 0.2, 0.1))
        self.assertEqual(
            result.comparisons[0].reference_discarded_attempt_frames,
            2,
        )
        self.assertTrue(
            all(
                abs(comparison.final_delta_error_s) <= 1e-9
                for comparison in result.comparisons
            )
        )

    def test_explicit_reference_and_comparison_override_defaults(self) -> None:
        result = self.analyze(reference_lap=1, comparison_laps=(5,))

        self.assertEqual(result.reference_lap, 1)
        self.assertEqual(len(result.comparisons), 1)
        self.assertEqual(result.comparisons[0].comparison_lap, 5)
        self.assertEqual(result.comparisons[0].official_delta_s, 0.5)

    def test_rejects_invalid_incomplete_missing_and_identical_laps(self) -> None:
        cases = (
            ({"reference_lap": 2}, "officially invalid"),
            ({"comparison_laps": (4,)}, "incomplete"),
            ({"comparison_laps": (99,)}, "missing"),
            (
                {"reference_lap": 3, "comparison_laps": (3,)},
                "must be different",
            ),
        )
        for options, message in cases:
            with self.subTest(options=options):
                with self.assertRaisesRegex(ValueError, message):
                    self.analyze(**options)

    def test_writes_reproducible_complete_bundle(self) -> None:
        result = self.analyze()
        first = Path(self.temporary.name) / "first"
        second = Path(self.temporary.name) / "second"

        write_analysis_bundle(result, first)
        write_analysis_bundle(result, second)

        expected_names = {
            "analysis.json",
            "report.html",
            "report.md",
            "lap-03-vs-01.csv",
            "lap-03-vs-01.svg",
            "lap-03-vs-05.csv",
            "lap-03-vs-05.svg",
        }
        self.assertEqual({path.name for path in first.iterdir()}, expected_names)
        self.assertEqual({path.name for path in second.iterdir()}, expected_names)
        for name in expected_names:
            self.assertEqual((first / name).read_bytes(), (second / name).read_bytes())

        document = json.loads((first / "analysis.json").read_text(encoding="utf-8"))
        self.assertEqual(document, analysis_document(result))
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["algorithm"]["version"], "1.1.0")
        self.assertEqual(document["corner_catalog"]["status"], "disabled")
        self.assertIsNone(document["corner_catalog"]["selected_track"])
        self.assertEqual(
            document["algorithm"]["driving_event_parameters"][
                "brake_on_threshold"
            ],
            0.05,
        )
        self.assertEqual(document["capture"]["basename"], "session.f1cap")
        self.assertEqual(document["selected_reference_lap"], 3)
        self.assertEqual(len(document["lap_eligibility"]), 5)
        self.assertTrue(document["comparisons"][0]["finish_delta_verified"])
        self.assertIn("driving_events", document["comparisons"][0])
        self.assertIn("sector", document["comparisons"][0]["loss_regions"][0])
        self.assertIsNone(
            document["comparisons"][0]["loss_regions"][0]["corner_context"]
        )
        self.assertIn(
            "unmatched_comparison_event_indices",
            document["comparisons"][0]["loss_regions"][0],
        )
        self.assertIn(
            "minimum_speed_difference_kph",
            document["comparisons"][0]["loss_regions"][0]["evidence"],
        )
        self.assertTrue((first / "analysis.json").read_bytes().endswith(b"\n"))
        self.assertNotIn("NaN", (first / "analysis.json").read_text(encoding="utf-8"))

        with (first / "lap-03-vs-01.csv").open(
            newline="",
            encoding="utf-8",
        ) as file:
            reader = csv.DictReader(file)
            rows = list(reader)
        self.assertEqual(tuple(reader.fieldnames or ()), CSV_COLUMNS)
        self.assertEqual(rows[0]["distance_m"], "0.000")
        self.assertEqual(rows[-1]["comparison_elapsed_s"], "2.000000")
        svg = (first / "lap-03-vs-01.svg").read_text(encoding="utf-8")
        self.assertIn("Lap 3 vs lap 1", svg)
        self.assertIn("Delta +0.500 s", svg)
        report = (first / "report.md").read_text(encoding="utf-8")
        self.assertIn("# F1 Telemetry Driver Report", report)
        self.assertIn("## Lap 1 versus lap 3", report)
        self.assertIn("[aligned trace](lap-03-vs-01.csv)", report)
        self.assertIn("No qualifying braking event overlaps this region", report)
        self.assertEqual(
            report.count("\n#### "),
            sum(len(comparison.regions) for comparison in result.comparisons),
        )
        self.assertTrue((first / "report.md").read_bytes().endswith(b"\n"))
        rendered_html = (first / "report.html").read_text(encoding="utf-8")
        payload = html_payload(rendered_html)
        self.assertEqual(payload["report_version"], 1)
        self.assertEqual(payload["analysis"], document)
        self.assertEqual(payload["presentation"]["initial_comparison_lap"], 1)
        self.assertEqual(payload["presentation"]["session_uid_label"], "123")
        self.assertTrue((first / "report.html").read_bytes().endswith(b"\n"))
        with self.assertRaisesRegex(FileExistsError, "already exists"):
            write_analysis_bundle(result, first)

    def test_html_payload_is_rounded_deduplicated_and_safely_embedded(self) -> None:
        result = self.analyze()
        hostile = replace(
            result,
            capture=replace(
                result.capture,
                basename='session</script><b title="x">\u2028&.f1cap',
            ),
        )
        output = Path(self.temporary.name) / "html"
        write_analysis_bundle(hostile, output)

        rendered = (output / "report.html").read_text(encoding="utf-8")
        payload = html_payload(rendered)
        traces = payload["traces"]
        self.assertEqual(payload["analysis"], analysis_document(hostile))
        self.assertEqual(payload["presentation"]["initial_comparison_lap"], 1)
        self.assertEqual(traces["distance_m"], list(range(101)))
        self.assertNotIn("distance_m", traces["reference"])
        self.assertTrue(
            all("distance_m" not in trace for trace in traces["comparisons"])
        )
        self.assertTrue(
            all(
                isinstance(gear, int)
                for trace in [traces["reference"], *traces["comparisons"]]
                for gear in trace["gear"]
            )
        )
        self.assertNotIn("</script><b", rendered)
        self.assertNotIn("\u2028", rendered)
        self.assertIn("\\u003c/script\\u003e", rendered)
        self.assertNotRegex(rendered, r"https?://")
        for forbidden in (
            "fetch(",
            "XMLHttpRequest",
            "WebSocket",
            "EventSource",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_html_initial_comparison_tie_breaks_by_lower_lap(self) -> None:
        result = self.analyze()
        tied = replace(
            result,
            comparisons=tuple(
                replace(comparison, official_delta_s=0.5)
                for comparison in reversed(result.comparisons)
            ),
        )
        output = Path(self.temporary.name) / "tie"
        write_analysis_bundle(tied, output)
        payload = html_payload((output / "report.html").read_text(encoding="utf-8"))
        self.assertEqual(payload["presentation"]["initial_comparison_lap"], 1)

    def test_html_supports_no_regions_and_no_events(self) -> None:
        result = self.analyze()
        empty = replace(
            result,
            comparisons=tuple(
                replace(
                    comparison,
                    regions=(),
                    reference_events=(),
                    comparison_events=(),
                    event_matches=(),
                    unmatched_reference_event_indices=(),
                    unmatched_comparison_event_indices=(),
                )
                for comparison in result.comparisons
            ),
        )
        output = Path(self.temporary.name) / "empty"
        write_analysis_bundle(empty, output)
        payload = html_payload((output / "report.html").read_text(encoding="utf-8"))
        self.assertTrue(
            all(
                not presentation["region_labels"]
                for presentation in payload["presentation"]["comparisons"]
            )
        )
        self.assertTrue(
            all(
                not comparison["loss_regions"]
                and not comparison["driving_events"]["matches"]
                for comparison in payload["analysis"]["comparisons"]
            )
        )

    def test_html_uses_python_labels_and_relative_comparison_links(self) -> None:
        result = self.analyze(corner_catalog=matching_catalog())
        output = Path(self.temporary.name) / "catalog-html"
        write_analysis_bundle(result, output)
        payload = html_payload((output / "report.html").read_text(encoding="utf-8"))

        first = payload["presentation"]["comparisons"][0]
        self.assertEqual(first["region_labels"], ["Test Chicane (T1-T2)"])
        self.assertEqual(
            first["artifact_links"],
            {"csv": "lap-03-vs-01.csv", "svg": "lap-03-vs-01.svg"},
        )
        self.assertEqual(
            len(first["region_labels"]),
            len(payload["analysis"]["comparisons"][0]["loss_regions"]),
        )

    def test_html_assets_are_packaged_resources(self) -> None:
        asset_root = resources.files("f1_telemetry").joinpath("html_assets")
        for name in (
            "report.css",
            "report.js",
            "AtkinsonHyperlegible-Regular.woff2",
            "AtkinsonHyperlegible-Bold.woff2",
            "AtkinsonHyperlegible-OFL.txt",
        ):
            self.assertGreater(len(asset_root.joinpath(name).read_bytes()), 0)

    def test_catalog_context_is_shared_by_json_markdown_and_console(self) -> None:
        result = self.analyze(corner_catalog=matching_catalog())
        self.assertEqual(result.catalog_selection.status, "matched")
        region = result.comparisons[0].regions[0]
        assert region.corner_context is not None
        self.assertEqual(region.corner_context.primary_turn_id, "test-t01")
        self.assertEqual(
            region.corner_context.overlapping_turn_ids,
            ("test-t01", "test-t02"),
        )
        self.assertEqual(region.corner_context.complex_ids, ("test-chicane",))

        output = Path(self.temporary.name) / "catalog-output"
        write_analysis_bundle(result, output)
        document = json.loads((output / "analysis.json").read_text(encoding="utf-8"))
        catalog_document = document["corner_catalog"]
        self.assertEqual(catalog_document["status"], "matched")
        self.assertEqual(catalog_document["sha256"], "a" * 64)
        self.assertEqual(catalog_document["selected_track"]["track_length_m"], 100)
        region_document = document["comparisons"][0]["loss_regions"][0]
        self.assertEqual(region_document["corner_context"]["primary_turn_id"], "test-t01")
        self.assertEqual(
            region_document["corner_context"]["overlapping_turn_ids"],
            ["test-t01", "test-t02"],
        )

        report = render_markdown_report(result)
        self.assertIn("Test Chicane (T1-T2), Sector 2", report)
        console = io.StringIO()
        with contextlib.redirect_stdout(console):
            print_analysis(result)
        self.assertIn("Test Chicane (T1-T2) | 0-100 m", console.getvalue())

    def test_matching_track_id_with_wrong_length_stops_analysis(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not match session length"):
            self.analyze(corner_catalog=load_bundled_corner_catalog())

    def test_report_uses_stored_event_values_with_missing_throttle(self) -> None:
        result = self.analyze()
        comparison = result.comparisons[0]
        reference_event = DrivingEvent(
            event_index=0,
            braking_start_distance=10,
            braking_end_distance=30,
            braking_distance=20,
            peak_brake=0.8,
            minimum_speed_distance=40,
            minimum_speed_kph=100,
            throttle_reapplication_distance=None,
            coast_distance=0,
            analysis_end_distance=80,
        )
        compared_event = replace(
            reference_event,
            braking_start_distance=17,
            braking_end_distance=37,
            minimum_speed_kph=95,
        )
        differences = EventDifferences(
            braking_start_distance=7,
            braking_end_distance=7,
            braking_distance=0,
            peak_brake=0,
            minimum_speed_distance=0,
            minimum_speed_kph=-5,
            throttle_reapplication_distance=None,
            coast_distance=0,
        )
        match = MatchedDrivingEvent(
            match_index=0,
            reference=reference_event,
            comparison=compared_event,
            differences=differences,
            evidence_labels=("braked later", "lower minimum speed"),
        )
        region = replace(
            comparison.regions[0],
            matched_event_index=0,
        )
        enriched_comparison = replace(
            comparison,
            regions=(region,) + comparison.regions[1:],
            reference_events=(reference_event,),
            comparison_events=(compared_event,),
            event_matches=(match,),
        )
        enriched_result = replace(
            result,
            comparisons=(enriched_comparison,) + result.comparisons[1:],
        )

        report = render_markdown_report(enriched_result)

        self.assertIn("Comparison braked 7.000 m later.", report)
        self.assertIn("Comparison minimum speed was 5.000 km/h lower.", report)
        self.assertIn("sustained full throttle not detected", report)
        self.assertIn("coast distance 0.000 m", report)

        output = Path(self.temporary.name) / "event-html"
        write_analysis_bundle(enriched_result, output)
        payload = html_payload((output / "report.html").read_text(encoding="utf-8"))
        event_document = payload["analysis"]["comparisons"][0]["driving_events"]
        self.assertIsNone(
            event_document["reference"][0]["throttle_reapplication_distance_m"]
        )
        self.assertEqual(event_document["reference"][0]["coast_distance_m"], 0)
        self.assertEqual(
            event_document["matches"][0]["evidence_labels"],
            ["braked later", "lower minimum speed"],
        )

    def test_output_failure_leaves_no_bundle_or_temporary_directory(self) -> None:
        result = self.analyze()
        output = Path(self.temporary.name) / "failed"

        def fail_svg(
            _session: PreparedSession,
            _comparison: object,
            _output: Path,
        ) -> None:
            raise RuntimeError("injected output failure")

        with self.assertRaisesRegex(RuntimeError, "injected output failure"):
            write_analysis_bundle(result, output, svg_writer=fail_svg)

        self.assertFalse(output.exists())
        self.assertEqual(
            list(Path(self.temporary.name).glob(".failed.tmp-*")),
            [],
        )

        report_output = Path(self.temporary.name) / "failed-report"

        def fail_report(_result: object, _output: Path) -> None:
            raise RuntimeError("injected report failure")

        with self.assertRaisesRegex(RuntimeError, "injected report failure"):
            write_analysis_bundle(
                result,
                report_output,
                report_writer=fail_report,
            )

        self.assertFalse(report_output.exists())
        self.assertEqual(
            list(Path(self.temporary.name).glob(".failed-report.tmp-*")),
            [],
        )

        html_output = Path(self.temporary.name) / "failed-html"

        def fail_html(
            _result: object,
            _document: dict[str, object],
            _output: Path,
        ) -> None:
            raise RuntimeError("injected HTML failure")

        with self.assertRaisesRegex(RuntimeError, "injected HTML failure"):
            write_analysis_bundle(
                result,
                html_output,
                html_writer=fail_html,
            )

        self.assertFalse(html_output.exists())
        self.assertEqual(
            list(Path(self.temporary.name).glob(".failed-html.tmp-*")),
            [],
        )


if __name__ == "__main__":
    unittest.main()
