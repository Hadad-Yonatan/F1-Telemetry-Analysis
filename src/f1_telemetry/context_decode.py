"""Strict, immutable snapshots from F1 25 context packets."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import TypeVar

from f1.packets import (
    PacketCarSetupData,
    PacketCarStatusData,
    PacketParticipantsData,
    PacketSessionData,
    PacketTimeTrialData,
    PacketTyreSetsData,
    TimeTrialDataSet,
    TyreSetData,
)

from f1_telemetry.decode import (
    MAX_CARS,
    SESSION_PACKET_ID,
    PacketDecodeError,
    PacketIdentity,
    _decode_header,
    _identity,
    _validate_packet,
)

PARTICIPANTS_PACKET_ID = 4
CAR_SETUPS_PACKET_ID = 5
CAR_STATUS_PACKET_ID = 7
TYRE_SETS_PACKET_ID = 12
TIME_TRIAL_PACKET_ID = 14

_Packet = TypeVar(
    "_Packet",
    PacketSessionData,
    PacketParticipantsData,
    PacketCarSetupData,
    PacketCarStatusData,
    PacketTyreSetsData,
    PacketTimeTrialData,
)


@dataclass(frozen=True)
class SessionAssists:
    steering: bool
    braking: int
    gearbox: int | None
    pit: bool
    pit_release: bool
    ers: bool
    drs: bool
    dynamic_racing_line: int
    dynamic_racing_line_type: int


@dataclass(frozen=True)
class SessionRules:
    rule_set: int
    corner_cutting_stringency: int
    surface_type: int
    car_damage: int
    car_damage_rate: int
    collisions: int
    parc_ferme: bool
    safety_car: int
    formation_lap: bool
    red_flags: int


@dataclass(frozen=True)
class DecodedSessionContext:
    identity: PacketIdentity
    session_type: int
    game_mode: int | None
    formula: int
    weather: int
    track_temperature_c: int
    air_temperature_c: int
    time_of_day_minutes: int
    total_laps: int
    session_time_left_s: int
    session_duration_s: int
    game_paused: bool
    network_game: bool
    equal_car_performance: bool
    safety_car_status: int
    num_safety_car_periods: int
    num_virtual_safety_car_periods: int
    num_red_flag_periods: int
    assists: SessionAssists
    rules: SessionRules


@dataclass(frozen=True)
class Participant:
    name: str | None
    ai_controlled: bool
    driver_id: int | None
    network_id: int
    team_id: int
    my_team: bool
    race_number: int
    nationality: int | None
    public_telemetry: bool
    show_online_names: bool
    tech_level: int
    platform: int | None
    livery_colours: tuple[tuple[int, int, int], ...]


@dataclass(frozen=True)
class DecodedParticipants:
    identity: PacketIdentity
    num_active_cars: int
    player: Participant | None


@dataclass(frozen=True)
class CarSetup:
    front_wing: int
    rear_wing: int
    on_throttle: int
    off_throttle: int
    front_camber: float
    rear_camber: float
    front_toe: float
    rear_toe: float
    front_suspension: int
    rear_suspension: int
    front_anti_roll_bar: int
    rear_anti_roll_bar: int
    front_suspension_height: int
    rear_suspension_height: int
    brake_pressure: int
    brake_bias: int
    engine_braking: int
    rear_left_tyre_pressure: float
    rear_right_tyre_pressure: float
    front_left_tyre_pressure: float
    front_right_tyre_pressure: float
    ballast: int
    fuel_load: float


@dataclass(frozen=True)
class DecodedCarSetup:
    identity: PacketIdentity
    setup: CarSetup | None
    next_front_wing_value: float | None


@dataclass(frozen=True)
class CarStatus:
    traction_control: int
    anti_lock_brakes: bool
    fuel_mix: int
    front_brake_bias: int
    pit_limiter: bool
    fuel_in_tank_kg: float
    fuel_capacity_kg: float
    fuel_remaining_laps: float
    max_rpm: int
    idle_rpm: int
    max_gears: int
    drs_allowed: bool
    drs_activation_distance_m: int
    actual_tyre_compound: int | None
    visual_tyre_compound: int | None
    tyres_age_laps: int
    vehicle_fia_flags: int | None
    engine_power_ice_w: float
    engine_power_mguk_w: float
    ers_store_energy_j: float
    ers_deploy_mode: int
    ers_harvested_this_lap_mguk_j: float
    ers_harvested_this_lap_mguh_j: float
    ers_deployed_this_lap_j: float
    network_paused: bool


@dataclass(frozen=True)
class DecodedCarStatus:
    identity: PacketIdentity
    status: CarStatus | None


@dataclass(frozen=True)
class TyreSet:
    actual_tyre_compound: int | None
    visual_tyre_compound: int | None
    wear_percent: int
    available: bool
    recommended_session: int | None
    life_span_laps: int
    usable_life_laps: int
    lap_delta_time_ms: int
    fitted: bool


@dataclass(frozen=True)
class DecodedTyreSets:
    identity: PacketIdentity
    car_index: int
    sets: tuple[TyreSet | None, ...]
    fitted_index: int | None


@dataclass(frozen=True)
class TimeTrialLap:
    car_index: int | None
    team_id: int
    lap_time_ms: int
    sector_times_ms: tuple[int, int, int]
    traction_control: bool
    gearbox_assist: bool
    anti_lock_brakes: bool
    equal_car_performance: bool
    custom_setup: bool
    valid: bool


@dataclass(frozen=True)
class DecodedTimeTrial:
    identity: PacketIdentity
    player_session_best: TimeTrialLap | None
    personal_best: TimeTrialLap | None
    rival: TimeTrialLap | None


def _unpack(payload: bytes, packet_type: type[_Packet], packet_id: int) -> _Packet:
    header = _decode_header(payload)
    _validate_packet(
        payload, header, expected_packet_id=packet_id, expected_size=packet_type.size()
    )
    _finite("session time", header.session_time, nonnegative=True)
    return packet_type.unpack(payload)


def _bounded(name: str, value: int, upper: int) -> int:
    if not 0 <= value <= upper:
        raise PacketDecodeError(f"{name} value {value} is outside 0..{upper}")
    return value


def _choice(name: str, value: int, choices: tuple[int, ...]) -> int:
    if value not in choices:
        raise PacketDecodeError(f"{name} value {value} is not in {choices}")
    return value


def _flag(name: str, value: int) -> bool:
    return bool(_bounded(name, value, 1))


def _finite(name: str, value: float, *, nonnegative: bool = False) -> float:
    if not isfinite(value) or (nonnegative and value < 0):
        requirement = "finite and nonnegative" if nonnegative else "finite"
        raise PacketDecodeError(f"{name} must be {requirement}; got {value}")
    return value


def decode_session_context(payload: bytes) -> DecodedSessionContext:
    """Decode the optional context fields without changing core Session decoding."""
    packet = _unpack(payload, PacketSessionData, SESSION_PACKET_ID)
    return DecodedSessionContext(
        identity=_identity(packet.header),
        session_type=_bounded("session type", packet.session_type, 18),
        game_mode=_choice(
            "game mode", packet.game_mode, (0, 4, 5, 6, 7, 15, 17, 27, 28, 29, 30, 75, 127)
        ) or None,
        formula=_choice("formula", packet.formula, (0, 1, 2, 3, 4, 6, 8, 9)),
        weather=_bounded("weather", packet.weather, 5),
        track_temperature_c=packet.track_temperature,
        air_temperature_c=packet.air_temperature,
        time_of_day_minutes=_bounded("time of day", packet.time_of_day, 1439),
        total_laps=packet.total_laps,
        session_time_left_s=packet.session_time_left,
        session_duration_s=packet.session_duration,
        game_paused=_flag("game paused", packet.game_paused),
        network_game=_flag("network game", packet.network_game),
        equal_car_performance=_flag("equal car performance", packet.equal_car_performance),
        safety_car_status=_bounded("safety car status", packet.safety_car_status, 3),
        num_safety_car_periods=packet.num_safety_car_periods,
        num_virtual_safety_car_periods=packet.num_virtual_safety_car_periods,
        num_red_flag_periods=packet.num_red_flag_periods,
        assists=SessionAssists(
            steering=_flag("steering assist", packet.steering_assist),
            braking=_bounded("braking assist", packet.braking_assist, 3),
            gearbox=_bounded("gearbox assist", packet.gearbox_assist, 3) or None,
            pit=_flag("pit assist", packet.pit_assist),
            pit_release=_flag("pit release assist", packet.pit_release_assist),
            ers=_flag("ERS assist", packet.ers_assist),
            drs=_flag("DRS assist", packet.drs_assist),
            dynamic_racing_line=_bounded("dynamic racing line", packet.dynamic_racing_line, 2),
            dynamic_racing_line_type=_bounded(
                "dynamic racing line type", packet.dynamic_racing_line_type, 1
            ),
        ),
        rules=SessionRules(
            rule_set=_choice("ruleset", packet.rule_set, (0, 1, 2, 12)),
            corner_cutting_stringency=_bounded(
                "corner cutting stringency", packet.corner_cutting_stringency, 1
            ),
            surface_type=_bounded("surface type", packet.surface_type, 1),
            car_damage=_bounded("car damage", packet.car_damage, 3),
            car_damage_rate=_bounded("car damage rate", packet.car_damage_rate, 2),
            collisions=_bounded("collisions", packet.collisions, 2),
            parc_ferme=_flag("parc fermé rules", packet.parc_ferme_rules),
            safety_car=_bounded("safety car rule", packet.safety_car, 3),
            formation_lap=_flag("formation lap", packet.formation_lap),
            red_flags=_bounded("red flag rule", packet.red_flags, 3),
        ),
    )


def decode_participants(payload: bytes) -> DecodedParticipants:
    packet = _unpack(payload, PacketParticipantsData, PARTICIPANTS_PACKET_ID)
    identity = _identity(packet.header)
    count = _bounded("active car count", packet.num_active_cars, MAX_CARS)
    player = packet.participants[identity.player_car_index]
    # The active count is not a guarantee that the occupied car slots are a prefix.
    if count == 0 or not any(bytes(player)):
        return DecodedParticipants(identity, count, None)

    # ctypes returns the bytes before the first NUL, or all 32 if none exists.
    if len(player.name) == 32:
        raise PacketDecodeError("participant name is not NUL-terminated")
    try:
        name = player.name.decode("utf-8") or None
    except UnicodeDecodeError as error:
        raise PacketDecodeError("participant name is not valid UTF-8") from error
    colours = _bounded("livery colour count", player.num_colours, 4)
    platform = _choice("participant platform", player.platform, (0, 1, 3, 4, 6, 255))
    return DecodedParticipants(
        identity,
        count,
        Participant(
            name=name,
            ai_controlled=_flag("AI controlled", player.ai_controlled),
            driver_id=None if player.driver_id == 255 else player.driver_id,
            network_id=player.network_id,
            team_id=player.team_id,
            my_team=_flag("My Team", player.my_team),
            race_number=player.race_number,
            nationality=player.nationality or None,
            public_telemetry=_flag("public telemetry", player.your_telemetry),
            show_online_names=_flag("show online names", player.show_online_names),
            tech_level=player.tech_level,
            platform=None if platform in (0, 255) else platform,
            livery_colours=tuple(
                (colour.red, colour.green, colour.blue)
                for colour in player.livery_colours[:colours]
            ),
        ),
    )


def decode_car_setup(payload: bytes) -> DecodedCarSetup:
    packet = _unpack(payload, PacketCarSetupData, CAR_SETUPS_PACKET_ID)
    identity = _identity(packet.header)
    setup = packet.car_setup_data[identity.player_car_index]
    next_wing = _finite("next front wing value", packet.next_front_wing_value)
    if not any(bytes(setup)):
        return DecodedCarSetup(identity, None, None)

    return DecodedCarSetup(
        identity,
        CarSetup(
            front_wing=setup.front_wing,
            rear_wing=setup.rear_wing,
            on_throttle=_bounded("on-throttle differential", setup.on_throttle, 100),
            off_throttle=_bounded("off-throttle differential", setup.off_throttle, 100),
            front_camber=_finite("front camber", setup.front_camber),
            rear_camber=_finite("rear camber", setup.rear_camber),
            front_toe=_finite("front toe", setup.front_toe),
            rear_toe=_finite("rear toe", setup.rear_toe),
            front_suspension=setup.front_suspension,
            rear_suspension=setup.rear_suspension,
            front_anti_roll_bar=setup.front_anti_roll_bar,
            rear_anti_roll_bar=setup.rear_anti_roll_bar,
            front_suspension_height=setup.front_suspension_height,
            rear_suspension_height=setup.rear_suspension_height,
            brake_pressure=_bounded("brake pressure", setup.brake_pressure, 100),
            brake_bias=_bounded("brake bias", setup.brake_bias, 100),
            engine_braking=_bounded("engine braking", setup.engine_braking, 100),
            rear_left_tyre_pressure=_finite(
                "rear left tyre pressure", setup.rear_left_tyre_pressure, nonnegative=True
            ),
            rear_right_tyre_pressure=_finite(
                "rear right tyre pressure", setup.rear_right_tyre_pressure, nonnegative=True
            ),
            front_left_tyre_pressure=_finite(
                "front left tyre pressure", setup.front_left_tyre_pressure, nonnegative=True
            ),
            front_right_tyre_pressure=_finite(
                "front right tyre pressure", setup.front_right_tyre_pressure, nonnegative=True
            ),
            ballast=setup.ballast,
            fuel_load=_finite("fuel load", setup.fuel_load, nonnegative=True),
        ),
        next_wing,
    )


def _actual_compound(value: int) -> int | None:
    return _choice("actual tyre compound", value, (0, *range(7, 23))) or None


def _visual_compound(value: int) -> int | None:
    return _choice("visual tyre compound", value, (0, 7, 8, 9, 10, *range(15, 23))) or None


def decode_car_status(payload: bytes) -> DecodedCarStatus:
    packet = _unpack(payload, PacketCarStatusData, CAR_STATUS_PACKET_ID)
    identity = _identity(packet.header)
    status = packet.car_status_data[identity.player_car_index]
    if not any(bytes(status)):
        return DecodedCarStatus(identity, None)

    flags = _choice("vehicle FIA flags", status.vehicle_fia_flags, (-1, 0, 1, 2, 3))
    return DecodedCarStatus(
        identity,
        CarStatus(
            traction_control=_bounded("traction control", status.traction_control, 2),
            anti_lock_brakes=_flag("anti-lock brakes", status.anti_lock_brakes),
            fuel_mix=_bounded("fuel mix", status.fuel_mix, 3),
            front_brake_bias=_bounded("front brake bias", status.front_brake_bias, 100),
            pit_limiter=_flag("pit limiter", status.pit_limiter_status),
            fuel_in_tank_kg=_finite("fuel in tank", status.fuel_in_tank, nonnegative=True),
            fuel_capacity_kg=_finite("fuel capacity", status.fuel_capacity, nonnegative=True),
            fuel_remaining_laps=_finite("fuel remaining laps", status.fuel_remaining_laps),
            max_rpm=status.max_rpm,
            idle_rpm=status.idle_rpm,
            max_gears=status.max_gears,
            drs_allowed=_flag("DRS allowed", status.drs_allowed),
            drs_activation_distance_m=status.drs_activation_distance,
            actual_tyre_compound=_actual_compound(status.actual_tyre_compound),
            visual_tyre_compound=_visual_compound(status.visual_tyre_compound),
            tyres_age_laps=status.tyres_age_laps,
            vehicle_fia_flags=None if flags == -1 else flags,
            engine_power_ice_w=_finite("ICE power", status.engine_power_ice),
            engine_power_mguk_w=_finite("MGU-K power", status.engine_power_mguk),
            ers_store_energy_j=_finite("ERS energy store", status.ers_store_energy),
            ers_deploy_mode=_bounded("ERS deployment mode", status.ers_deploy_mode, 3),
            ers_harvested_this_lap_mguk_j=_finite(
                "MGU-K energy harvested", status.ers_harvested_this_lap_mguk
            ),
            ers_harvested_this_lap_mguh_j=_finite(
                "MGU-H energy harvested", status.ers_harvested_this_lap_mguh
            ),
            ers_deployed_this_lap_j=_finite("ERS energy deployed", status.ers_deployed_this_lap),
            network_paused=_flag("network paused", status.network_paused),
        ),
    )


def _tyre_set(data: TyreSetData) -> TyreSet | None:
    if not any(bytes(data)):
        return None
    return TyreSet(
        actual_tyre_compound=_actual_compound(data.actual_tyre_compound),
        visual_tyre_compound=_visual_compound(data.visual_tyre_compound),
        wear_percent=_bounded("tyre wear", data.wear, 100),
        available=_flag("tyre availability", data.available),
        recommended_session=_bounded("recommended tyre session", data.recommended_session, 18) or None,
        life_span_laps=data.life_span,
        usable_life_laps=data.usable_life,
        lap_delta_time_ms=data.lap_delta_time,
        fitted=_flag("tyre fitted", data.fitted),
    )


def decode_tyre_sets(payload: bytes) -> DecodedTyreSets:
    """Decode the target car's sets; this packet cycles through car slots."""
    packet = _unpack(payload, PacketTyreSetsData, TYRE_SETS_PACKET_ID)
    car_index = _bounded("tyre sets car index", packet.car_idx, MAX_CARS - 1)
    sets = tuple(_tyre_set(data) for data in packet.tyre_set_data)
    fitted = packet.fitted_idx
    if fitted == 255:
        fitted = None
    else:
        _bounded("fitted tyre index", fitted, len(sets) - 1)
        if sets[fitted] is None:
            fitted = None
    return DecodedTyreSets(_identity(packet.header), car_index, sets, fitted)


