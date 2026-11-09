"""Associate captured context with the retained player lap timelines."""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_right
from dataclasses import asdict, dataclass, fields
from typing import TYPE_CHECKING, Sequence

from f1_telemetry.capture_format import CaptureRecord
from f1_telemetry.context_decode import (
    CAR_SETUPS_PACKET_ID,
    CAR_STATUS_PACKET_ID,
    PARTICIPANTS_PACKET_ID,
    TIME_TRIAL_PACKET_ID,
    TYRE_SETS_PACKET_ID,
    CarSetup,
    DecodedCarSetup,
    DecodedCarStatus,
    DecodedParticipants,
    DecodedSessionContext,
    DecodedTimeTrial,
    DecodedTyreSets,
    Participant,
    SessionAssists,
    SessionRules,
    TimeTrialLap,
    TyreSet,
    decode_car_setup,
    decode_car_status,
    decode_participants,
    decode_session_context,
    decode_time_trial,
    decode_tyre_sets,
)
from f1_telemetry.decode import SESSION_PACKET_ID, PacketDecodeError, _decode_header

if TYPE_CHECKING:
    from f1_telemetry.lap_analysis import LapTrace


@dataclass(frozen=True, slots=True)
class SessionSettings:
    session_type: int
    game_mode: int | None
    formula: int
    weather: int
    track_temperature_c: int
    air_temperature_c: int
    time_of_day_minutes: int
    game_paused: bool
    network_game: bool
    equal_car_performance: bool
    safety_car_status: int
    num_safety_car_periods: int
    num_virtual_safety_car_periods: int
    num_red_flag_periods: int
    assists: SessionAssists
    rules: SessionRules


@dataclass(frozen=True, slots=True)
class SetupSnapshot:
    fingerprint: str
    values: CarSetup


@dataclass(frozen=True, slots=True)
class CarConditions:
    traction_control: int
    anti_lock_brakes: bool
    fuel_mix: int
    front_brake_bias: int
    pit_limiter: bool
    fuel_in_tank_kg: float
    actual_tyre_compound: int | None
    visual_tyre_compound: int | None
    tyres_age_laps: int
    vehicle_fia_flags: int | None
    network_paused: bool


@dataclass(frozen=True, slots=True)
class TyreFitment:
    fitted_index: int
    tyre: TyreSet


@dataclass(frozen=True, slots=True)
class TrialBests:
    player_session_best: TimeTrialLap | None
    personal_best: TimeTrialLap | None
    rival: TimeTrialLap | None


ContextValue = SessionSettings | Participant | SetupSnapshot | CarConditions | TyreFitment | TrialBests
_ContextPacket = (
    DecodedSessionContext | DecodedParticipants | DecodedCarSetup
    | DecodedCarStatus | DecodedTyreSets | DecodedTimeTrial
)

_DECODERS = {
    SESSION_PACKET_ID: ("session", decode_session_context),
    PARTICIPANTS_PACKET_ID: ("participant", decode_participants),
    CAR_SETUPS_PACKET_ID: ("setup", decode_car_setup),
    CAR_STATUS_PACKET_ID: ("car_status", decode_car_status),
    TYRE_SETS_PACKET_ID: ("tyres", decode_tyre_sets),
    TIME_TRIAL_PACKET_ID: ("time_trial", decode_time_trial),
}
_KINDS = tuple(kind for kind, _ in _DECODERS.values())
_REQUIRED_STABILITY_KINDS = ("session", "participant", "setup", "car_status")


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    kind: str
    overall_frame_identifier: int
    frame_identifier: int
    captured_unix_ns: int
    value: ContextValue | None


@dataclass(frozen=True, slots=True)
class ContextDiagnostics:
    kind: str
    accepted: int
    ignored: int
    decode_errors: int
    duplicates: int
    conflicts: int


@dataclass(frozen=True, slots=True)
class LapContext:
    lap_number: int
    start_frame: int
    end_frame: int
    # Indices address the session's shared, ordered timeline.
    start_indices: tuple[int | None, ...]
    change_indices: tuple[int, ...]
    missing_at_start: tuple[str, ...]
    stable_during_lap: bool | None
    unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class SessionContext:
    session_uid: int
    player_car_index: int
    settings: SessionSettings | None
    timeline: tuple[ContextSnapshot, ...]
    laps: tuple[LapContext, ...]
    diagnostics: tuple[ContextDiagnostics, ...]
    schema_version: int = 1

    def at_lap_start(self, lap: LapContext, kind: str) -> ContextValue | None:
        index = lap.start_indices[_KINDS.index(kind)]
        return self.timeline[index].value if index is not None else None


