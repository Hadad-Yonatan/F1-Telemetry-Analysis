"""Strict F1 25 player-car Motion and Motion Ex packet decoders."""

from __future__ import annotations

import math
from dataclasses import dataclass
from struct import Struct

from f1_telemetry.decode import PacketDecodeError, PacketIdentity, _decode_header, _identity, _validate_packet


MOTION_PACKET_ID = 0
MOTION_EX_PACKET_ID = 13
MOTION_PACKET_SIZE = 1349
MOTION_EX_PACKET_SIZE = 273
WHEEL_ORDER = ("rear_left", "rear_right", "front_left", "front_right")

_HEADER_SIZE = 29
_CAR_MOTION = Struct("<6f6h6f")
_MOTION_EX = Struct("<61f")
_THREE_FLOATS = Struct("<3f")
_THREE_SHORTS = Struct("<3h")
_FOUR_FLOATS = Struct("<4f")
_FLOAT = Struct("<f")

Vector3 = tuple[float, float, float]
WheelValues = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class Motion:
    """Immutable selected-car Motion fields, kept in their compact wire layout."""

    packed: bytes

    @property
    def position_m(self) -> Vector3:
        return _THREE_FLOATS.unpack_from(self.packed, 0)

    @property
    def velocity_mps(self) -> Vector3:
        return _THREE_FLOATS.unpack_from(self.packed, 12)

    @property
    def forward(self) -> Vector3:
        return tuple(value / 32767 for value in _THREE_SHORTS.unpack_from(self.packed, 24))

    @property
    def right(self) -> Vector3:
        return tuple(value / 32767 for value in _THREE_SHORTS.unpack_from(self.packed, 30))

    @property
    def g_force(self) -> Vector3:
        """Lateral, longitudinal, vertical, in G."""
        return _THREE_FLOATS.unpack_from(self.packed, 36)

    @property
    def orientation_rad(self) -> Vector3:
        """Yaw, pitch, roll, in radians."""
        return _THREE_FLOATS.unpack_from(self.packed, 48)


@dataclass(frozen=True, slots=True)
class MotionEx:
    """Immutable player Motion Ex; validate all values, decode channels on demand."""

    packed: bytes

    @property
    def wheel_slip_ratio(self) -> WheelValues:
        return _FOUR_FLOATS.unpack_from(self.packed, 16 * 4)

    @property
    def wheel_slip_angle(self) -> WheelValues:
        return _FOUR_FLOATS.unpack_from(self.packed, 20 * 4)

    @property
    def front_wheels_angle_rad(self) -> float:
        return _FLOAT.unpack_from(self.packed, 42 * 4)[0]


@dataclass(frozen=True, slots=True)
class DecodedMotion:
    identity: PacketIdentity
    motion: Motion


@dataclass(frozen=True, slots=True)
class DecodedMotionEx:
    identity: PacketIdentity
    motion_ex: MotionEx


def _motion_identity(payload: bytes, packet_id: int, size: int) -> PacketIdentity:
    header = _decode_header(payload)
    _validate_packet(payload, header, expected_packet_id=packet_id, expected_size=size)
    identity = _identity(header)
    if not math.isfinite(identity.session_time) or identity.session_time < 0:
        raise PacketDecodeError("Motion session time must be finite and nonnegative")
    return identity


def decode_motion(payload: bytes) -> DecodedMotion:
    """Validate the header and every numeric field for the header's player car."""
    identity = _motion_identity(payload, MOTION_PACKET_ID, MOTION_PACKET_SIZE)
    start = _HEADER_SIZE + identity.player_car_index * _CAR_MOTION.size
    body = bytes(payload[start : start + _CAR_MOTION.size])
    values = _CAR_MOTION.unpack(body)
    if not all(map(math.isfinite, values[:6] + values[12:])):
        raise PacketDecodeError("selected-car Motion values must be finite")
    if -32768 in values[6:12]:
        raise PacketDecodeError("Motion direction components must be in -32767..32767")
    return DecodedMotion(identity, Motion(body))


def decode_motion_ex(payload: bytes) -> DecodedMotionEx:
    """Validate every player Motion Ex value without retaining Python floats per frame."""
    identity = _motion_identity(payload, MOTION_EX_PACKET_ID, MOTION_EX_PACKET_SIZE)
    body = bytes(payload[_HEADER_SIZE:])
    if not all(map(math.isfinite, _MOTION_EX.unpack(body))):
        raise PacketDecodeError("Motion Ex values must be finite")
    return DecodedMotionEx(identity, MotionEx(body))
