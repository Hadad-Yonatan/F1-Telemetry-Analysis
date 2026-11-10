import tempfile
import threading
import unittest
from importlib.util import find_spec
from pathlib import Path

from playwright.sync_api import sync_playwright

from f1_telemetry.bundle import write_bundle_manifest
from f1_telemetry.library import import_bundles, init_library
from tests.test_library import UID, write_fixture


@unittest.skipUnless(find_spec("flask"), "requires the optional library extra")
class LibraryBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from werkzeug.serving import WSGIRequestHandler, make_server

        from f1_telemetry.library_web import create_app

        class QuietHandler(WSGIRequestHandler):
            def log_request(self, code="-", size="-"):
                pass

        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        root = Path(temp.name)
        bundle = write_fixture(root, "bundle")
        cls.root = root
        cls.database = root / "index.sqlite3"
        init_library(cls.database)
        import_bundles(cls.database, [bundle])
        server = make_server("127.0.0.1", 0, create_app(cls.database), request_handler=QuietHandler)
        cls.addClassCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        cls.addClassCleanup(server.shutdown)
        cls.base = f"http://127.0.0.1:{server.server_port}"
        playwright = sync_playwright().start()
        cls.addClassCleanup(playwright.stop)
        cls.browser = playwright.chromium.launch()
        cls.addClassCleanup(cls.browser.close)

    def test_filter_open_session_and_use_verified_report_on_mobile(self):
        page = self.browser.new_page(viewport={"width": 390, "height": 844})
        self.addCleanup(page.close)
        failures, errors, requests = [], [], []
        page.on("requestfailed", lambda request: failures.append(request.url))
        page.on("response", lambda response: failures.append(response.url) if response.status >= 400 else None)
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: requests.append(request.url))
        page.goto(self.base)
        self.assertTrue(page.get_by_role("heading", name="Sessions").is_visible())
        page.get_by_label("Track ID").fill("11")
        page.get_by_label("Track ID").press("Enter")
        self.assertTrue(page.get_by_text("No sessions match these filters.").is_visible())
        page.get_by_label("Track ID").fill("10")
        page.get_by_role("button", name="Filter sessions").click()
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
        page.get_by_role("link", name="Test Circuit").click()
        self.assertTrue(page.get_by_text(str(UID), exact=True).is_visible())
        page.get_by_role("link", name="Lap 1", exact=True).click()
        self.assertTrue(page.get_by_role("heading", name="Lap 1", exact=True).is_visible())
        page.get_by_role("link", name="Back to session").click()
        page.get_by_role("link", name="Open standalone report").click()
        report = page.frame_locator('iframe[title="Driver analysis report"]')
        picker = report.get_by_role("combobox", name="Comparison lap")
        picker.select_option("5")
        self.assertEqual(report.locator("body").get_attribute("data-comparison-lap"), "5")
        self.assertEqual(errors, [])
        self.assertEqual(failures, [])
        self.assertTrue(all(url.startswith(self.base + "/") for url in requests), requests)

    def test_desktop_filters_and_comparison_links_are_keyboard_accessible(self):
        page = self.browser.new_page(viewport={"width": 1440, "height": 900})
        self.addCleanup(page.close)
        errors, failures = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        page.on("requestfailed", lambda request: failures.append(request.url))
        page.goto(self.base)
        mode = page.get_by_label("Mode", exact=True)
        mode.select_option("time-trial")
        page.get_by_role("button", name="Filter sessions").press("Enter")
        page.get_by_role("link", name="Test Circuit").press("Enter")
        page.get_by_role("link", name="Lap 1 vs 3").press("Enter")
        self.assertTrue(page.get_by_role("heading", name="Loss regions").is_visible())
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 1440)
        self.assertEqual(errors, [])
        self.assertEqual(failures, [])

    def test_report_frame_cannot_navigate_to_an_external_origin(self):
        bundle = write_fixture(self.root, "navigation-test", reference=1)
        (bundle / "bundle.json").unlink()
        (bundle / "report.html").write_text(
            "<!doctype html><script>location.replace('https://example.invalid/report-proof');</script>",
            encoding="utf-8",
        )
        bundle_id = write_bundle_manifest(bundle)
        import_bundles(self.database, [bundle])
        page = self.browser.new_page()
        self.addCleanup(page.close)
        external = []
        def prevent_request(route):
            external.append(route.request.url)
            route.abort()
        page.route("https://example.invalid/**", prevent_request)
        with page.expect_event("console", predicate=lambda message: "frame-src" in message.text):
            page.goto(self.base + "/reports/" + bundle_id)
        self.assertEqual(external, [])
