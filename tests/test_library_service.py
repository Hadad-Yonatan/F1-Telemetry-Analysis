import tempfile
import unittest
from datetime import date
from pathlib import Path

from f1_telemetry.bundle import verify_bundle
from f1_telemetry.capture_format import CaptureWriter
from f1_telemetry.library import import_bundles, init_library
from f1_telemetry.library_service import LibraryService, PageRequest, SessionFilters
from tests.test_library import UID, write_fixture


class LibraryServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.database = root / "index.sqlite3"
        old = root / "old"
        old.mkdir()
        self.first = write_fixture(old, "one")
        self.second = write_fixture(old, "two", reference=1)
        newer = root / "newer"
        newer.mkdir()
        with CaptureWriter(newer / "sample.f1cap", created_unix_ns=86_400_000_000_000):
            pass
        self.third = write_fixture(newer, "three")
        self.ids = tuple(verify_bundle(bundle).bundle_id for bundle in (self.first, self.second, self.third))
        init_library(self.database)
        import_bundles(self.database, [self.second, self.first, self.third])
        self.service = LibraryService(self.database)

    def test_stable_pagination_orders_by_date_then_bundle_id(self):
        expected = [self.ids[2], *sorted(self.ids[:2])]
        self.assertEqual(
            [session.bundle_id for session in self.service.list_sessions().items], expected
        )
        for number in (1, 2, 3, 4):
            with self.subTest(page=number):
                page = self.service.list_sessions(page=PageRequest(number, 1))
                self.assertEqual([row.bundle_id for row in page.items], expected[number - 1:number])
                self.assertEqual((page.page, page.page_size, page.total), (number, 1, 3))

    def test_combined_filters_use_utc_day_and_eligible_lap_setup(self):
        fingerprint = self.service.get_lap(self.ids[2], 1).setup_fingerprint
        result = self.service.list_sessions(SessionFilters(
            track_id=10, created_from=date(1970, 1, 2), created_through=date(1970, 1, 2),
            mode="time-trial", team_id=3, setup_fingerprint=fingerprint, algorithm_version="1.1.0",
        ))
        self.assertEqual(result.total, 1)
        self.assertEqual(result.items[0].bundle_id, self.ids[2])
        self.assertEqual(result.items[0].mode, "time-trial")
        for filters in (
            SessionFilters(track_id=11, mode="time-trial"),
            SessionFilters(team_id=42),
            SessionFilters(mode="practice"),
            SessionFilters(algorithm_version="' OR 1=1 --"),
            SessionFilters(setup_fingerprint="0" * 64),
            SessionFilters(created_from=date.max),
            SessionFilters(created_through=date(1969, 12, 31)),
        ):
            with self.subTest(filters=filters):
                self.assertEqual(self.service.list_sessions(filters).total, 0)
        before_midnight = self.service.list_sessions(SessionFilters(created_through=date(1970, 1, 1)))
        self.assertEqual({row.bundle_id for row in before_midnight.items}, set(self.ids[:2]))

    def test_detail_queries_are_read_only_and_preserve_source_identifiers(self):
        database_before = self.database.read_bytes()
        reports_before = {file.name: file.read_bytes() for file in self.first.iterdir()}
        bundle_id = self.ids[0]
        detail = self.service.get_session(bundle_id)
        self.assertEqual(detail.summary.session_uid, str(UID))
        self.assertEqual(detail.context["session_uid"], str(UID))
        self.assertEqual(detail.summary.track_name, "Test Circuit")
        laps = self.service.list_laps(bundle_id)
        self.assertEqual([lap.number for lap in laps], [1, 2, 3, 4, 5])
        self.assertFalse(self.service.get_lap(bundle_id, 2).eligible)
        self.assertEqual(self.service.get_setup(laps[0].setup_fingerprint)["front_wing"], 25)
        comparisons = self.service.list_comparisons(bundle_id)
        self.assertEqual([comparison["comparison_lap"] for comparison in comparisons], [1, 5])
        self.assertEqual(self.service.get_comparison(bundle_id, 1), comparisons[0])
        self.assertEqual(self.service.list_regions(bundle_id, 1), tuple(comparisons[0]["loss_regions"]))
        self.assertEqual([corner["number"] for corner in self.service.list_corners(bundle_id)], [1, 2])
        artifacts = self.service.list_artifacts(bundle_id)
        artifact = self.service.get_artifact(bundle_id, "report.html")
        self.assertIn(artifact.artifact, artifacts)
        self.assertEqual(artifact.bundle_root, self.first.resolve())
        self.assertEqual(self.database.read_bytes(), database_before)
        self.assertEqual({file.name: file.read_bytes() for file in self.first.iterdir()}, reports_before)

    def test_unknown_records_and_invalid_parameters_are_distinct(self):
        missing = "0" * 64
        self.assertIsNone(self.service.get_session(missing))
        self.assertEqual(self.service.list_laps(missing), ())
        self.assertIsNone(self.service.get_lap(self.ids[0], 100))
        self.assertIsNone(self.service.get_comparison(self.ids[0], 100))
        self.assertIsNone(self.service.get_setup(missing))
        self.assertIsNone(self.service.get_artifact(self.ids[0], "../sample.f1cap"))
        for operation in (
            lambda: self.service.get_session("not a digest"),
            lambda: PageRequest(0, 25),
            lambda: PageRequest(1, 101),
            lambda: PageRequest(1 << 63, 100),
            lambda: SessionFilters(mode="benchmark"),
            lambda: SessionFilters(track_id=True),
            lambda: SessionFilters(created_from=date.max, created_through=date.min),
            lambda: SessionFilters(setup_fingerprint="bad"),
        ):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                operation()