def setup_fingerprint(setup: CarSetup) -> str:
    values = {}
    for field in fields(setup):
        value = getattr(setup, field.name)
        values[field.name] = value.hex() if isinstance(value, float) else value
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(b"f1-25-setup-v1\x00" + canonical).hexdigest()


def _context_value(packet: _ContextPacket) -> ContextValue | None:
    if isinstance(packet, DecodedSessionContext):
        return SessionSettings(**{
            field.name: getattr(packet, field.name) for field in fields(SessionSettings)
        })
    if isinstance(packet, DecodedParticipants):
        return packet.player
    if isinstance(packet, DecodedCarSetup):
        return (
            SetupSnapshot(setup_fingerprint(packet.setup), packet.setup)
            if packet.setup is not None
            else None
        )
    if isinstance(packet, DecodedCarStatus):
        return (
            CarConditions(**{
                field.name: getattr(packet.status, field.name) for field in fields(CarConditions)
            })
            if packet.status is not None
            else None
        )
    if isinstance(packet, DecodedTyreSets):
        if packet.fitted_index is None:
            return None
        fitted = packet.sets[packet.fitted_index]
        return TyreFitment(packet.fitted_index, fitted) if fitted is not None else None
    assert isinstance(packet, DecodedTimeTrial)
    if not any((packet.player_session_best, packet.personal_best, packet.rival)):
        return None
    return TrialBests(packet.player_session_best, packet.personal_best, packet.rival)


def _condition_key(value: ContextValue | None) -> object:
    """Leave routine fuel consumption, tyre wear, and best times out of stability."""
    if isinstance(value, SessionSettings):
        return tuple(
            getattr(value, field.name) for field in fields(SessionSettings)
            if field.name != "time_of_day_minutes"
        )
    if isinstance(value, CarConditions):
        return tuple(
            getattr(value, field.name) for field in fields(CarConditions)
            if field.name not in ("fuel_in_tank_kg", "tyres_age_laps")
        )
    if isinstance(value, TyreFitment):
        return (value.fitted_index, value.tyre.actual_tyre_compound, value.tyre.visual_tyre_compound)
    return value


def _stability(
    start: dict[str, ContextValue | None], changes: Sequence[ContextSnapshot]
) -> bool | None:
    known = {kind: _condition_key(value) for kind, value in start.items() if value is not None}
    incomplete = any(kind not in known for kind in _REQUIRED_STABILITY_KINDS)
    for row in changes:
        if row.kind == "time_trial":
            continue
        if row.value is None:
            incomplete |= row.kind in _REQUIRED_STABILITY_KINDS
            continue
        key = _condition_key(row.value)
        if row.kind in known and key != known[row.kind]:
            return False
        known[row.kind] = key
    return None if incomplete else True


@dataclass(slots=True)
class _PacketCounts:
    accepted: int = 0
    ignored: int = 0
    decode_errors: int = 0
    duplicates: int = 0
    conflicts: int = 0


