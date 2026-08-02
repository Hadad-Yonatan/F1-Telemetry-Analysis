"""Strict decoding for the F1 25 packets needed by the first analyzer."""

from __future__ import annotations

from dataclasses import dataclass

from f1.packets import (
    PacketCarTelemetryData,
    PacketHeader,
    PacketLapData,
    PacketSessionData,
    PacketSessionHistoryData,
)

F1_25_PACKET_FORMAT = 2025
F1_25_GAME_YEAR = 25
PACKET_VERSION = 1
LAP_DATA_PACKET_ID = 2
CAR_TELEMETRY_PACKET_ID = 6
SESSION_PACKET_ID = 1
SESSION_HISTORY_PACKET_ID = 11
MAX_CARS = 22

FrameKey = tuple[int, int, int, int]


class PacketDecodeError(ValueError):
    """Raised when a payload does not match the supported F1 25 contract."""


@dataclass(frozen=True)
class PacketIdentity:
    session_uid: int
    session_time: float
    frame_identifier: int
    overall_frame_identifier: int
    player_car_index: int

    @property
    def frame_key(self) -> FrameKey:
        """Stable join key, including the counter unaffected by flashbacks."""
        return (
            self.session_uid,
            self.frame_identifier,
            self.overall_frame_identifier,
            self.player_car_index,
        )


@dataclass(frozen=True)
class DecodedLapData:
    identity: PacketIdentity
    lap_number: int
    lap_distance: float
    total_distance: float
    current_lap_time_ms: int
    last_lap_time_ms: int
    sector: int
    lap_invalid: bool
    pit_status: int
    driver_status: int
    result_status: int


@dataclass(frozen=True)
class DecodedCarTelemetry:
    identity: PacketIdentity
    speed_kph: int
    throttle: float
    brake: float
    steering: float
    clutch: int
    gear: int
    engine_rpm: int
    drs: bool


@dataclass(frozen=True)
class DecodedSessionData:
    identity: PacketIdentity
    track_length: int
    track_id: int
    session_type: int
    sector2_distance: float
    sector3_distance: float


@dataclass(frozen=True)
class DecodedLapHistory:
    lap_number: int
    lap_time_ms: int
    sector1_time_ms: int
    sector2_time_ms: int
    sector3_time_ms: int
    validity_flags: int

    @property
    def officially_valid(self) -> bool:
        return self.validity_flags & 0x0F == 0x0F

    @property
    def structurally_complete(self) -> bool:
        sectors = (
            self.sector1_time_ms,
            self.sector2_time_ms,
            self.sector3_time_ms,
        )
        return (
            self.lap_time_ms > 0
            and all(value > 0 for value in sectors)
            and abs(sum(sectors) - self.lap_time_ms) <= 5
        )


@dataclass(frozen=True)
class DecodedSessionHistory:
    identity: PacketIdentity
    car_index: int
    laps: tuple[DecodedLapHistory, ...]


def _decode_header(payload: bytes) -> PacketHeader:
    if len(payload) < PacketHeader.size():
        raise PacketDecodeError(
            f"packet header is truncated: expected {PacketHeader.size()} bytes, "
            f"found {len(payload)}"
        )
    return PacketHeader.unpack(payload)


def _validate_packet(
    payload: bytes,
    header: PacketHeader,
    *,
    expected_packet_id: int,
    expected_size: int,
) -> None:
    if header.packet_format != F1_25_PACKET_FORMAT:
        raise PacketDecodeError(
            f"unsupported packet format {header.packet_format}; expected 2025"
        )
    if header.game_year != F1_25_GAME_YEAR:
        raise PacketDecodeError(
            f"unsupported game year {header.game_year}; expected 25"
        )
    if header.packet_version != PACKET_VERSION:
        raise PacketDecodeError(
            f"unsupported packet version {header.packet_version}; expected 1"
        )
    if header.packet_id != expected_packet_id:
        raise PacketDecodeError(
            f"unexpected packet ID {header.packet_id}; expected {expected_packet_id}"
        )
    if len(payload) != expected_size:
        raise PacketDecodeError(
            f"packet {expected_packet_id} has {len(payload)} bytes; "
            f"expected {expected_size}"
        )
    if header.player_car_index >= MAX_CARS:
        raise PacketDecodeError(
            f"player car index {header.player_car_index} is outside 0..21"
        )


def _identity(header: PacketHeader) -> PacketIdentity:
    return PacketIdentity(
        session_uid=int(header.session_uid),
        session_time=float(header.session_time),
        frame_identifier=int(header.frame_identifier),
        overall_frame_identifier=int(header.overall_frame_identifier),
        player_car_index=int(header.player_car_index),
    )


def _require_range(name: str, value: float, lower: float, upper: float) -> None:
    tolerance = 1e-6
    if value < lower - tolerance or value > upper + tolerance:
        raise PacketDecodeError(
            f"{name} value {value} is outside the specified range "
            f"{lower}..{upper}"
        )


