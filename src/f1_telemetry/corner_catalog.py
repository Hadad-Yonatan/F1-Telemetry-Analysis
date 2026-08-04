"""Strict versioned corner catalogs and deterministic region association."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal, Mapping, Sequence

CATALOG_SCHEMA_VERSION = 1
CATALOG_PACKET_FORMAT = 2025
CATALOG_GAME_YEAR = 25
BUNDLED_CATALOG_NAME = "corner-catalog-v1.json"

_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class CatalogMetadata:
    schema_version: int
    catalog_id: str
    packet_format: int
    game_year: int
    sha256: str


@dataclass(frozen=True)
class TurnDefinition:
    turn_id: str
    number: int
    display_name: str
    direction: Literal["left", "right"]
    entry_distance_m: float
    apex_distance_m: float
    exit_distance_m: float


@dataclass(frozen=True)
class CornerComplex:
    complex_id: str
    display_name: str
    turn_ids: tuple[str, ...]


@dataclass(frozen=True)
class TrackDefinition:
    track_id: int
    display_name: str
    track_length_m: int
    turns: tuple[TurnDefinition, ...]
    complexes: tuple[CornerComplex, ...]


@dataclass(frozen=True)
class CornerCatalog:
    metadata: CatalogMetadata
    tracks: tuple[TrackDefinition, ...]


@dataclass(frozen=True)
class CornerAssociation:
    primary_turn_id: str
    overlapping_turn_ids: tuple[str, ...]
    complex_ids: tuple[str, ...]


@dataclass(frozen=True)
class CatalogSelection:
    status: Literal["matched", "track_not_found", "disabled"]
    metadata: CatalogMetadata | None
    track: TrackDefinition | None

    def __post_init__(self) -> None:
        if self.status == "matched":
            if self.metadata is None or self.track is None:
                raise ValueError("matched catalog selection requires metadata and track")
        elif self.status == "track_not_found":
            if self.metadata is None or self.track is not None:
                raise ValueError(
                    "track-not-found catalog selection requires metadata only"
                )
        elif self.status == "disabled":
            if self.metadata is not None or self.track is not None:
                raise ValueError("disabled catalog selection cannot contain catalog data")
        else:
            raise ValueError(f"invalid catalog selection status: {self.status}")


def disabled_catalog_selection() -> CatalogSelection:
    """Return the explicit catalog-disabled selection state."""
    return CatalogSelection(status="disabled", metadata=None, track=None)


def _object_without_duplicate_keys(
    pairs: Sequence[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _object(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    return value


def _array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{context} must be an array")
    return value


def _fields(
    value: Mapping[str, object],
    *,
    required: set[str],
    optional: set[str] = frozenset(),
    context: str,
) -> None:
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise ValueError(f"{context} missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{context} unknown fields: {', '.join(sorted(unknown))}")


def _integer(value: object, context: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{context} must be an integer")
    if value < minimum:
        raise ValueError(f"{context} must be at least {minimum}")
    return value


def _number(value: object, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{context} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{context} must be finite")
    return result


def _name(value: object, context: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{context} must be a non-empty trimmed string")
    return value


def _identifier(value: object, context: str) -> str:
    result = _name(value, context)
    if _ID_PATTERN.fullmatch(result) is None:
        raise ValueError(f"{context} must be a lowercase ASCII identifier")
    return result


def _parse_turn(value: object, track_length: int, index: int) -> TurnDefinition:
    context = f"turn {index}"
    item = _object(value, context)
    _fields(
        item,
        required={
            "turn_id",
            "number",
            "display_name",
            "direction",
            "entry_distance_m",
            "apex_distance_m",
            "exit_distance_m",
        },
        context=context,
    )
    direction = item["direction"]
    if direction not in ("left", "right"):
        raise ValueError(f"{context} direction must be left or right")
    entry = _number(item["entry_distance_m"], f"{context} entry distance")
    apex = _number(item["apex_distance_m"], f"{context} apex distance")
    exit_distance = _number(item["exit_distance_m"], f"{context} exit distance")
    if entry < 0 or exit_distance > track_length:
        raise ValueError(f"{context} distances must be within the track length")
    if entry > apex:
        raise ValueError(f"{context} entry distance cannot exceed its apex")
    if apex > exit_distance:
        raise ValueError(f"{context} apex distance cannot exceed its exit")
    if entry == exit_distance:
        raise ValueError(f"{context} must have positive length")
    return TurnDefinition(
        turn_id=_identifier(item["turn_id"], f"{context} ID"),
        number=_integer(item["number"], f"{context} number", minimum=1),
        display_name=_name(item["display_name"], f"{context} display name"),
        direction=direction,
        entry_distance_m=entry,
        apex_distance_m=apex,
        exit_distance_m=exit_distance,
    )


def _parse_complex(value: object, index: int) -> CornerComplex:
    context = f"complex {index}"
    item = _object(value, context)
    _fields(
        item,
        required={"complex_id", "display_name", "turn_ids"},
        context=context,
    )
    raw_turn_ids = _array(item["turn_ids"], f"{context} turn IDs")
    if len(raw_turn_ids) < 2:
        raise ValueError(f"{context} must contain at least two turns")
    turn_ids = tuple(
        _identifier(turn_id, f"{context} member {member_index}")
        for member_index, turn_id in enumerate(raw_turn_ids, start=1)
    )
    if len(set(turn_ids)) != len(turn_ids):
        raise ValueError(f"{context} contains duplicate turn IDs")
    return CornerComplex(
        complex_id=_identifier(item["complex_id"], f"{context} ID"),
        display_name=_name(item["display_name"], f"{context} display name"),
        turn_ids=turn_ids,
    )


def _parse_track(value: object, index: int) -> TrackDefinition:
    context = f"track {index}"
    item = _object(value, context)
    _fields(
        item,
        required={"track_id", "display_name", "track_length_m", "turns"},
        optional={"complexes"},
        context=context,
    )
    track_id = _integer(item["track_id"], f"{context} ID")
    track_length = _integer(
        item["track_length_m"], f"{context} length", minimum=1
    )
    raw_turns = _array(item["turns"], f"{context} turns")
    if not raw_turns:
        raise ValueError(f"{context} must contain at least one turn")
    turns = tuple(
        _parse_turn(turn, track_length, turn_index)
        for turn_index, turn in enumerate(raw_turns, start=1)
    )
    turn_ids = [turn.turn_id for turn in turns]
    turn_numbers = [turn.number for turn in turns]
    if len(set(turn_ids)) != len(turn_ids):
        raise ValueError(f"{context} contains duplicate turn IDs")
    if len(set(turn_numbers)) != len(turn_numbers):
        raise ValueError(f"{context} contains duplicate turn numbers")
    if turn_numbers != sorted(turn_numbers):
        raise ValueError(f"{context} turns must be ordered by number")
    for previous, current in zip(turns, turns[1:]):
        if current.entry_distance_m < previous.entry_distance_m:
            raise ValueError(f"{context} turns must be ordered by distance")
        if current.entry_distance_m < previous.exit_distance_m:
            raise ValueError(f"{context} turn windows cannot overlap")

    raw_complexes = _array(item.get("complexes", []), f"{context} complexes")
    complexes = tuple(
        _parse_complex(complex_value, complex_index)
        for complex_index, complex_value in enumerate(raw_complexes, start=1)
    )
    complex_ids = [complex_value.complex_id for complex_value in complexes]
    if len(set(complex_ids)) != len(complex_ids):
        raise ValueError(f"{context} contains duplicate complex IDs")
    positions = {turn_id: turn_index for turn_index, turn_id in enumerate(turn_ids)}
    assigned_turns: set[str] = set()
    for complex_value in complexes:
        try:
            member_positions = [positions[turn_id] for turn_id in complex_value.turn_ids]
        except KeyError as error:
            raise ValueError(
                f"{context} complex {complex_value.complex_id} references "
                f"missing turn {error.args[0]}"
            ) from error
        if member_positions != list(
            range(member_positions[0], member_positions[0] + len(member_positions))
        ):
            raise ValueError(
                f"{context} complex {complex_value.complex_id} members must be "
                "ordered and consecutive"
            )
        multiply_assigned = assigned_turns.intersection(complex_value.turn_ids)
        if multiply_assigned:
            raise ValueError(
                f"{context} complex turns cannot be assigned more than once: "
                f"{', '.join(sorted(multiply_assigned))}"
            )
        assigned_turns.update(complex_value.turn_ids)
    return TrackDefinition(
        track_id=track_id,
        display_name=_name(item["display_name"], f"{context} display name"),
        track_length_m=track_length,
        turns=turns,
        complexes=complexes,
    )


def _catalog_from_bytes(data: bytes, source: str) -> CornerCatalog:
    try:
        raw = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid corner catalog {source}: {error}") from error
    root = _object(raw, "catalog root")
    _fields(
        root,
        required={
            "schema_version",
            "catalog_id",
            "packet_format",
            "game_year",
            "tracks",
        },
        context="catalog root",
    )
    schema_version = _integer(root["schema_version"], "catalog schema version")
    packet_format = _integer(root["packet_format"], "catalog packet format")
    game_year = _integer(root["game_year"], "catalog game year")
    if schema_version != CATALOG_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported corner catalog schema version: {schema_version}"
        )
    if packet_format != CATALOG_PACKET_FORMAT:
        raise ValueError(f"unsupported catalog packet format: {packet_format}")
    if game_year != CATALOG_GAME_YEAR:
        raise ValueError(f"unsupported catalog game year: {game_year}")
    raw_tracks = _array(root["tracks"], "catalog tracks")
    if not raw_tracks:
        raise ValueError("catalog must contain at least one track")
    tracks = tuple(
        _parse_track(track, track_index)
        for track_index, track in enumerate(raw_tracks, start=1)
    )
    track_ids = [track.track_id for track in tracks]
    if len(set(track_ids)) != len(track_ids):
        raise ValueError("catalog contains duplicate track IDs")
    if track_ids != sorted(track_ids):
        raise ValueError("catalog tracks must be ordered by track ID")
    all_turn_ids = [turn.turn_id for track in tracks for turn in track.turns]
    if len(set(all_turn_ids)) != len(all_turn_ids):
        raise ValueError("catalog contains duplicate turn IDs")
    all_complex_ids = [
        complex_value.complex_id
        for track in tracks
        for complex_value in track.complexes
    ]
    if len(set(all_complex_ids)) != len(all_complex_ids):
        raise ValueError("catalog contains duplicate complex IDs")
    return CornerCatalog(
        metadata=CatalogMetadata(
            schema_version=schema_version,
            catalog_id=_identifier(root["catalog_id"], "catalog ID"),
            packet_format=packet_format,
            game_year=game_year,
            sha256=hashlib.sha256(data).hexdigest(),
        ),
        tracks=tracks,
    )


def load_corner_catalog(path: str | Path) -> CornerCatalog:
    """Load and validate a catalog, hashing its exact source bytes."""
    catalog_path = Path(path)
    return _catalog_from_bytes(catalog_path.read_bytes(), str(catalog_path))


def load_bundled_corner_catalog() -> CornerCatalog:
    """Load the catalog shipped as package data."""
    resource = resources.files("f1_telemetry").joinpath(
        "data", BUNDLED_CATALOG_NAME
    )
    return _catalog_from_bytes(resource.read_bytes(), f"bundled {BUNDLED_CATALOG_NAME}")


def select_catalog_track(
    catalog: CornerCatalog,
    track_id: int,
    track_length: int,
) -> CatalogSelection:
    """Select an exact track and reject a known ID with a different length."""
    track = next((item for item in catalog.tracks if item.track_id == track_id), None)
    if track is None:
        return CatalogSelection(
            status="track_not_found",
            metadata=catalog.metadata,
            track=None,
        )
    if track.track_length_m != track_length:
        raise ValueError(
            f"corner catalog track {track_id} length {track.track_length_m} m "
            f"does not match session length {track_length} m"
        )
    return CatalogSelection(
        status="matched",
        metadata=catalog.metadata,
        track=track,
    )


def associate_region(
    track: TrackDefinition,
    start_distance: float,
    end_distance: float,
) -> CornerAssociation | None:
    """Associate a non-wrapping distance region with every positive overlap."""
    if not math.isfinite(start_distance) or not math.isfinite(end_distance):
        raise ValueError("region distances must be finite")
    if start_distance < 0 or end_distance > track.track_length_m:
        raise ValueError("region distances must be within the track length")
    if start_distance > end_distance:
        raise ValueError("region start cannot exceed region end")

    overlaps: list[tuple[TurnDefinition, float]] = []
    for turn in track.turns:
        overlap = min(end_distance, turn.exit_distance_m) - max(
            start_distance, turn.entry_distance_m
        )
        if overlap > 0:
            overlaps.append((turn, overlap))
    if not overlaps:
        return None
    primary = min(
        overlaps,
        key=lambda item: (-item[1], item[0].entry_distance_m, item[0].turn_id),
    )[0]
    overlapping_ids = tuple(turn.turn_id for turn, _ in overlaps)
    overlapping_set = set(overlapping_ids)
    complex_ids = tuple(
        complex_value.complex_id
        for complex_value in track.complexes
        if overlapping_set.intersection(complex_value.turn_ids)
    )
    return CornerAssociation(
        primary_turn_id=primary.turn_id,
        overlapping_turn_ids=overlapping_ids,
        complex_ids=complex_ids,
    )


def format_corner_label(
    track: TrackDefinition,
    association: CornerAssociation,
) -> str:
    """Return the deterministic driver-facing label for an association."""
    turns_by_id = {turn.turn_id: turn for turn in track.turns}
    overlapping = set(association.overlapping_turn_ids)
    complexes_by_id = {
        complex_value.complex_id: complex_value for complex_value in track.complexes
    }
    for complex_id in association.complex_ids:
        complex_value = complexes_by_id[complex_id]
        members = [
            turns_by_id[turn_id]
            for turn_id in complex_value.turn_ids
            if turn_id in overlapping
        ]
        if len(members) >= 2:
            return (
                f"{complex_value.display_name} "
                f"(T{members[0].number}-T{members[-1].number})"
            )
    primary = turns_by_id[association.primary_turn_id]
    return f"T{primary.number} {primary.display_name}"


DEFAULT_CORNER_CATALOG = load_bundled_corner_catalog()
