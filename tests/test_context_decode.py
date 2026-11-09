import unittest
from dataclasses import FrozenInstanceError, fields

from f1.packets import (
    PacketCarSetupData,
    PacketCarStatusData,
    PacketParticipantsData,
    PacketSessionData,
    PacketTimeTrialData,
    PacketTyreSetsData,
)

from f1_telemetry.context_decode import (
    CarSetup,
    decode_car_setup,
    decode_car_status,
    decode_participants,
    decode_session_context,
    decode_time_trial,
    decode_tyre_sets,
)
from f1_telemetry.decode import PacketDecodeError, decode_session_data
from tests.test_decode_normalize import populate_header


def make_packet(packet_type, packet_id, *, player=3):
    packet = packet_type()
    populate_header(packet.header, packet_id, player_car_index=player)
    return packet


class ContextHeaderTests(unittest.TestCase):
    def test_context_decoders_require_exact_f1_25_wire_contract(self):
        cases = (
            (PacketSessionData, decode_session_context, 1, 753),
            (PacketParticipantsData, decode_participants, 4, 1284),
            (PacketCarSetupData, decode_car_setup, 5, 1133),
            (PacketCarStatusData, decode_car_status, 7, 1239),
            (PacketTyreSetsData, decode_tyre_sets, 12, 231),
            (PacketTimeTrialData, decode_time_trial, 14, 101),
        )
        for packet_type, decode, packet_id, size in cases:
            packet = make_packet(packet_type, packet_id)
            with self.subTest(packet_id=packet_id):
                self.assertEqual(len(packet.pack()), size)
                self.assertEqual(decode(packet.pack()).identity.frame_key, (123, 10, 20, 3))
                for payload, error in (
                    (b"", "header is truncated"),
                    (packet.pack()[:-1], f"expected {size}"),
                    (packet.pack() + b"\x00", f"expected {size}"),
                ):
                    with self.assertRaisesRegex(PacketDecodeError, error):
                        decode(payload)
            for field, value, error in (
                ("packet_format", 2024, "unsupported packet format"),
                ("game_year", 24, "unsupported game year"),
                ("packet_version", 2, "unsupported packet version"),
                ("packet_id", 99, "unexpected packet ID"),
                ("player_car_index", 255, "outside 0..21"),
                ("session_time", float("nan"), "session time"),
            ):
                with self.subTest(packet_id=packet_id, field=field):
                    packet = make_packet(packet_type, packet_id)
                    setattr(packet.header, field, value)
                    with self.assertRaisesRegex(PacketDecodeError, error):
                        decode(packet.pack())


class SessionContextDecoderTests(unittest.TestCase):
    def test_decodes_environment_assists_rules_and_safety_state(self):
        packet = make_packet(PacketSessionData, 1)
        packet.session_type = 18
        packet.game_mode = 5
        packet.rule_set = 2
        packet.weather = 4
        packet.track_temperature = 16
        packet.air_temperature = -2
        packet.time_of_day = 1439
        packet.braking_assist = 3
        packet.gearbox_assist = 2
        packet.drs_assist = 1
        packet.equal_car_performance = 1
        packet.safety_car_status = 2
        packet.num_red_flag_periods = 1
        packet.car_damage = 3
        context = decode_session_context(packet.pack())
        self.assertEqual((context.session_type, context.game_mode), (18, 5))
        self.assertEqual((context.weather, context.track_temperature_c, context.air_temperature_c), (4, 16, -2))
        self.assertEqual(context.time_of_day_minutes, 1439)
        self.assertEqual((context.assists.braking, context.assists.gearbox), (3, 2))
        self.assertTrue(context.assists.drs)
        self.assertTrue(context.equal_car_performance)
        self.assertEqual((context.safety_car_status, context.num_red_flag_periods), (2, 1))
        self.assertEqual((context.rules.rule_set, context.rules.car_damage), (2, 3))

    def test_bad_optional_fields_do_not_change_core_session_decoding(self):
        packet = make_packet(PacketSessionData, 1)
        packet.track_length = 3323
        packet.track_id = 5
        context = decode_session_context(packet.pack())
        self.assertIsNone(context.assists.gearbox)
        self.assertIsNone(context.game_mode)
        for field, value, error in (
            ("weather", 6, "weather"),
            ("game_mode", 255, "game mode"),
            ("time_of_day", 1440, "time of day"),
            ("gearbox_assist", 4, "gearbox assist"),
            ("equal_car_performance", 2, "equal car performance"),
            ("safety_car_status", 4, "safety car status"),
            ("rule_set", 3, "ruleset"),
        ):
            with self.subTest(field=field):
                original = getattr(packet, field)
                setattr(packet, field, value)
                with self.assertRaisesRegex(PacketDecodeError, error):
                    decode_session_context(packet.pack())
                self.assertEqual(decode_session_data(packet.pack()).track_length, 3323)
                setattr(packet, field, original)

    def test_documented_late_game_modes_are_not_treated_as_sentinels(self):
        packet = make_packet(PacketSessionData, 1)
        for mode in (75, 127):
            with self.subTest(mode=mode):
                packet.game_mode = mode
                self.assertEqual(decode_session_context(packet.pack()).game_mode, mode)


