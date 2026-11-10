"""Content identity and verification for immutable analysis bundles."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from f1_telemetry.consistency import (
    CONSISTENCY_FILENAME,
    CONSISTENCY_METHOD_VERSION,
    CONSISTENCY_SCHEMA_VERSION,
)

MANIFEST_FILENAME = "bundle.json"
MANIFEST_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ARTIFACT_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]*")


class BundleError(ValueError):
    """An analysis bundle is incomplete, unsupported, or inconsistent."""


@dataclass(frozen=True, slots=True)
class Artifact:
    path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class VerifiedBundle:
    root: Path
    bundle_id: str
    document: dict[str, object]
    artifacts: tuple[Artifact, ...]
    manifested: bool


def _unique_object(items: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in items:
        if key in result:
            raise BundleError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise BundleError(f"invalid JSON constant: {value}")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise BundleError("non-finite JSON number")
    return result


def _load_object(raw: bytes, name: str) -> dict[str, object]:
    try:
        data = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_constant=_reject_constant, parse_float=_finite_float,
        )
    except (UnicodeError, ValueError, RecursionError) as error:
        raise BundleError(f"invalid {name}: {error}") from error
    return object_value(data, name)


def object_value(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BundleError(f"{name} must be a JSON object")
    return value


def array_value(value: object, name: str) -> list:
    if not isinstance(value, list):
        raise BundleError(f"{name} must be a JSON array")
    return value


def integer_value(value: object, name: str, low: int = 0, high: int = (1 << 63) - 1) -> int:
    if type(value) is not int or not low <= value <= high:
        raise BundleError(f"{name} must be an integer in {low}..{high}")
    return value


def digest_value(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise BundleError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _root(path: str | Path) -> Path:
    root = Path(path)
    if root.is_symlink() or not root.is_dir():
        raise BundleError(f"bundle root is not a regular directory: {root}")
    return root.resolve(strict=True)


def _files(root: Path) -> dict[str, Path]:
    result = {}
    for path in sorted(root.iterdir()):
        if not _ARTIFACT_NAME.fullmatch(path.name) or not stat.S_ISREG(path.lstat().st_mode):
            raise BundleError(f"unsafe artifact: {path.name}")
        result[path.name] = path
    return result


def _artifact(path: Path) -> Artifact:
    size = 0
    digest = hashlib.sha256()
    with _open_artifact(path) as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return Artifact(path.name, size, digest.hexdigest())


@contextmanager
def _open_artifact(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise BundleError(f"artifact is not a regular file: {path.name}")
        yield stream


def _read_artifact(path: Path) -> bytes:
    with _open_artifact(path) as stream:
        return stream.read()


def write_bundle_manifest(root: Path) -> str:
    """Write the manifest last, in the caller's unpublished bundle directory."""
    files = _files(_root(root))
    if MANIFEST_FILENAME in files or "analysis.json" not in files:
        raise BundleError("bundle must have analysis.json and no pre-existing manifest")
    document = _load_object(_read_artifact(files["analysis.json"]), "analysis.json")
    required = _required_artifacts(document, manifested=True)
    _verify_file_set(files, required, manifested=True)
    artifacts = {name: _artifact(path) for name, path in files.items()}
    bundle_id = artifacts["analysis.json"].sha256
    with (root / MANIFEST_FILENAME).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(
            {"schema_version": MANIFEST_SCHEMA_VERSION, "bundle_id": bundle_id,
             "artifacts": [asdict(artifact) for artifact in artifacts.values()]},
            stream, sort_keys=True, indent=2, allow_nan=False,
        )
        stream.write("\n")
    return bundle_id


def _manifest_artifacts(raw: bytes, bundle_id: str) -> tuple[Artifact, ...]:
    data = _load_object(raw, MANIFEST_FILENAME)
    if set(data) != {"schema_version", "bundle_id", "artifacts"}:
        raise BundleError("invalid bundle manifest fields")
    integer_value(data["schema_version"], "manifest schema version", 1, 1)
    if data["bundle_id"] != bundle_id:
        raise BundleError("bundle ID does not match exact analysis.json bytes")
    artifacts = []
    for value in array_value(data["artifacts"], "manifest artifacts"):
        entry = object_value(value, "manifest artifact")
        if set(entry) != {"path", "size_bytes", "sha256"}:
            raise BundleError("invalid manifest artifact fields")
        name = entry["path"]
        if not isinstance(name, str) or not _ARTIFACT_NAME.fullmatch(name) or name == MANIFEST_FILENAME:
            raise BundleError(f"unsafe manifest artifact path: {name!r}")
        artifacts.append(Artifact(
            name, integer_value(entry["size_bytes"], "artifact size"),
            digest_value(entry["sha256"], "artifact digest"),
        ))
    paths = [entry.path for entry in artifacts]
    if paths != sorted(set(paths)):
        raise BundleError("manifest paths must be unique and sorted")
    return tuple(artifacts)


