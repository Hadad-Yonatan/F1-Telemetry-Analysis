import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from f1_telemetry.analysis_output import write_analysis_bundle
from f1_telemetry.capture_format import CaptureWriter
from f1_telemetry.lap_analysis import AnalysisConfig, PreparedSession, analyze_capture
from f1_telemetry.session_context import ContextCollector
from f1_telemetry.session_policy import PolicySelection
from tests.test_analysis_output import prepared_session
from tests.test_motion_analysis import fixture_motion
from tests.test_session_context import add, session_packet


def browser_result(capture: Path, *, step: float = 1.0):
    original = prepared_session()
    validations = original.validations
    extended = PreparedSession(
        session_uid=3_410_183_638_808_808_837,
        track_length=500,
        track_id=98,
        sector2_distance=166.667,
        sector3_distance=333.333,
        validations=validations,
        laps=tuple(
            replace(
                lap,
                samples=(
                    replace(lap.samples[0], lap_distance=0, total_distance=0, session_uid=3_410_183_638_808_808_837),
                    replace(lap.samples[-1], lap_distance=499, total_distance=499, session_uid=3_410_183_638_808_808_837),
                ),
            )
            for lap in original.laps
        ),
        normalization=original.normalization,
        policy=PolicySelection("time-trial", "validated", "1.0.0", 18),
    )
    collector = ContextCollector(extended.session_uid, 0)
    add(collector, session_packet(1, uid=extended.session_uid))
    extended = replace(extended, context=collector.finish(extended.laps))
    with patch("f1_telemetry.lap_analysis.prepare_session", return_value=extended):
        result = analyze_capture(capture, corner_catalog=None, config=AnalysisConfig(distance_step_m=step))
    comparisons = []
    for comparison in result.comparisons:
        region = replace(
            comparison.regions[0],
            start_distance=200,
            end_distance=250,
        )
        comparisons.append(replace(comparison, regions=(region,)))
    motion = fixture_motion(result.session, result.reference_lap, result.comparisons[0].reference.distance, gap=True)
    return replace(result, comparisons=tuple(comparisons), motion=motion)


def contrast_ratio(first: str, second: str) -> float:
    def luminance(color: str) -> float:
        values = [int(value) / 255 for value in color.removeprefix("rgb(").removesuffix(")").split(", ")]
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in values]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


class HtmlReportBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
        capture = root / "browser.f1cap"
        with CaptureWriter(capture, created_unix_ns=123_456_789):
            pass
        bundle = root / "bundle"
        write_analysis_bundle(browser_result(capture), bundle)
        cls.report_url = (bundle / "report.html").as_uri()
        coarse_bundle = root / "coarse"
        write_analysis_bundle(browser_result(capture, step=20.0), coarse_bundle)
        cls.coarse_report_url = (coarse_bundle / "report.html").as_uri()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()
        cls.temporary.cleanup()

    def test_consistency_is_visible_without_selecting_a_comparison(self):
        page = self.open_report()
        section = page.get_by_role("region", name="Lap consistency")
        self.assertTrue(section.get_by_text("3 eligible laps", exact=False).is_visible())
        self.assertTrue(section.get_by_role("table", name="Official times (seconds)").is_visible())
        self.assertTrue(section.get_by_text("without a matching corner catalog", exact=False).is_visible())

    def open_report(self, viewport: dict[str, int] | None = None, *, url: str | None = None):
        page = self.browser.new_page(viewport=viewport or {"width": 1440, "height": 900})
        requests: list[str] = []
        errors: list[str] = []
        page.on("request", lambda request: requests.append(request.url))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url or self.report_url)
        self.addCleanup(page.close)
        self.assertTrue(all(url.startswith("file:") for url in requests), requests)
        self.assertEqual(errors, [])
        self.addCleanup(lambda: self.assertEqual(errors, []))
        self.addCleanup(lambda: self.assertTrue(all(url.startswith("file:") for url in requests), requests))
        return page

    def test_comparison_switch_cursor_channels_and_accessible_controls(self) -> None:
        page = self.open_report()
        selector = page.get_by_role("combobox", name="Comparison lap")
        self.assertEqual(selector.input_value(), "1")
        self.assertEqual(page.locator("body").get_attribute("data-comparison-lap"), "1")
        self.assertTrue(page.get_by_text("3410183638808808837", exact=True).is_visible())
        self.assertTrue(page.get_by_text("Track temperature", exact=True).is_visible())
        self.assertTrue(page.get_by_text("33 °C", exact=True).is_visible())
        self.assertTrue(page.get_by_text("24 °C", exact=True).is_visible())
        self.assertTrue(page.get_by_text("time-trial (validated, method 1.0.0)", exact=True).is_visible())
        self.assertEqual(page.locator(".telemetry-chart").count(), 6)
        self.assertEqual(page.get_by_role("checkbox", name="Brake").count(), 1)

        charts = page.get_by_label("Synchronized telemetry charts")
        charts.focus()
        page.keyboard.press("ArrowRight")
        self.assertEqual(charts.get_attribute("data-cursor-distance"), "1.000")
        page.keyboard.press("Shift+ArrowRight")
        self.assertEqual(charts.get_attribute("data-cursor-distance"), "11.000")
        page.keyboard.press("End")
        self.assertEqual(charts.get_attribute("data-cursor-distance"), "500.000")

        brake = page.get_by_role("checkbox", name="Brake")
        brake.uncheck()
        self.assertTrue(page.locator('.chart-panel[data-channel="brake"]').is_hidden())
        brake.check()
        self.assertTrue(page.locator('.chart-panel[data-channel="brake"]').is_visible())

        selector.select_option("5")
        self.assertEqual(charts.get_attribute("data-cursor-distance"), "0.000")
        self.assertEqual(charts.get_attribute("data-range-start"), "0.000")
        self.assertEqual(charts.get_attribute("data-range-end"), "500.000")

    def test_drag_region_zoom_reset_and_keyboard_reset(self) -> None:
        page = self.open_report()
        charts = page.get_by_label("Synchronized telemetry charts")
        speed_chart = page.get_by_role("img", name="Speed telemetry chart")
        speed_chart.scroll_into_view_if_needed()
        box = speed_chart.bounding_box()
        assert box is not None
        page.mouse.move(box["x"] + box["width"] * 0.25, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] * 0.75, box["y"] + box["height"] / 2)
        page.mouse.up()
        self.assertGreater(float(charts.get_attribute("data-range-start")), 0)
        self.assertLess(float(charts.get_attribute("data-range-end")), 500)

        page.get_by_role("button", name="Reset view").click()
        self.assertEqual(charts.get_attribute("data-range-start"), "0.000")
        self.assertEqual(charts.get_attribute("data-range-end"), "500.000")

        page.locator('button[data-region-index="0"]').click()
        self.assertEqual(charts.get_attribute("data-range-start"), "100.000")
        self.assertEqual(charts.get_attribute("data-range-end"), "350.000")
        charts.press("Escape")
        self.assertEqual(charts.get_attribute("data-range-start"), "0.000")
        self.assertEqual(charts.get_attribute("data-range-end"), "500.000")

    def test_desktop_mobile_layout_and_palette_contrast(self) -> None:
        desktop = self.open_report({"width": 1440, "height": 900})
        columns = desktop.locator(".analysis-layout").evaluate("node => getComputedStyle(node).gridTemplateColumns")
        self.assertIn(" ", columns)

        mobile = self.open_report({"width": 390, "height": 844})
        self.assertLessEqual(
            mobile.evaluate("document.documentElement.scrollWidth"),
            mobile.evaluate("window.innerWidth"),
        )
        mobile_columns = mobile.locator(".analysis-layout").evaluate("node => getComputedStyle(node).gridTemplateColumns")
        self.assertNotIn(" ", mobile_columns)

        for scheme in ("light", "dark"):
            mobile.emulate_media(color_scheme=scheme)
            colors = mobile.locator("body").evaluate(
                "node => { const style = getComputedStyle(node); return [style.color, style.backgroundColor]; }"
            )
            self.assertGreaterEqual(contrast_ratio(colors[0], colors[1]), 7.0)

    def test_track_map_syncs_comparison_cursor_region_and_reset(self) -> None:
        page = self.open_report({"width": 390, "height": 844})
        track_map = page.get_by_role("img", name="Track map.")
        charts = page.get_by_label("Synchronized telemetry charts")
        self.assertEqual(track_map.get_attribute("data-comparison-lap"), "1")
        self.assertEqual(track_map.locator(".map-comparison").get_attribute("d").count("M"), 2)
        track_map.press("ArrowRight")
        self.assertEqual(charts.get_attribute("data-cursor-distance"), "1.000")
        self.assertEqual(track_map.get_attribute("data-cursor-distance"), "1.000")
        page.get_by_role("combobox", name="Comparison lap").select_option("5")
        self.assertEqual(track_map.get_attribute("data-comparison-lap"), "5")
        page.locator('button[data-region-index="0"]').click()
        self.assertEqual(track_map.get_attribute("data-cursor-distance"), "200.000")
        self.assertEqual(track_map.get_attribute("data-range-start"), "100.000")
        self.assertEqual(track_map.get_attribute("data-range-end"), "350.000")
        track_map.press("Escape")
        self.assertEqual(track_map.get_attribute("data-range-start"), "0.000")
        self.assertEqual(track_map.get_attribute("data-range-end"), "500.000")

    def test_track_map_keys_advance_one_sample_on_a_coarse_grid(self) -> None:
        page = self.open_report(url=self.coarse_report_url)
        track_map = page.get_by_role("img", name="Track map.")
        self.assertIn("L", track_map.locator(".map-reference").get_attribute("d"))
        self.assertEqual(track_map.locator(".map-comparison").get_attribute("d").count("M"), 2)
        track_map.press("ArrowRight")
        self.assertEqual(track_map.get_attribute("data-cursor-distance"), "20.000")
        track_map.press("Shift+ArrowRight")
        self.assertEqual(track_map.get_attribute("data-cursor-distance"), "40.000")
        page.locator('button[data-region-index="0"]').click()
        track_map.press("Home")
        self.assertEqual(track_map.get_attribute("data-cursor-distance"), "100.000")
        track_map.press("ArrowLeft")
        self.assertEqual(track_map.get_attribute("data-cursor-distance"), "100.000")
        track_map.press("End")
        self.assertEqual(track_map.get_attribute("data-cursor-distance"), "340.000")


if __name__ == "__main__":
    unittest.main()
