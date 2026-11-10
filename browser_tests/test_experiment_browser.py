import tempfile
import threading
import unittest
from importlib.util import find_spec
from pathlib import Path

from playwright.sync_api import sync_playwright

from f1_telemetry.experiment import run_experiment
from f1_telemetry.experiment_model import parse_manifest
from f1_telemetry.library import import_bundles, init_library
from tests.test_experiment import manifest_document, write_trial_bundle


@unittest.skipUnless(find_spec("flask"), "requires the optional library extra")
class ExperimentBrowserTests(unittest.TestCase):
    def test_report_is_offline_and_library_experiment_is_read_only(self):
        from werkzeug.serving import WSGIRequestHandler, make_server
        from f1_telemetry.library_web import create_app

        class QuietHandler(WSGIRequestHandler):
            def log_request(self, code="-", size="-"):
                pass

        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        database = root / "library.sqlite3"
        init_library(database)
        control = write_trial_bundle(root, "control")
        candidate = write_trial_bundle(root, "candidate", wing=30, offset=2)
        import_bundles(database, [control, candidate])
        output = root / "experiment"
        experiment_id = run_experiment(
            parse_manifest(manifest_document(control, candidate, label="<Candidate>")), database, output,
        )
        database_before = database.read_bytes()
        server = make_server("127.0.0.1", 0, create_app(database), request_handler=QuietHandler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"

        with sync_playwright() as playwright, playwright.chromium.launch() as browser:
            for viewport in ({"width": 390, "height": 844}, {"width": 1280, "height": 900}):
                page = browser.new_page(viewport=viewport)
                requests, failures, errors = [], [], []
                page.on("request", lambda request: requests.append(request.url))
                page.on("requestfailed", lambda request: failures.append(request.url))
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
                page.goto((output / "report.html").as_uri())
                self.assertTrue(page.get_by_role("heading", name="Controlled setup experiment").is_visible())
                self.assertTrue(page.get_by_text("Candidate: <Candidate>", exact=False).is_visible())
                self.assertTrue(page.get_by_role("rowheader", name="pace.lap_time (s)").is_visible())
                self.assertTrue(all(url.startswith("file:") for url in requests))
                requests.clear()
                page.goto(base)
                page.get_by_role("link", name="Setup experiments", exact=True).press("Enter")
                page.get_by_role("link", name="Control vs <Candidate>").press("Enter")
                self.assertTrue(page.get_by_role("heading", name="Control vs <Candidate>").is_visible())
                self.assertTrue(page.get_by_role("link", name="Control", exact=True).is_visible())
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), viewport["width"])
                api = page.request.get(base + "/api/v1/experiments/" + experiment_id)
                self.assertEqual(api.json()["data"]["experiment_id"], experiment_id)
                self.assertEqual(page.request.post(base + "/api/v1/experiments").status, 405)
                self.assertTrue(all(url.startswith(base + "/") for url in requests))
                self.assertEqual(errors, [])
                self.assertEqual(failures, [])
                page.close()
        self.assertEqual(database.read_bytes(), database_before)
