import contextlib
import io
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path

from f1_telemetry.bundle import verify_bundle
from f1_telemetry.library import import_bundles, init_library, main
from tests.test_library import UID, write_fixture


@unittest.skipUnless(find_spec("flask"), "requires the optional library extra")
class LibraryWebTests(unittest.TestCase):
    def setUp(self):
        from f1_telemetry.library_web import create_app

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = write_fixture(self.root, "bundle")
        self.bundle_id = verify_bundle(self.bundle).bundle_id
        self.db = self.root / "index.sqlite3"
        init_library(self.db)
        import_bundles(self.db, [self.bundle])
        self.client = create_app(self.db).test_client()
        self.api = f"/api/v1/sessions/{self.bundle_id}"
        self.artifacts = f"/artifacts/{self.bundle_id}"

    def test_pages_and_versioned_api_are_read_only(self):
        database_before = self.db.read_bytes()
        artifacts_before = {path.name: path.read_bytes() for path in self.bundle.iterdir()}
        response = self.client.get("/?mode=time-trial&team_id=3")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Test Circuit", response.data)
        self.assertIn(str(self.db).encode(), response.data)
        result = self.client.get("/api/v1/bundles?track_id=10&page_size=1").json
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["pagination"], {"page": 1, "page_size": 1, "total": 1})
        self.assertEqual(result["data"][0]["session_uid"], str(UID))
        self.assertEqual(self.client.get(self.api).json["data"]["session_uid"], str(UID))
        lap = self.client.get(self.api + "/laps/1").json["data"]
        self.assertTrue(lap["eligible"])
        self.assertEqual(self.client.get(self.api + "/comparisons/1").status_code, 200)
        self.assertTrue(self.client.get(self.api + "/comparisons/1/regions").json["data"])
        self.assertEqual(self.client.get(self.api + "/comparisons/1/regions/0").status_code, 200)
        self.assertEqual(self.client.get(self.api + "/corners/test-t01").json["data"]["number"], 1)
        self.assertEqual(self.client.get("/api/v1/setups/" + lap["setup_fingerprint"]).json["data"]["values"]["front_wing"], 25)
        self.assertEqual(self.client.get(f"/sessions/{self.bundle_id}/laps/1").content_type, "text/html; charset=utf-8")
        artifact_info = self.client.get(self.api + "/artifacts").json["data"]
        self.assertNotIn("root", artifact_info[0])
        self.assertIn(self.artifacts + "/report.html", {row["url"] for row in artifact_info})
        self.assertEqual(self.client.post(self.api).status_code, 405)
        self.assertEqual(self.client.put(self.artifacts + "/analysis.json", data=b"new").status_code, 405)
        self.assertEqual(self.db.read_bytes(), database_before)
        self.assertEqual({path.name: path.read_bytes() for path in self.bundle.iterdir()}, artifacts_before)

    def test_bad_filters_missing_objects_and_untrusted_hosts(self):
        for query in (
            "page=0", "page_size=101", "track_id=text", "mode=other",
            "team_id=3&team_id=4", "algorithm=1", "created_from=19700101",
            "created_from=1970-01-02&created_through=1970-01-01",
        ):
            with self.subTest(query=query):
                response = self.client.get("/api/v1/sessions?" + query)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json["error"]["status"], 400)
        self.assertEqual(self.client.get("/api/v1/sessions/" + "0" * 64).status_code, 404)
        self.assertEqual(self.client.get(self.api + "/laps/101").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/sessions", headers={"Host": "example.invalid"}).status_code, 400)
        self.assertEqual(self.client.get("/api/v1/sessions", base_url="http://[::1]:8765").status_code, 200)

    def test_artifacts_are_checked_against_index_and_sandboxed(self):
        report = self.bundle / "report.html"
        raw = report.read_bytes()
        response = self.client.get(self.artifacts + "/report.html")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, raw)
        self.assertIn("sandbox allow-scripts", response.headers["Content-Security-Policy"])
        self.assertNotIn("allow-same-origin", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        for path in ("bundle.json", "../sample.f1cap", "%2e%2e%2fsample.f1cap", "not-listed.html"):
            self.assertEqual(self.client.get(self.artifacts + "/" + path).status_code, 404)
        report.write_bytes(b"!" + raw[1:])
        self.assertEqual(self.client.get(self.artifacts + "/report.html").status_code, 409)
        report.unlink()
        external = self.root / "external.html"
        external.write_bytes(raw)
        report.symlink_to(external)
        self.assertEqual(self.client.get(self.artifacts + "/report.html").status_code, 404)

    def test_serve_refuses_non_loopback_and_invalid_port(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(["serve", "--database", str(self.db), "--host", "0.0.0.0"]), 1)
            self.assertEqual(main(["serve", "--database", str(self.db), "--port", "0"]), 1)
        self.assertIn("host must be localhost", err.getvalue())
        self.assertIn("port must be between", err.getvalue())