def _time_trial_lap(data: TimeTrialDataSet) -> TimeTrialLap | None:
    if not any(bytes(data)):
        return None
    valid = _flag("Time Trial lap valid", data.valid)
    car_index = (
        None if data.car_idx == 255 and not valid
        else _bounded("Time Trial car index", data.car_idx, MAX_CARS - 1)
    )
    return TimeTrialLap(
        car_index=car_index,
        team_id=data.team_id,
        lap_time_ms=data.lap_time_in_ms,
        sector_times_ms=(data.sector1_time_in_ms, data.sector2_time_in_ms, data.sector3_time_in_ms),
        traction_control=_flag("Time Trial traction control", data.traction_control),
        gearbox_assist=_flag("Time Trial gearbox assist", data.gearbox_assist),
        anti_lock_brakes=_flag("Time Trial anti-lock brakes", data.anti_lock_brakes),
        equal_car_performance=_flag("Time Trial equal car performance", data.equal_car_performance),
        custom_setup=_flag("Time Trial custom setup", data.custom_setup),
        valid=valid,
    )


def decode_time_trial(payload: bytes) -> DecodedTimeTrial:
    packet = _unpack(payload, PacketTimeTrialData, TIME_TRIAL_PACKET_ID)
    return DecodedTimeTrial(
        _identity(packet.header),
        _time_trial_lap(packet.player_session_best_data_set),
        _time_trial_lap(packet.personal_best_data_set),
        _time_trial_lap(packet.rival_data_set),
    )