class ContextCollector:
    """Capture selected-player context during the existing Session/History scan."""

    def __init__(self, session_uid: int, player_car_index: int) -> None:
        self.session_uid = session_uid
        self.player_car_index = player_car_index
        self._frames: dict[str, dict[int, ContextSnapshot]] = {kind: {} for kind in _KINDS}
        self._counts = {kind: _PacketCounts() for kind in _KINDS}

    def accept(self, record: CaptureRecord, packet_id: int) -> None:
        decoder = _DECODERS.get(packet_id)
        if decoder is None:
            return
        kind, decode = decoder
        counts = self._counts[kind]
        try:
            header = _decode_header(record.payload)
            if (header.session_uid, header.player_car_index) != (
                self.session_uid, self.player_car_index
            ):
                counts.ignored += 1
                return
            packet = decode(record.payload)
        except PacketDecodeError:
            counts.decode_errors += 1
            return
        if isinstance(packet, DecodedTyreSets) and packet.car_index != self.player_car_index:
            counts.ignored += 1
            return

        counts.accepted += 1
        identity = packet.identity
        snapshot = ContextSnapshot(
            kind, identity.overall_frame_identifier, identity.frame_identifier,
            record.captured_unix_ns, _context_value(packet),
        )
        frames = self._frames[kind]
        previous = frames.get(snapshot.overall_frame_identifier)
        if previous is not None:
            if (previous.value, previous.frame_identifier) == (
                snapshot.value, snapshot.frame_identifier
            ):
                counts.duplicates += 1
            else:
                counts.conflicts += 1
            # At equal timestamps the later capture record wins.
            if snapshot.captured_unix_ns < previous.captured_unix_ns:
                return
        frames[snapshot.overall_frame_identifier] = snapshot

    def finish(self, laps: Sequence[LapTrace]) -> SessionContext:
        timeline = []
        for frames in self._frames.values():
            previous = None
            for frame in sorted(frames):
                snapshot = frames[frame]
                if previous is None or snapshot.value != previous.value:
                    timeline.append(snapshot)
                previous = snapshot
        timeline.sort(key=lambda row: (row.overall_frame_identifier, row.captured_unix_ns, row.kind))
        frame_numbers = [row.overall_frame_identifier for row in timeline]
        indices_by_kind: dict[str, list[int]] = {kind: [] for kind in _KINDS}
        frames_by_kind: dict[str, list[int]] = {kind: [] for kind in _KINDS}
        for index, row in enumerate(timeline):
            indices_by_kind[row.kind].append(index)
            frames_by_kind[row.kind].append(row.overall_frame_identifier)
        lap_contexts = []
        for lap in laps:
            start = next(
                (sample for sample in lap.samples if sample.current_lap_time_ms > 0),
                lap.samples[0],
            ).overall_frame_identifier
            end = lap.samples[-1].overall_frame_identifier
            if any(
                (sample.session_uid, sample.player_car_index)
                != (self.session_uid, self.player_car_index)
                for sample in lap.samples
            ):
                lap_contexts.append(LapContext(
                    lap.lap_number, start, end, (None,) * len(_KINDS), (), _KINDS, None,
                    "retained samples do not share the selected session and player",
                ))
                continue
            start_indices = []
            for kind in _KINDS:
                position = bisect_right(frames_by_kind[kind], start)
                start_indices.append(indices_by_kind[kind][position - 1] if position else None)
            indices = tuple(start_indices)
            values = {
                kind: timeline[index].value if index is not None else None
                for kind, index in zip(_KINDS, indices)
            }
            missing = tuple(kind for kind in _KINDS if values[kind] is None)
            first_change = bisect_right(frame_numbers, start)
            after_end = bisect_right(frame_numbers, end)
            changes = tuple(range(first_change, after_end))
            stable = _stability(values, timeline[first_change:after_end])
            lap_contexts.append(LapContext(lap.lap_number, start, end, indices, changes, missing, stable))
        settings = next(
            (row.value for row in timeline if isinstance(row.value, SessionSettings)), None
        )
        diagnostics = tuple(
            ContextDiagnostics(kind, **asdict(counts)) for kind, counts in self._counts.items()
        )
        return SessionContext(
            self.session_uid, self.player_car_index, settings, tuple(timeline),
            tuple(lap_contexts), diagnostics,
        )


def context_document(context: SessionContext) -> dict[str, object]:
    """Serialize context without repeating snapshots in each lap."""
    return {
        "schema_version": context.schema_version,
        "session_uid": str(context.session_uid),
        "player_car_index": context.player_car_index,
        "settings": asdict(context.settings) if context.settings is not None else None,
        "timeline": [
            {
                "kind": row.kind,
                "overall_frame_identifier": row.overall_frame_identifier,
                "frame_identifier": row.frame_identifier,
                "captured_unix_ns": str(row.captured_unix_ns),
                "value": asdict(row.value) if row.value is not None else None,
            }
            for row in context.timeline
        ],
        "laps": [
            {
                "lap_number": lap.lap_number,
                "start_frame": lap.start_frame,
                "end_frame": lap.end_frame,
                "start_indices": dict(zip(_KINDS, lap.start_indices)),
                "change_indices": list(lap.change_indices),
                "missing_at_start": list(lap.missing_at_start),
                "stable_during_lap": lap.stable_during_lap,
                "unavailable_reason": lap.unavailable_reason,
            }
            for lap in context.laps
        ],
        "diagnostics": {row.kind: {
            "accepted": row.accepted,
            "ignored": row.ignored,
            "decode_errors": row.decode_errors,
            "duplicates": row.duplicates,
            "conflicts": row.conflicts,
        } for row in context.diagnostics},
    }