def decode_lap_data(payload: bytes) -> DecodedLapData:
    """Decode the player-car entry from one F1 25 Lap Data packet."""
    header = _decode_header(payload)
    _validate_packet(
        payload,
        header,
        expected_packet_id=LAP_DATA_PACKET_ID,
        expected_size=PacketLapData.size(),
    )
    packet = PacketLapData.unpack(payload)
    lap = packet.lap_data[header.player_car_index]
    if lap.current_lap_invalid not in (0, 1):
        raise PacketDecodeError(
            f"current lap invalid flag is {lap.current_lap_invalid}; expected 0 or 1"
        )

    return DecodedLapData(
        identity=_identity(packet.header),
        lap_number=int(lap.current_lap_num),
        lap_distance=float(lap.lap_distance),
        total_distance=float(lap.total_distance),
        current_lap_time_ms=int(lap.current_lap_time_in_ms),
        last_lap_time_ms=int(lap.last_lap_time_in_ms),
        sector=int(lap.sector),
        lap_invalid=bool(lap.current_lap_invalid),
        pit_status=int(lap.pit_status),
        driver_status=int(lap.driver_status),
        result_status=int(lap.result_status),
    )


def decode_car_telemetry(payload: bytes) -> DecodedCarTelemetry:
    """Decode the player-car entry from one F1 25 Car Telemetry packet."""
    header = _decode_header(payload)
    _validate_packet(
        payload,
        header,
        expected_packet_id=CAR_TELEMETRY_PACKET_ID,
        expected_size=PacketCarTelemetryData.size(),
    )
    packet = PacketCarTelemetryData.unpack(payload)
    telemetry = packet.car_telemetry_data[header.player_car_index]
    _require_range("throttle", telemetry.throttle, 0.0, 1.0)
    _require_range("brake", telemetry.brake, 0.0, 1.0)
    _require_range("steering", telemetry.steer, -1.0, 1.0)
    _require_range("clutch", telemetry.clutch, 0, 100)
    _require_range("gear", telemetry.gear, -1, 8)
    if telemetry.drs not in (0, 1):
        raise PacketDecodeError(f"DRS value is {telemetry.drs}; expected 0 or 1")

    return DecodedCarTelemetry(
        identity=_identity(packet.header),
        speed_kph=int(telemetry.speed),
        throttle=float(telemetry.throttle),
        brake=float(telemetry.brake),
        steering=float(telemetry.steer),
        clutch=int(telemetry.clutch),
        gear=int(telemetry.gear),
        engine_rpm=int(telemetry.engine_rpm),
        drs=bool(telemetry.drs),
    )


def decode_session_data(payload: bytes) -> DecodedSessionData:
    """Decode track length and sector boundaries from Session Data."""
    header = _decode_header(payload)
    _validate_packet(
        payload,
        header,
        expected_packet_id=SESSION_PACKET_ID,
        expected_size=PacketSessionData.size(),
    )
    packet = PacketSessionData.unpack(payload)
    return DecodedSessionData(
        identity=_identity(packet.header),
        track_length=int(packet.track_length),
        track_id=int(packet.track_id),
        session_type=int(packet.session_type),
        sector2_distance=float(packet.sector2_lap_distance_start),
        sector3_distance=float(packet.sector3_lap_distance_start),
    )


def _combined_time_ms(minutes: int, milliseconds: int) -> int:
    return int(minutes) * 60_000 + int(milliseconds)


def decode_session_history(payload: bytes) -> DecodedSessionHistory:
    """Decode completed-lap timing and official validity flags for one car."""
    header = _decode_header(payload)
    _validate_packet(
        payload,
        header,
        expected_packet_id=SESSION_HISTORY_PACKET_ID,
        expected_size=PacketSessionHistoryData.size(),
    )
    packet = PacketSessionHistoryData.unpack(payload)
    if packet.num_laps > 100:
        raise PacketDecodeError(
            f"session history has invalid lap count {packet.num_laps}"
        )

    laps = []
    for index in range(packet.num_laps):
        lap = packet.lap_history_data[index]
        laps.append(
            DecodedLapHistory(
                lap_number=index + 1,
                lap_time_ms=int(lap.lap_time_in_ms),
                sector1_time_ms=_combined_time_ms(
                    lap.sector1_time_minutes_part,
                    lap.sector1_time_ms_part,
                ),
                sector2_time_ms=_combined_time_ms(
                    lap.sector2_time_minutes_part,
                    lap.sector2_time_ms_part,
                ),
                sector3_time_ms=_combined_time_ms(
                    lap.sector3_time_minutes_part,
                    lap.sector3_time_ms_part,
                ),
                validity_flags=int(lap.lap_valid_bit_flags),
            )
        )

    return DecodedSessionHistory(
        identity=_identity(packet.header),
        car_index=int(packet.car_idx),
        laps=tuple(laps),
    )
