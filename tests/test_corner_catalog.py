import contextlib
import copy
import io
import json
import tempfile
import unittest
from importlib import resources
from pathlib import Path
from unittest.mock import patch

from f1_telemetry.corner_catalog import (
    CatalogSelection,
    CornerAssociation,
    CornerComplex,
    TrackDefinition,
    TurnDefinition,
    associate_region,
    disabled_catalog_selection,
    format_corner_label,
    load_bundled_corner_catalog,
    load_corner_catalog,
    select_catalog_track,
)
from f1_telemetry.lap_analysis import build_parser, main


def valid_document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "catalog_id": "test-corners-v1",
        "packet_format": 2025,
        "game_year": 25,
        "tracks": [
            {
                "track_id": 99,
                "display_name": "Test Circuit",
                "track_length_m": 1000,
                "turns": [
                    {
                        "turn_id": "test-t01",
                        "number": 1,
                        "display_name": "First",
                        "direction": "left",
                        "entry_distance_m": 100.0,
                        "apex_distance_m": 125.0,
                        "exit_distance_m": 150.0,
                    },
                    {
                        "turn_id": "test-t02",
                        "number": 2,
                        "display_name": "Second",
                        "direction": "right",
                        "entry_distance_m": 150.0,
                        "apex_distance_m": 175.0,
                        "exit_distance_m": 200.0,
                    },
                    {
                        "turn_id": "test-t03",
                        "number": 3,
                        "display_name": "Third",
                        "direction": "left",
                        "entry_distance_m": 300.0,
                        "apex_distance_m": 325.0,
                        "exit_distance_m": 350.0,
                    },
                ],
                "complexes": [
                    {
                        "complex_id": "test-first-second",
                        "display_name": "First/Second",
                        "turn_ids": ["test-t01", "test-t02"],
                    }
                ],
            }
        ],
    }


class CatalogParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "catalog.json"

    def load(self, document: object):
        self.path.write_text(
            json.dumps(document, ensure_ascii=False),
            encoding="utf-8",
        )
        return load_corner_catalog(self.path)

    def assert_invalid(self, document: object, message: str) -> None:
        with self.assertRaisesRegex(ValueError, message):
            self.load(document)

    def test_parses_strict_catalog_and_hashes_exact_bytes(self) -> None:
        catalog = self.load(valid_document())
        self.assertEqual(catalog.metadata.catalog_id, "test-corners-v1")
        self.assertEqual(catalog.tracks[0].turns[0].display_name, "First")
        self.assertEqual(len(catalog.metadata.sha256), 64)

    def test_rejects_unknown_and_missing_fields_at_every_level(self) -> None:
        locations = (
            ((), "extra", "tracks"),
            (("tracks", 0), "extra", "turns"),
            (("tracks", 0, "turns", 0), "extra", "direction"),
            (("tracks", 0, "complexes", 0), "extra", "turn_ids"),
        )
        for path, unknown, required in locations:
            with self.subTest(path=path, kind="unknown"):
                document = valid_document()
                target = document
                for component in path:
                    target = target[component]  # type: ignore[index]
                target[unknown] = 1  # type: ignore[index]
                self.assert_invalid(document, "unknown fields")
            with self.subTest(path=path, kind="missing"):
                document = valid_document()
                target = document
                for component in path:
                    target = target[component]  # type: ignore[index]
                del target[required]  # type: ignore[index]
                self.assert_invalid(document, "missing fields")

    def test_rejects_booleans_and_non_finite_or_out_of_range_numbers(self) -> None:
        mutations = (
            (("schema_version",), True, "integer"),
            (("tracks", 0, "track_id"), True, "integer"),
            (("tracks", 0, "turns", 0, "number"), True, "integer"),
            (("tracks", 0, "turns", 0, "entry_distance_m"), True, "number"),
            (("tracks", 0, "turns", 0, "entry_distance_m"), -1, "within"),
            (("tracks", 0, "turns", 0, "exit_distance_m"), 1001, "within"),
        )
        for path, value, message in mutations:
            with self.subTest(path=path, value=value):
                document = valid_document()
                target = document
                for component in path[:-1]:
                    target = target[component]  # type: ignore[index]
                target[path[-1]] = value  # type: ignore[index]
                self.assert_invalid(document, message)
        self.path.write_text(
            json.dumps(valid_document()).replace("100.0", "NaN", 1),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "non-finite"):
            load_corner_catalog(self.path)

    def test_rejects_duplicate_ids_numbers_and_json_fields(self) -> None:
        mutations = []
        duplicate_track = valid_document()
        duplicate_track["tracks"].append(copy.deepcopy(duplicate_track["tracks"][0]))  # type: ignore[index,union-attr]
        mutations.append((duplicate_track, "duplicate track"))
        duplicate_turn = valid_document()
        duplicate_turn["tracks"][0]["turns"][1]["turn_id"] = "test-t01"  # type: ignore[index]
        mutations.append((duplicate_turn, "duplicate turn IDs"))
        duplicate_number = valid_document()
        duplicate_number["tracks"][0]["turns"][1]["number"] = 1  # type: ignore[index]
        mutations.append((duplicate_number, "duplicate turn numbers"))
        duplicate_complex = valid_document()
        duplicate_complex["tracks"][0]["complexes"].append(  # type: ignore[index,union-attr]
            copy.deepcopy(duplicate_complex["tracks"][0]["complexes"][0])  # type: ignore[index]
        )
        mutations.append((duplicate_complex, "duplicate complex"))
        for document, message in mutations:
            with self.subTest(message=message):
                self.assert_invalid(document, message)
        self.path.write_text(
            '{"schema_version":1,"schema_version":1}', encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "duplicate JSON field"):
            load_corner_catalog(self.path)

    def test_rejects_invalid_ids_names_direction_and_game_identity(self) -> None:
        mutations = (
            (("catalog_id",), "Bad ID", "lowercase ASCII"),
            (("tracks", 0, "turns", 0, "turn_id"), "T 1", "lowercase ASCII"),
            (("tracks", 0, "display_name"), "", "non-empty"),
            (("tracks", 0, "turns", 0, "display_name"), " First", "trimmed"),
            (("tracks", 0, "turns", 0, "direction"), "straight", "left or right"),
            (("packet_format",), 2024, "packet format"),
            (("game_year",), 24, "game year"),
        )
        for path, value, message in mutations:
            with self.subTest(path=path):
                document = valid_document()
                target = document
                for component in path[:-1]:
                    target = target[component]  # type: ignore[index]
                target[path[-1]] = value  # type: ignore[index]
                self.assert_invalid(document, message)

    def test_rejects_unordered_invalid_and_overlapping_turn_windows(self) -> None:
        mutations = (
            ((1, "number"), 0, "at least 1"),
            ((1, "number"), 1, "duplicate turn numbers"),
            ((0, "entry_distance_m"), 130, "entry distance cannot exceed"),
            ((0, "apex_distance_m"), 151, "apex distance cannot exceed"),
            ((1, "entry_distance_m"), 149, "cannot overlap"),
            ((1, "entry_distance_m"), 90, "ordered by distance"),
        )
        for (turn_index, field), value, message in mutations:
            with self.subTest(turn=turn_index, field=field):
                document = valid_document()
                document["tracks"][0]["turns"][turn_index][field] = value  # type: ignore[index]
                self.assert_invalid(document, message)
        document = valid_document()
        document["tracks"][0]["turns"][0]["apex_distance_m"] = 100  # type: ignore[index]
        document["tracks"][0]["turns"][0]["exit_distance_m"] = 100  # type: ignore[index]
        self.assert_invalid(document, "positive length")

    def test_rejects_invalid_complex_membership(self) -> None:
        mutations = (
            (["test-t01"], "at least two"),
            (["test-t01", "missing"], "references missing"),
            (["test-t01", "test-t01"], "duplicate turn IDs"),
            (["test-t01", "test-t03"], "ordered and consecutive"),
            (["test-t02", "test-t01"], "ordered and consecutive"),
        )
        for members, message in mutations:
            with self.subTest(members=members):
                document = valid_document()
                document["tracks"][0]["complexes"][0]["turn_ids"] = members  # type: ignore[index]
                self.assert_invalid(document, message)
        document = valid_document()
        document["tracks"][0]["complexes"].append(  # type: ignore[index,union-attr]
            {
                "complex_id": "test-second-third",
                "display_name": "Second/Third",
                "turn_ids": ["test-t02", "test-t03"],
            }
        )
        self.assert_invalid(document, "assigned more than once")

    def test_bundled_catalog_contains_the_locked_tracks(self) -> None:
        catalog = load_bundled_corner_catalog()
        self.assertEqual(
            [(track.track_id, track.track_length_m) for track in catalog.tracks],
            [(5, 3323), (10, 7007), (11, 5798)],
        )

    def test_package_contract_includes_the_bundled_catalog(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        packaged = resources.files("f1_telemetry").joinpath(
            "data", "corner-catalog-v1.json"
        )
        self.assertTrue(packaged.is_file())
        self.assertIn(
            'f1_telemetry = ["data/*.json"]',
            (repository / "pyproject.toml").read_text(encoding="utf-8"),
        )


class CatalogAssociationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalog.json"
            path.write_text(json.dumps(valid_document()), encoding="utf-8")
            cls.catalog = load_corner_catalog(path)
        cls.track = cls.catalog.tracks[0]

    def test_selects_exact_track_and_handles_unknown_disabled_and_mismatch(self) -> None:
        selection = select_catalog_track(self.catalog, 99, 1000)
        self.assertEqual(selection.status, "matched")
        self.assertEqual(selection.track, self.track)
        self.assertEqual(
            select_catalog_track(self.catalog, 98, 1000).status,
            "track_not_found",
        )
        self.assertEqual(disabled_catalog_selection().status, "disabled")
        with self.assertRaisesRegex(ValueError, "does not match"):
            select_catalog_track(self.catalog, 99, 999)

    def test_selection_states_are_strict(self) -> None:
        with self.assertRaises(ValueError):
            CatalogSelection(status="matched", metadata=None, track=None)
        with self.assertRaises(ValueError):
            CatalogSelection(
                status="disabled",
                metadata=self.catalog.metadata,
                track=None,
            )

    def test_associates_single_multiple_ties_complexes_and_no_match(self) -> None:
        single = associate_region(self.track, 110, 140)
        assert single is not None
        self.assertEqual(single.primary_turn_id, "test-t01")
        self.assertEqual(single.overlapping_turn_ids, ("test-t01",))
        self.assertEqual(single.complex_ids, ("test-first-second",))
        self.assertEqual(format_corner_label(self.track, single), "T1 First")

        multiple = associate_region(self.track, 125, 175)
        assert multiple is not None
        self.assertEqual(
            multiple.overlapping_turn_ids,
            ("test-t01", "test-t02"),
        )
        self.assertEqual(multiple.primary_turn_id, "test-t01")
        self.assertEqual(
            format_corner_label(self.track, multiple),
            "First/Second (T1-T2)",
        )
        self.assertIsNone(associate_region(self.track, 200, 300))
        self.assertIsNone(associate_region(self.track, 150, 150))

    def test_rejects_invalid_region_ranges(self) -> None:
        for start, end in ((-1, 10), (10, 1001), (20, 10)):
            with self.subTest(start=start, end=end):
                with self.assertRaises(ValueError):
                    associate_region(self.track, start, end)

    def test_tie_breaks_by_entry_then_stable_id(self) -> None:
        association = associate_region(self.track, 125, 175)
        assert association is not None
        self.assertEqual(association.primary_turn_id, "test-t01")

        same_entry = TrackDefinition(
            track_id=100,
            display_name="Tie Test",
            track_length_m=500,
            turns=(
                TurnDefinition(
                    turn_id="tie-b",
                    number=1,
                    display_name="B",
                    direction="left",
                    entry_distance_m=100,
                    apex_distance_m=125,
                    exit_distance_m=150,
                ),
                TurnDefinition(
                    turn_id="tie-a",
                    number=2,
                    display_name="A",
                    direction="right",
                    entry_distance_m=100,
                    apex_distance_m=125,
                    exit_distance_m=150,
                ),
            ),
            complexes=(),
        )
        stable = associate_region(same_entry, 110, 140)
        assert stable is not None
        self.assertEqual(stable.primary_turn_id, "tie-a")


class CatalogCliTests(unittest.TestCase):
    def test_catalog_flags_are_mutually_exclusive(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(
                    [
                        "capture.f1cap",
                        "--corner-catalog",
                        "catalog.json",
                        "--no-corner-catalog",
                    ]
                )

    def test_custom_catalog_replaces_the_default_for_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            catalog_path = Path(temporary) / "custom.json"
            catalog_path.write_text(json.dumps(valid_document()), encoding="utf-8")
            custom = load_corner_catalog(catalog_path)
            with (
                patch(
                    "f1_telemetry.lap_analysis.analyze_capture",
                    side_effect=ValueError("stop after selection"),
                ) as analyze,
                self.assertRaises(SystemExit),
            ):
                main(["capture.f1cap", "--corner-catalog", str(catalog_path)])
            selected = analyze.call_args.kwargs["corner_catalog"]
            self.assertEqual(selected.metadata.sha256, custom.metadata.sha256)
            self.assertEqual(selected.tracks, custom.tracks)

    def test_no_catalog_passes_explicit_none(self) -> None:
        with (
            patch(
                "f1_telemetry.lap_analysis.analyze_capture",
                side_effect=ValueError("stop after selection"),
            ) as analyze,
            self.assertRaises(SystemExit),
        ):
            main(["capture.f1cap", "--no-corner-catalog"])
        self.assertIsNone(analyze.call_args.kwargs["corner_catalog"])


if __name__ == "__main__":
    unittest.main()