def _required_artifacts(document: dict[str, object], manifested: bool) -> dict[str, dict]:
    integer_value(document.get("schema_version"), "analysis schema version", 1, 1)
    ref = integer_value(document.get("selected_reference_lap"), "reference lap", 1, 100)
    laps = {}
    for value in array_value(document.get("lap_eligibility"), "lap eligibility"):
        lap = object_value(value, "lap")
        number = integer_value(lap.get("lap_number"), "lap number", 1, 100)
        if number in laps or type(lap.get("eligible")) is not bool:
            raise BundleError(f"duplicate lap or invalid eligibility: {number}")
        laps[number] = lap
    if ref not in laps or not laps[ref]["eligible"]:
        raise BundleError("reference lap must be eligible")
    required: dict[str, dict] = {"analysis.json": {}}
    if manifested:
        required.update({"report.html": {}, "report.md": {}})
    if "consistency" in document:
        descriptor = object_value(document["consistency"], "consistency descriptor")
        if descriptor.get("path") != CONSISTENCY_FILENAME:
            raise BundleError("invalid consistency artifact path")
        required[CONSISTENCY_FILENAME] = descriptor
    comparisons = array_value(document.get("comparisons"), "comparisons")
    if not comparisons:
        raise BundleError("analysis must include at least one comparison")
    for value in comparisons:
        comparison = object_value(value, "comparison")
        number = integer_value(comparison.get("comparison_lap"), "comparison lap", 1, 100)
        actual_ref = integer_value(comparison.get("reference_lap"), "comparison reference lap", 1, 100)
        if actual_ref != ref or number == ref or number not in laps or not laps[number]["eligible"]:
            raise BundleError("comparison does not reference the selected eligible laps")
        stem = f"lap-{ref:02d}-vs-{number:02d}"
        if stem + ".csv" in required:
            raise BundleError("duplicate comparison")
        required[stem + ".csv"] = comparison
        required[stem + ".svg"] = {}
    return required


def _verify_file_set(files: dict[str, Path], required: dict[str, dict], *, manifested: bool) -> None:
    missing = sorted(required.keys() - files.keys())
    if missing:
        raise BundleError(f"missing artifacts: {', '.join(missing)}")
    optional = set() if manifested else {"report.md", "report.html"}
    extra = sorted(files.keys() - required.keys() - optional)
    if extra:
        raise BundleError(f"unexpected artifacts: {', '.join(extra)}")


def _verify_legacy_csv(path: Path, comparison: dict, track_length: int) -> None:
    from f1_telemetry.analysis_output import CSV_COLUMNS
    from io import TextIOWrapper

    try:
        with _open_artifact(path) as binary, TextIOWrapper(binary, encoding="utf-8", newline="") as stream:
            reader = csv.reader(stream, strict=True)
            if tuple(next(reader, ())) != CSV_COLUMNS:
                raise BundleError(f"invalid comparison CSV header: {path.name}")
            previous_distance = -1.0
            last_delta = None
            for row in reader:
                if len(row) != len(CSV_COLUMNS):
                    raise BundleError(f"invalid comparison CSV row: {path.name}")
                values = [float(value) for value in row]
                distance, _, _, last_delta = values[:4]
                if not all(map(math.isfinite, values)) or distance <= previous_distance:
                    raise BundleError(f"invalid comparison CSV samples: {path.name}")
                if previous_distance < 0 and distance != 0:
                    raise BundleError(f"comparison CSV does not begin at zero: {path.name}")
                previous_distance = distance
            delta = comparison.get("calculated_finish_delta_s")
            if (type(delta) not in (int, float) or last_delta is None
                or previous_distance != track_length
                or not math.isclose(last_delta, delta, rel_tol=0, abs_tol=1e-6)):
                raise BundleError(f"comparison CSV finish does not match analysis: {path.name}")
    except (UnicodeError, csv.Error, ValueError) as error:
        raise BundleError(f"invalid comparison CSV {path.name}: {error}") from error


def verify_bundle(path: str | Path) -> VerifiedBundle:
    """Verify an exact manifest or the structure of a legacy schema-1 bundle."""
    root = _root(path)
    files = _files(root)
    if "analysis.json" not in files:
        raise BundleError("bundle is missing analysis.json")
    raw = _read_artifact(files["analysis.json"])
    bundle_id = hashlib.sha256(raw).hexdigest()
    document = _load_object(raw, "analysis.json")
    manifest = files.pop(MANIFEST_FILENAME, None)
    required = _required_artifacts(document, manifest is not None)
    _verify_file_set(files, required, manifested=manifest is not None)
    if manifest is not None:
        declared = _manifest_artifacts(_read_artifact(manifest), bundle_id)
        if {entry.path for entry in declared} != files.keys():
            raise BundleError("manifest artifact set does not match bundle files")
    else:
        session = object_value(document.get("session"), "analysis session")
        length = integer_value(session.get("track_length_m"), "track length", 1)
        for name, comparison in required.items():
            if name.endswith(".csv"):
                _verify_legacy_csv(files[name], comparison, length)
    artifacts = tuple(_artifact(path) for path in files.values())
    if next(entry.sha256 for entry in artifacts if entry.path == "analysis.json") != bundle_id:
        raise BundleError("analysis.json changed during verification")
    if manifest is not None and artifacts != declared:
        raise BundleError("bundle artifact size or SHA-256 does not match the manifest")
    if CONSISTENCY_FILENAME in required:
        descriptor = required[CONSISTENCY_FILENAME]
        expected = digest_value(descriptor.get("sha256"), "consistency SHA-256")
        actual = next(entry.sha256 for entry in artifacts if entry.path == CONSISTENCY_FILENAME)
        raw_consistency = _read_artifact(files[CONSISTENCY_FILENAME])
        if actual != expected or hashlib.sha256(raw_consistency).hexdigest() != actual:
            raise BundleError("consistency artifact SHA-256 does not match analysis.json")
        consistency = _load_object(raw_consistency, CONSISTENCY_FILENAME)
        for item in (descriptor, consistency):
            integer_value(
                item.get("schema_version"), "consistency schema",
                CONSISTENCY_SCHEMA_VERSION, CONSISTENCY_SCHEMA_VERSION,
            )
            if item.get("method_version") != CONSISTENCY_METHOD_VERSION:
                raise BundleError("unsupported consistency method")
        status = consistency.get("status")
        if status not in ("available", "unavailable") or descriptor.get("status") != status:
            raise BundleError("inconsistent consistency availability")
    return VerifiedBundle(root, bundle_id, document, artifacts, manifest is not None)
