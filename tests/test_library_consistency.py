import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from f1_telemetry.bundle import BundleError, verify_bundle, write_bundle_manifest
from f1_telemetry.library import LibraryError, import_bundles, init_library, open_database, rebuild_library, verify_library
from f1_telemetry.library_service import LibraryService, SessionFilters
from tests.test_library import write_fixture


class LibraryConsistencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundles = self.root / "bundles"
        self.bundles.mkdir()
        self.bundle = write_fixture(self.bundles, "with-result")
        self.verified = verify_bundle(self.bundle)
        self.db = self.root / "library.sqlite3"
        init_library(self.db)
        self.service = LibraryService(self.db)

    def downgrade_to_foundation_schema(self):
        # Equivalent to the foundation index after importing these bundles,
        # before it knew about the consistency-specific tables.
        with closing(sqlite3.connect(self.db)) as connection, connection:
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND (name LIKE 'consistency_%' OR name LIKE 'motion_%' OR name LIKE 'experiment%')"
            ).fetchall()
            for name, in tables:
                connection.execute(f'DROP TABLE "{name}"')
            connection.execute("PRAGMA user_version=1")

    def make_legacy_bundle(self):
        root = write_fixture(self.bundles, "pre-consistency", reference=1)
        analysis = root / "analysis.json"
        document = json.loads(analysis.read_bytes())
        document.pop("consistency")
        analysis.write_text(json.dumps(document), encoding="utf-8")
        (root / "consistency.json").unlink()
        (root / "bundle.json").unlink()
        return root

    def test_migration_backfills_previous_index_and_rebuild_preserves_order(self):
        legacy = self.make_legacy_bundle()
        import_bundles(self.db, [legacy, self.bundle])
        self.downgrade_to_foundation_schema()
        with self.assertRaisesRegex(LibraryError, "schema 1"):
            self.service.get_consistency(self.verified.bundle_id)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone(), (1,))

        init_library(self.db)
        self.assertEqual(verify_library(self.db), 2)
        self.assertEqual(self.service.get_consistency(self.verified.bundle_id), self.verified.consistency)
        self.assertIsNone(self.service.get_consistency(verify_bundle(legacy).bundle_id))
        self.assertEqual(import_bundles(self.db, [self.bundle]), (0, 1))
        ordered = self.service.list_sessions()
        self.assertEqual(rebuild_library(self.db, self.bundles), (2, 0))
        self.assertEqual(self.service.list_sessions(), ordered)
        self.assertEqual(self.service.get_consistency(self.verified.bundle_id), self.verified.consistency)
        self.assertEqual(self.service.list_sessions(SessionFilters(consistency="available")).total, 1)
        self.assertEqual(self.service.list_sessions(SessionFilters(consistency="unavailable")).total, 0)
        self.assertEqual(self.service.list_sessions(SessionFilters(corner_consistency="not-indexed", track_id=10)).total, 1)
        self.assertEqual(self.service.list_sessions(SessionFilters(consistency="available", track_id=11)).total, 0)

    def test_migration_interrupt_rolls_back_ddl_rows_and_version(self):
        import_bundles(self.db, [self.bundle])
        self.downgrade_to_foundation_schema()
        with patch("f1_telemetry.library.index_consistency", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                init_library(self.db)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone(), (1,))
            self.assertEqual(connection.execute("SELECT count(*) FROM bundles").fetchone(), (1,))
            self.assertIsNone(connection.execute(
                "SELECT name FROM sqlite_master WHERE name='consistency_runs'"
            ).fetchone())
        init_library(self.db)
        self.assertEqual(verify_library(self.db), 1)

    def test_failed_import_rolls_back_its_automatic_migration(self):
        import_bundles(self.db, [self.bundle])
        self.downgrade_to_foundation_schema()
        with self.assertRaises(BundleError):
            import_bundles(self.db, [self.root / "missing"])
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone(), (1,))
            self.assertEqual(connection.execute("SELECT count(*) FROM bundles").fetchone(), (1,))
            self.assertIsNone(connection.execute(
                "SELECT name FROM sqlite_master WHERE name='consistency_runs'"
            ).fetchone())

    def test_hash_valid_but_structurally_invalid_consistency_rolls_back_import(self):
        malformed = write_fixture(self.bundles, "invalid", reference=1)
        consistency = malformed / "consistency.json"
        document = json.loads(consistency.read_bytes())
        document["laps"][0]["lap_number"] = 2  # official lap 2 was ineligible
        raw = json.dumps(document).encode()
        consistency.write_bytes(raw)
        analysis = malformed / "analysis.json"
        data = json.loads(analysis.read_bytes())
        data["consistency"]["sha256"] = sha256(raw).hexdigest()
        analysis.write_text(json.dumps(data), encoding="utf-8")
        (malformed / "bundle.json").unlink()
        write_bundle_manifest(malformed)
        with self.assertRaisesRegex(BundleError, "lap number"):
            import_bundles(self.db, [self.bundle, malformed])
        self.assertEqual(self.service.list_sessions().total, 0)

    def test_verify_detects_changed_index_without_touching_artifacts(self):
        import_bundles(self.db, [self.bundle])
        with closing(open_database(self.db, read_only=False)) as connection, connection:
            connection.execute("UPDATE consistency_runs SET slope_s_per_attempt=0.42")
        with self.assertRaisesRegex(LibraryError, "indexed consistency changed"):
            verify_library(self.db)

    def test_missing_corner_name_is_a_controlled_import_error(self):
        analysis = self.bundle / "analysis.json"
        document = json.loads(analysis.read_bytes())
        del document["corner_catalog"]["selected_track"]["turns"][0]["display_name"]
        analysis.write_text(json.dumps(document), encoding="utf-8")
        (self.bundle / "bundle.json").unlink()
        write_bundle_manifest(self.bundle)
        with self.assertRaisesRegex(BundleError, "turn display name"):
            import_bundles(self.db, [self.bundle])
        self.assertEqual(self.service.list_sessions().total, 0)

    def test_verify_detects_changed_corner_metric_label(self):
        import_bundles(self.db, [self.bundle])
        with closing(open_database(self.db, read_only=False)) as connection, connection:
            connection.execute("UPDATE consistency_corner_summaries SET metric='wrong' WHERE ordinal=0")
        with self.assertRaisesRegex(BundleError, "invalid indexed consistency corner metric"):
            verify_library(self.db)
