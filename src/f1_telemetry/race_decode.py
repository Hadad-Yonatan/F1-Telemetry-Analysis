"""Strict F1 25 Event, Car Damage, and race pit-evidence decoding."""

from __future__ import annotations

import math
from dataclasses import dataclass

from f1.packets import PacketCarDamageData, PacketEventData, PacketLapData

from f1_telemetry.decode import (
    LAP_DATA_PACKET_ID, PacketDecodeError, PacketIdentity, _decode_header, _identity, _validate_packet,
)
from f1_telemetry.motion_decode import WHEEL_ORDER

EVENT_PACKET_ID = 3
CAR_DAMAGE_PACKET_ID = 10
_EVENT_CODES = frozenset((
    "SSTA", "SEND", "FTLP", "RTMT", "DRSE", "DRSD", "TMPT", "CHQF", "RCWN", "PENA",
    "SPTP", "STLG", "LGOT", "DTSV", "SGSV", "FLBK", "BUTN", "RDFL", "OVTK", "SCAR", "COLL",
))
_DAMAGE = (
    "front_left_wing_damage", "front_right_wing_damage", "rear_wing_damage", "floor_damage",
    "diffuser_damage", "sidepod_damage", "gear_box_damage", "engine_damage",
)
_WEAR = ("engine_mguh_wear", "engine_es_wear", "engine_ce_wear", "engine_ice_wear", "engine_mguk_wear", "engine_tc_wear")
_FAULTS = ("drs_fault", "ers_fault", "engine_blown", "engine_seized")


@dataclass(frozen=True, slots=True)
class RaceEvent:
    identity: PacketIdentity
    code: str
    safety_car_type: int | None = None
    safety_car_event: int | None = None


@dataclass(frozen=True, slots=True)
class DamageState:
    fields: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class RaceDamage:
    identity: PacketIdentity
    damage: DamageState


@dataclass(frozen=True, slots=True)
class RacePitEvidence:
    identity: PacketIdentity
    lap_number: int
    pit_status: int
    pit_stops: int
    lap_distance: float
    lap_time_ms: int


def _checked_header(payload: bytes, packet_id: int, size: int) -> PacketIdentity:
    header = _decode_header(payload)
    _validate_packet(payload, header, expected_packet_id=packet_id, expected_size=size)
    if not math.isfinite(header.session_time) or header.session_time < 0:
        raise PacketDecodeError("race packet session time must be finite and nonnegative")
    return _identity(header)


def decode_race_event(payload: bytes) -> RaceEvent:
    identity = _checked_header(payload, EVENT_PACKET_ID, 45)
    packet = PacketEventData.unpack(payload)
    try:
        code = bytes(packet.event_string_code).decode("ascii")
    except UnicodeError as error:
        raise PacketDecodeError("invalid race event code") from error
    if code not in _EVENT_CODES:
        raise PacketDecodeError(f"unsupported race event code: {code!r}")
    if code == "SCAR":
        info = packet.event_details.safety_car
        if info.safety_car_type not in (0, 1, 2, 3) or info.event_type not in (0, 1, 2, 3):
            raise PacketDecodeError("invalid safety-car type or transition")
        return RaceEvent(identity, code, info.safety_car_type, info.event_type)
    return RaceEvent(identity, code)


def decode_race_damage(payload: bytes) -> RaceDamage:
    identity = _checked_header(payload, CAR_DAMAGE_PACKET_ID, 1041)
    car = PacketCarDamageData.unpack(payload).car_damage_data[identity.player_car_index]
    if any(not math.isfinite(value) or not 0 <= value <= 100 for value in car.tyres_wear):
        raise PacketDecodeError("invalid tyre wear in Car Damage")
    numeric = [(name, getattr(car, name)) for name in _DAMAGE]
    for name in ("tyres_damage", "brakes_damage", "tyre_blisters"):
        numeric.extend((f"{name}.{wheel}", value) for wheel, value in zip(WHEEL_ORDER, getattr(car, name)))
    if any(not 0 <= value <= 100 for _, value in numeric) or any(
        not 0 <= getattr(car, name) <= 100 for name in _WEAR
    ):
        raise PacketDecodeError("invalid damage percentage")
    faults = [(name, getattr(car, name)) for name in _FAULTS]
    if any(value not in (0, 1) for _, value in faults):
        raise PacketDecodeError("invalid damage fault flag")
    return RaceDamage(identity, DamageState(tuple(sorted((*numeric, *faults)))))


def decode_race_pit_evidence(payload: bytes) -> RacePitEvidence:
    identity = _checked_header(payload, LAP_DATA_PACKET_ID, PacketLapData.size())
    lap = PacketLapData.unpack(payload).lap_data[identity.player_car_index]
    if lap.pit_status not in (0, 1, 2):
        raise PacketDecodeError("invalid race pit status")
    if not math.isfinite(lap.lap_distance):
        raise PacketDecodeError("invalid race lap distance")
    return RacePitEvidence(
        identity, lap.current_lap_num, lap.pit_status, lap.num_pit_stops,
        lap.lap_distance, lap.current_lap_time_in_ms,
    )
