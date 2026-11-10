import contextlib
import hashlib
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from f1.packets import PacketParticipantsData

from f1_telemetry.analysis_output import write_analysis_bundle
from f1_telemetry.bundle import BundleError, verify_bundle, write_bundle_manifest
from f1_telemetry.capture_format import CaptureWriter
from f1_telemetry.lap_analysis import analyze_capture
from f1_telemetry.library import (
    LibraryError,
    import_bundles,
    init_library,
    main,
    open_database,
    rebuild_library,
    verify_library,
)
from f1_telemetry.session_context import ContextCollector
from tests.test_analysis_output import matching_catalog, prepared_session
from tests.test_session_context import add, packet, session_packet, setup_packet


UID = (1 << 64) - 1


def write_fixture(root: Path, name: str, *, reference: int | None = None) -> Path:
    capture = root / "sample.f1cap"
    if not capture.exists():
        with CaptureWriter(capture, created_unix_ns=123):
            pass
    session = prepared_session()
    laps = tuple(
        replace(lap, samples=tuple(replace(sample, session_uid=UID) for sample in lap.samples))
        for lap in session.laps
    )
    collector = ContextCollector(UID, 0)
    add(collector, session_packet(1, uid=UID))
    add(collector, setup_packet(2, 25, uid=UID))
    participant = packet(PacketParticipantsData, 4, 3, uid=UID)
    participant.num_active_cars = 1
    participant.participants[0].team_id = 3
    participant.participants[0].name = b"Driver"
    add(collector, participant)
    session = replace(session, session_uid=UID, laps=laps, context=collector.finish(laps))
    with patch("f1_telemetry.lap_analysis.prepare_session", return_value=session):
        result = analyze_capture(capture, reference_lap=reference, corner_catalog=matching_catalog())
    bundle = root / name
    write_analysis_bundle(result, bundle)
    return bundle


class BundleLibraryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundles = self.root / "bundles"
        self.bundles.mkdir()
        self.bundle = write_fixture(self.bundles, "one")
        self.db = self.root / "library.sqlite3"

    def query(self, sql):
        with contextlib.closing(open_database(self.db)) as connection:
            return connection.execute(sql).fetchall()

    def test_consistency_descriptor_cannot_be_bypassed_by_a_regenerated_manifest(self):
        artifact = self.bundle / "consistency.json"
        artifact.write_bytes(artifact.read_bytes() + b" ")
        (self.bundle / "bundle.json").unlink()
        write_bundle_manifest(self.bundle)
        with self.assertRaisesRegex(BundleError, "does not match analysis.json"):
            verify_bundle(self.bundle)

    def test_manifest_hashes_exact_files_and_does_not_change_analysis_identity(self):
        bundle = verify_bundle(self.bundle)
        manifest = json.loads((self.bundle / "bundle.json").read_bytes())
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["bundle_id"], hashlib.sha256((self.bundle / "analysis.json").read_bytes()).hexdigest())
        self.assertEqual(
            [entry["path"] for entry in manifest["artifacts"]],
            sorted(path.name for path in self.bundle.iterdir() if path.name != "bundle.json"),
        )
        for artifact in bundle.artifacts:
            raw = (self.bundle / artifact.path).read_bytes()
            self.assertEqual((artifact.size_bytes, artifact.sha256), (len(raw), hashlib.sha256(raw).hexdigest()))

    def test_missing_tampered_extra_and_symlinked_artifacts_are_rejected(self):
        report = self.bundle / "report.html"
        saved = report.read_bytes()
        report.unlink()
        with self.assertRaisesRegex(BundleError, "missing artifacts"):
            verify_bundle(self.bundle)
        report.write_bytes(saved + b"changed")
        with self.assertRaisesRegex(BundleError, "SHA-256"):
            verify_bundle(self.bundle)
        report.write_bytes(saved)
        extra = self.bundle / "extra.txt"
        extra.write_text("not listed")
        with self.assertRaisesRegex(BundleError, "unexpected artifacts"):
            verify_bundle(self.bundle)
        manifest = self.bundle / "bundle.json"
        manifest_bytes = manifest.read_bytes()
        contents = json.loads(manifest_bytes)
        contents["artifacts"].append({
            "path": "extra.txt", "size_bytes": len(extra.read_bytes()),
            "sha256": hashlib.sha256(extra.read_bytes()).hexdigest(),
        })
        contents["artifacts"].sort(key=lambda entry: entry["path"])
        manifest.write_text(json.dumps(contents))
        with self.assertRaisesRegex(BundleError, "unexpected artifacts"):
            verify_bundle(self.bundle)
        manifest.write_bytes(manifest_bytes)
        extra.unlink()
        report.unlink()
        outside = self.root / "outside.html"
        outside.write_bytes(saved)
        report.symlink_to(outside)
        with self.assertRaisesRegex(BundleError, "unsafe artifact"):
            verify_bundle(self.bundle)

    def test_manifest_writer_refuses_incomplete_bundle(self):
        manifest = self.bundle / "bundle.json"
        manifest.unlink()
        (self.bundle / "report.html").unlink()
        with self.assertRaisesRegex(BundleError, "missing artifacts: report.html"):
            write_bundle_manifest(self.bundle)
        self.assertFalse(manifest.exists())

    def test_manifest_cannot_reference_parent_or_duplicate_paths(self):
        path = self.bundle / "bundle.json"
        source = json.loads(path.read_bytes())
        for invalid, match in (("../analysis.json", "unsafe"), ("analysis.json", "unique and sorted")):
            doc = {**source, "artifacts": [*source["artifacts"], {**source["artifacts"][0], "path": invalid}]}
            path.write_text(json.dumps(doc))
            with self.subTest(path=invalid), self.assertRaisesRegex(BundleError, match):
                verify_bundle(self.bundle)

    def test_legacy_bundle_checks_csv_finish_and_requires_no_html_or_markdown(self):
        (self.bundle / "bundle.json").unlink()
        (self.bundle / "report.html").unlink()
        (self.bundle / "report.md").unlink()
        self.assertFalse(verify_bundle(self.bundle).manifested)
        csv = next(self.bundle.glob("*.csv"))
        rows = csv.read_text().splitlines()
        cells = rows[-1].split(",")
        cells[3] = "999.0"
        csv.write_text("\n".join([*rows[:-1], ",".join(cells)]) + "\n")
        with self.assertRaisesRegex(BundleError, "finish does not match"):
            verify_bundle(self.bundle)

    def test_import_indexes_unsigned_uid_and_setup_and_is_duplicate_safe(self):
        init_library(self.db)
        self.assertEqual(import_bundles(self.db, [self.bundle, self.bundle]), (1, 1))
        init_library(self.db)
        self.assertEqual(verify_library(self.db), 1)
        self.assertEqual(self.query("SELECT session_uid, typeof(session_uid), track_id, session_type, team_id FROM sessions"),
                         [(str(UID), "text", 10, 18, 3)])
        self.assertEqual(self.query("SELECT lap_number FROM laps WHERE eligible=1 ORDER BY lap_number"), [(1,), (3,), (5,)])
        self.assertEqual(self.query("SELECT count(DISTINCT setup_fingerprint) FROM laps"), [(1,)])
        self.assertEqual(self.query("SELECT comparison_lap FROM comparisons ORDER BY comparison_lap"), [(1,), (5,)])
        self.assertEqual(self.query("SELECT turn_id FROM corners ORDER BY turn_number"), [("test-t01",), ("test-t02",)])
        self.assertTrue(self.query("SELECT region_index FROM regions"))
        self.assertTrue(all("/" not in path for (path,) in self.query("SELECT path FROM artifacts")))

    def test_import_failure_rolls_back_previous_bundle_in_same_command(self):
        init_library(self.db)
        broken = write_fixture(self.bundles, "broken", reference=1)
        (broken / "report.html").unlink()
        with self.assertRaises(BundleError):
            import_bundles(self.db, [self.bundle, broken])
        self.assertEqual(verify_library(self.db), 0)
        self.assertEqual(self.query("SELECT count(*) FROM captures"), [(0,)])

    def test_rebuild_has_stable_index_and_failure_preserves_existing_database(self):
        other = write_fixture(self.bundles, "two", reference=1)
        clone = self.bundles / "three"
        shutil.copytree(other, clone)
        self.assertEqual(rebuild_library(self.db, self.bundles), (2, 1))
        query = "SELECT bundle_id, root FROM bundles ORDER BY bundle_id"
        before = self.query(query)
        self.assertEqual(rebuild_library(self.db, self.bundles), (2, 1))
        self.assertEqual(self.query(query), before)
        self.assertEqual(verify_library(self.db), 2)
        original = self.db.read_bytes()
        (clone / "analysis.json").unlink()
        with self.assertRaisesRegex(BundleError, "missing analysis.json"):
            rebuild_library(self.db, self.bundles)
        self.assertEqual(self.db.read_bytes(), original)
        self.assertEqual(list(self.db.parent.glob(".library-rebuild-*")), [])

    def test_verify_detects_changed_legacy_artifacts_and_wrong_database_version(self):
        (self.bundle / "bundle.json").unlink()
        init_library(self.db)
        import_bundles(self.db, [self.bundle])
        self.assertEqual(verify_library(self.db), 1)
        (self.bundle / "report.md").write_text("changed")
        with self.assertRaisesRegex(LibraryError, "indexed bundle changed"):
            verify_library(self.db)
        with contextlib.closing(sqlite3.connect(self.db)) as connection:
            connection.execute("PRAGMA user_version=9")
        with self.assertRaisesRegex(LibraryError, "schema 9"):
            verify_library(self.db)

    def test_corrupt_database_is_reported_without_overwriting(self):
        self.db.write_bytes(b"not a database")
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertEqual(main(["verify", "--database", str(self.db)]), 1)
            self.assertEqual(main(["rebuild", str(self.bundles), "--database", str(self.db)]), 1)
        self.assertIn("Library error:", error.getvalue())
        self.assertEqual(self.db.read_bytes(), b"not a database")