class PlayerContextDecoderTests(unittest.TestCase):
    def test_participants_uses_selected_slot_and_decodes_utf8(self):
        packet = make_packet(PacketParticipantsData, 4)
        packet.num_active_cars = 1
        packet.participants[0].name = b"other"
        player = packet.participants[3]
        player.name = "Léa…".encode("utf-8")
        player.driver_id = 255
        player.platform = 255
        player.team_id = 2
        player.race_number = 7
        player.your_telemetry = 1
        player.num_colours = 1
        player.livery_colours[0].red = 240
        decoded = decode_participants(packet.pack()).player
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded.name, "Léa…")
        self.assertEqual((decoded.team_id, decoded.race_number), (2, 7))
        self.assertEqual((decoded.driver_id, decoded.platform), (None, None))
        self.assertTrue(decoded.public_telemetry)
        self.assertEqual(decoded.livery_colours, ((240, 0, 0),))
        with self.assertRaises(FrozenInstanceError):
            decoded.team_id = 5

    def test_participants_rejects_bad_count_name_and_player_flags(self):
        packet = make_packet(PacketParticipantsData, 4)
        packet.num_active_cars = 23
        with self.assertRaisesRegex(PacketDecodeError, "active car count"):
            decode_participants(packet.pack())
        packet.num_active_cars = 1
        player = packet.participants[3]
        self.assertIsNone(decode_participants(packet.pack()).player)
        player.name = b"\xff"
        with self.assertRaisesRegex(PacketDecodeError, "UTF-8"):
            decode_participants(packet.pack())
        player.name = b"x" * 32
        with self.assertRaisesRegex(PacketDecodeError, "NUL-terminated"):
            decode_participants(packet.pack())
        player.name = b"driver"
        player.num_colours = 5
        with self.assertRaisesRegex(PacketDecodeError, "livery colour count"):
            decode_participants(packet.pack())
        player.num_colours = 4
        player.ai_controlled = 2
        with self.assertRaisesRegex(PacketDecodeError, "AI controlled"):
            decode_participants(packet.pack())

    def test_setup_retains_all_23_player_fields_and_exact_binary32_values(self):
        packet = make_packet(PacketCarSetupData, 5)
        self.assertIsNone(decode_car_setup(packet.pack()).setup)
        packet.car_setup_data[0].front_wing = 1
        player = packet.car_setup_data[3]
        player.front_wing = 42
        player.rear_wing = 30
        player.front_camber = -2.2
        player.front_left_tyre_pressure = 22.3
        player.on_throttle = 100
        player.fuel_load = 11.5
        packet.next_front_wing_value = 41.0
        decoded = decode_car_setup(packet.pack())
        self.assertEqual(len(fields(CarSetup)), 23)
        self.assertEqual(decoded.setup.front_wing, 42)
        self.assertEqual(decoded.setup.front_camber.hex(), "-0x1.19999a0000000p+1")
        self.assertEqual(decoded.setup.front_left_tyre_pressure, player.front_left_tyre_pressure)
        self.assertEqual((decoded.setup.on_throttle, decoded.setup.fuel_load), (100, 11.5))
        self.assertEqual(decoded.next_front_wing_value, 41.0)

    def test_setup_rejects_bad_percent_nonfinite_float_and_negative_pressure(self):
        packet = make_packet(PacketCarSetupData, 5)
        player = packet.car_setup_data[3]
        for field, value, error in (
            ("brake_bias", 101, "brake bias"),
            ("rear_camber", float("nan"), "rear camber"),
            ("rear_left_tyre_pressure", -1.0, "rear left tyre pressure"),
        ):
            with self.subTest(field=field):
                setattr(player, field, value)
                with self.assertRaisesRegex(PacketDecodeError, error):
                    decode_car_setup(packet.pack())
                setattr(player, field, 0)

    def test_status_uses_player_fuel_tyre_ers_and_assist_values(self):
        packet = make_packet(PacketCarStatusData, 7)
        self.assertIsNone(decode_car_status(packet.pack()).status)
        packet.car_status_data[0].fuel_in_tank = 55
        player = packet.car_status_data[3]
        player.fuel_in_tank = 10.25
        player.fuel_capacity = 110
        player.fuel_remaining_laps = -0.25
        player.actual_tyre_compound = 22
        player.visual_tyre_compound = 16
        player.vehicle_fia_flags = -1
        player.traction_control = 2
        player.anti_lock_brakes = 1
        player.ers_deploy_mode = 3
        player.ers_store_energy = 1200
        decoded = decode_car_status(packet.pack()).status
        self.assertEqual((decoded.fuel_in_tank_kg, decoded.fuel_remaining_laps), (10.25, -0.25))
        self.assertEqual((decoded.actual_tyre_compound, decoded.visual_tyre_compound), (22, 16))
        self.assertIsNone(decoded.vehicle_fia_flags)
        self.assertEqual((decoded.traction_control, decoded.anti_lock_brakes), (2, True))
        self.assertEqual((decoded.ers_deploy_mode, decoded.ers_store_energy_j), (3, 1200))

    def test_status_rejects_bad_assist_flags_and_nonfinite_fuel(self):
        packet = make_packet(PacketCarStatusData, 7)
        player = packet.car_status_data[3]
        for field, value, error in (
            ("traction_control", 3, "traction control"),
            ("vehicle_fia_flags", -2, "vehicle FIA flags"),
            ("fuel_in_tank", float("inf"), "fuel in tank"),
            ("actual_tyre_compound", 23, "actual tyre compound"),
        ):
            with self.subTest(field=field):
                setattr(player, field, value)
                with self.assertRaisesRegex(PacketDecodeError, error):
                    decode_car_status(packet.pack())
                setattr(player, field, 0)


