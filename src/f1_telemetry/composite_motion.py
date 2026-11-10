"""Versioned exact-frame joins for optional Motion and Motion Ex telemetry."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from math import isfinite
from pathlib import Path
from typing import Generic, Iterable, TypeVar

from f1_telemetry.capture_format import CaptureRecord, iter_capture
from f1_telemetry.decode import F1_25_PACKET_FORMAT, PacketDecodeError, PacketIdentity
from f1_telemetry.motion_decode import (
    MOTION_EX_PACKET_ID,
    MOTION_PACKET_ID,
    Motion,
    MotionEx,
    decode_motion,
    decode_motion_ex,
)
from f1_telemetry.normalize import FrameNormalizer, NormalizationResult, NormalizedSample
from f1_telemetry.probe import parse_header_prefix


COMPOSITE_NORMALIZATION_SCHEMA_VERSION = 1
MAX_MOTION_INTERPOLATION_GAP_M = 10.0
_ExactKey = tuple[int, int, int, int, float]
_Group = TypeVar("_Group", Motion, MotionEx)


def _exact_key(frame: PacketIdentity | NormalizedSample) -> _ExactKey:
    return (
        frame.session_uid,
        frame.frame_identifier,
        frame.overall_frame_identifier,
        frame.player_car_index,
        frame.session_time,
    )


@dataclass(frozen=True, slots=True)
class CompositeSample:
    core: NormalizedSample
    motion: Motion | None
    motion_ex: MotionEx | None


@dataclass(frozen=True, slots=True)
class MotionStreamCoverage:
    packets: int
    decode_errors: int
    duplicate_packets: int
    conflicting_packets: int
    conflicting_identities: int
    identity_mismatches: int
    orphan_identities: int
    blocked_by_core: int
    joined_samples: int
    missing_samples: int


@dataclass(frozen=True, slots=True)
class CompositeCoverage:
    core_samples: int
    nonexact_core_samples: int
    duplicate_core_identities: int
    joined_both: int
    motion: MotionStreamCoverage
    motion_ex: MotionStreamCoverage


@dataclass(frozen=True, slots=True)
class CompositeNormalizationResult:
    core: NormalizationResult
    samples: tuple[CompositeSample, ...]
    coverage: CompositeCoverage
    schema_version: int = COMPOSITE_NORMALIZATION_SCHEMA_VERSION


class _Stream(Generic[_Group]):
    def __init__(self) -> None:
        self.groups: dict[_ExactKey, _Group] = {}
        self.conflicts: dict[_ExactKey, set[_Group]] = {}
        self.packets = 0
        self.decode_errors = 0
        self.duplicates = 0
        self.conflicting_packets = 0
        self.blocked = 0
        self.joined = 0

    def put(self, identity: PacketIdentity, group: _Group) -> None:
        key = _exact_key(identity)
        previous = self.groups.setdefault(key, group)
        if previous is group:
            return
        if previous == group:
            self.duplicates += 1
            return
        variants = self.conflicts.setdefault(key, set())
        if group in variants:
            self.duplicates += 1
            return
        variants.add(group)
        self.conflicting_packets += 1

    def take(self, key: _ExactKey, blocked: bool) -> _Group | None:
        value = self.groups.pop(key, None)
        if value is None:
            return None
        if blocked:
            self.blocked += 1
            return None
        if key in self.conflicts:
            return None
        self.joined += 1
        return value

    def coverage(self, core: Counter[_ExactKey], core_samples: int) -> MotionStreamCoverage:
        # Only build partial keys for unmatched optional frames; the successful
        # fast path needs no additional per-frame identity index.
        unmatched_keys = {key[:4] for key in self.groups}
        present = {key[:4] for key in core if key[:4] in unmatched_keys} if unmatched_keys else set()
        mismatches = sum(key[:4] in present for key in self.groups)
        return MotionStreamCoverage(
            self.packets, self.decode_errors, self.duplicates, self.conflicting_packets,
            len(self.conflicts), mismatches, len(self.groups) - mismatches, self.blocked,
            self.joined, core_samples - self.joined,
        )


class CompositeFrameNormalizer:
    """Collect v1 core and optional motion in one scan, without widening v1 rows."""

    def __init__(self) -> None:
        self._core = FrameNormalizer()
        self._core_samples: list[NormalizedSample] = []
        self._nonexact: set[int] = set()
        self._motion = _Stream[Motion]()
        self._motion_ex = _Stream[MotionEx]()
        self._finished = False

    def accept(self, record: CaptureRecord) -> None:
        if self._finished:
            raise RuntimeError("cannot accept packets after composite normalization finishes")
        sample = self._core.accept(record)
        if sample is not None:
            if not self._core.last_join_exact or not isfinite(sample.session_time) or sample.session_time < 0:
                self._nonexact.add(id(sample))
            self._core_samples.append(sample)
            return

        header = parse_header_prefix(record.payload)
        if header is None or header.packet_format != F1_25_PACKET_FORMAT:
            return
        if header.packet_id == MOTION_PACKET_ID:
            self._motion.packets += 1
            try:
                decoded = decode_motion(record.payload)
            except PacketDecodeError:
                self._motion.decode_errors += 1
            else:
                self._motion.put(decoded.identity, decoded.motion)
        elif header.packet_id == MOTION_EX_PACKET_ID:
            self._motion_ex.packets += 1
            try:
                decoded_ex = decode_motion_ex(record.payload)
            except PacketDecodeError:
                self._motion_ex.decode_errors += 1
            else:
                self._motion_ex.put(decoded_ex.identity, decoded_ex.motion_ex)

    def finish(self) -> CompositeNormalizationResult:
        if self._finished:
            raise RuntimeError("composite normalization already finished")
        self._finished = True
        self._core.finish()
        self._core_samples.sort(key=lambda sample: (sample.captured_unix_ns, sample.session_uid))
        core = tuple(self._core_samples)
        self._core_samples.clear()
        identities = Counter(map(_exact_key, core))
        samples = []
        joined_both = 0
        for sample in core:
            key = _exact_key(sample)
            blocked = id(sample) in self._nonexact or identities[key] > 1
            motion = self._motion.take(key, blocked)
            motion_ex = self._motion_ex.take(key, blocked)
            joined_both += motion is not None and motion_ex is not None
            samples.append(CompositeSample(sample, motion, motion_ex))
        coverage = CompositeCoverage(
            len(core), len(self._nonexact), sum(count > 1 for count in identities.values()), joined_both,
            self._motion.coverage(identities, len(core)),
            self._motion_ex.coverage(identities, len(core)),
        )
        self._motion.groups.clear()
        self._motion_ex.groups.clear()
        return CompositeNormalizationResult(NormalizationResult(core, replace(self._core.stats)), tuple(samples), coverage)


def normalize_composite_capture(
    path: str | Path, *, allow_truncated_tail: bool = False,
) -> CompositeNormalizationResult:
    """Decode both optional channels in the same capture pass as the unchanged core."""
    normalizer = CompositeFrameNormalizer()
    for record in iter_capture(path, allow_truncated_tail=allow_truncated_tail):
        normalizer.accept(record)
    return normalizer.finish()


def resample_motion_channel(
    observations: Iterable[tuple[float, tuple[float, ...]]],
    distances: Iterable[float],
) -> tuple[tuple[float, ...] | None, ...]:
    """Interpolate ascending observations at nondecreasing targets, with no extrapolation."""
    points = iter(observations)
    left = None
    right = next(points, None)
    if right is not None and not isfinite(right[0]):
        raise ValueError("motion observation distances must be finite and strictly increasing")
    previous = float("-inf")
    results = []
    for distance in distances:
        if not isfinite(distance) or distance < previous:
            raise ValueError("motion target distances must be finite and nondecreasing")
        previous = distance
        while right is not None and right[0] < distance:
            left, right = right, next(points, None)
            if right is not None and (not isfinite(right[0]) or right[0] <= left[0]):
                raise ValueError("motion observation distances must be finite and strictly increasing")
        if right is None:
            results.append(None)
        elif right[0] == distance:
            results.append(right[1])
        elif left is None or right[0] - left[0] > MAX_MOTION_INTERPOLATION_GAP_M:
            results.append(None)
        else:
            ratio = (distance - left[0]) / (right[0] - left[0])
            results.append(tuple(a + ratio * (b - a) for a, b in zip(left[1], right[1], strict=True)))
    return tuple(results)