class TargetedContextDecoderTests(unittest.TestCase):
    def test_tyre_sets_uses_the_explicit_target_and_preserves_slot_indices(self):
        packet = make_packet(PacketTyreSetsData, 12)
        packet.car_idx = 5
        decoded = decode_tyre_sets(packet.pack())
        self.assertEqual((decoded.identity.player_car_index, decoded.car_index), (3, 5))
        self.assertEqual(decoded.sets, (None,) * 20)
        self.assertIsNone(decoded.fitted_index)
        tyre = packet.tyre_set_data[19]
        tyre.actual_tyre_compound = 7
        tyre.visual_tyre_compound = 7
        tyre.wear = 100
        tyre.lap_delta_time = -120
        tyre.fitted = 1
        packet.fitted_idx = 19
        decoded = decode_tyre_sets(packet.pack())
        self.assertEqual(decoded.fitted_index, 19)
        self.assertEqual(decoded.sets[:19], (None,) * 19)
        self.assertEqual((decoded.sets[19].wear_percent, decoded.sets[19].lap_delta_time_ms), (100, -120))
        packet.fitted_idx = 255
        self.assertIsNone(decode_tyre_sets(packet.pack()).fitted_index)

    def test_tyre_sets_rejects_invalid_target_fitted_index_and_wear(self):
        packet = make_packet(PacketTyreSetsData, 12)
        packet.car_idx = 22
        with self.assertRaisesRegex(PacketDecodeError, "tyre sets car index"):
            decode_tyre_sets(packet.pack())
        packet.car_idx = 3
        packet.fitted_idx = 20
        with self.assertRaisesRegex(PacketDecodeError, "fitted tyre index"):
            decode_tyre_sets(packet.pack())
        packet.fitted_idx = 0
        packet.tyre_set_data[0].wear = 101
        with self.assertRaisesRegex(PacketDecodeError, "tyre wear"):
            decode_tyre_sets(packet.pack())

    def test_time_trial_preserves_all_three_roles_and_validity(self):
        packet = make_packet(PacketTimeTrialData, 14)
        self.assertIsNone(decode_time_trial(packet.pack()).rival)
        player = packet.player_session_best_data_set
        player.car_idx = 3
        player.team_id = 2
        player.lap_time_in_ms = 60_011
        player.sector1_time_in_ms = 20_003
        player.sector2_time_in_ms = 20_003
        player.sector3_time_in_ms = 20_005
        player.equal_car_performance = 1
        personal_best = packet.personal_best_data_set
        personal_best.car_idx = 6
        personal_best.valid = 1
        rival = packet.rival_data_set
        rival.car_idx = 21
        rival.custom_setup = 1
        decoded = decode_time_trial(packet.pack())
        self.assertEqual(decoded.player_session_best.lap_time_ms, 60_011)
        self.assertEqual(decoded.player_session_best.sector_times_ms, (20_003, 20_003, 20_005))
        self.assertTrue(decoded.player_session_best.equal_car_performance)
        self.assertFalse(decoded.player_session_best.valid)
        self.assertEqual(decoded.personal_best.car_index, 6)
        self.assertTrue(decoded.personal_best.valid)
        self.assertEqual(decoded.rival.car_index, 21)
        self.assertTrue(decoded.rival.custom_setup)

    def test_time_trial_checks_target_slots_and_boolean_fields(self):
        packet = make_packet(PacketTimeTrialData, 14)
        rival = packet.rival_data_set
        rival.car_idx = 255
        self.assertIsNone(decode_time_trial(packet.pack()).rival.car_index)
        rival.valid = 1
        with self.assertRaisesRegex(PacketDecodeError, "Time Trial car index"):
            decode_time_trial(packet.pack())
        rival.car_idx = 21
        rival.valid = 2
        with self.assertRaisesRegex(PacketDecodeError, "Time Trial lap valid"):
            decode_time_trial(packet.pack())
