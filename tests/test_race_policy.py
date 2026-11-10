import tempfile
import unittest
from pathlib import Path

from f1.packets import (
    PacketCarDamageData, PacketCarStatusData, PacketEventData, PacketLapData,
    PacketParticipantsData, PacketSessionHistoryData,
)

from f1_telemetry.analysis_output import analysis_document, write_analysis_bundle
from f1_telemetry.bundle import verify_bundle
from f1_telemetry.capture_format import CaptureWriter
from f1_telemetry.decode import PacketDecodeError
from f1_telemetry.lap_analysis import analyze_capture, prepare_session
from f1_telemetry.race_decode import decode_race_damage, decode_race_event, decode_race_pit_evidence
from tests.test_decode_normalize import make_telemetry_packet
from tests.test_session_context import packet, session_packet, setup_packet


def event_packet(frame, code, safety_type=0, safety_event=0, **identity):
    value = packet(PacketEventData, 3, frame, **identity)
    value.event_string_code[:] = code.encode("ascii")
    if code == "SCAR":
        value.event_details.safety_car.safety_car_type = safety_type
        value.event_details.safety_car.event_type = safety_event
    return value


def damage_packet(frame, damage=0, wear=0, **identity):
    value = packet(PacketCarDamageData, 10, frame, **identity)
    car = value.car_damage_data[value.header.player_car_index]
    car.front_left_wing_damage = damage
    car.tyres_wear[:] = [wear] * 4
    return value


def write_race(
    path: Path, *, events=True, damage=True, malformed=False, red_event=True, resume_lap=10,
    red_in_unpaired_tail=False, green_caution_counters=False, malformed_on_start=False,
):
    sender = ("192.0.2.10", 20777)
    history = packet(PacketSessionHistoryData, 11, 10_000)
    history.num_laps = 15
    with CaptureWriter(path, created_unix_ns=42) as writer:
        def write(value, frame):
            writer.write_datagram(value.pack(), sender, captured_unix_ns=frame)

        if events:
            write(event_packet(1, "SSTA"), 1)
        for number in range(1, 16):
            base = number * 100
            official = 3000 + 100 * number
            lap_record = history.lap_history_data[number - 1]
            lap_record.lap_time_in_ms = official
            lap_record.sector1_time_ms_part = 900
            lap_record.sector2_time_ms_part = 900
            lap_record.sector3_time_ms_part = official - 1800
            lap_record.lap_valid_bit_flags = 15
            session = session_packet(base + 1)
            session.session_type = 15
            session.gearbox_assist = 2
            session.num_red_flag_periods = int(number >= 10)
            if green_caution_counters and number >= 2:
                session.num_safety_car_periods = session.num_virtual_safety_car_periods = 1
            write(session, base + 1)
            participant = packet(PacketParticipantsData, 4, base + 2)
            participant.num_active_cars = 1
            participant.participants[0].name = b"Driver"
            participant.participants[0].team_id = 3
            write(participant, base + 2)
            write(setup_packet(base + 3, 25), base + 3)

            def tyres(frame, compound):
                status = packet(PacketCarStatusData, 7, frame)
                car = status.car_status_data[0]
                car.actual_tyre_compound = compound
                car.visual_tyre_compound = 16
                car.fuel_in_tank = 40 - number
                write(status, frame)

            initial_compound = 16 if number <= 3 else 17 if number <= 13 else 18
            tyres(base + 4, initial_compound)
            if damage:
                write(damage_packet(base + 5, 20 if number >= 11 else 0, wear=number), base + 5)
            if events and (number == resume_lap or red_in_unpaired_tail and number == 2):
                write(event_packet(base + 6, "LGOT"), base + 6)
            for offset, distance, elapsed in ((10, 0, 1), (20, 50, 2000), (30, 99, official - 10)):
                frame = base + offset
                lap_packet = packet(PacketLapData, 2, frame)
                lap = lap_packet.lap_data[0]
                lap.current_lap_num = number
                lap.current_lap_time_in_ms = elapsed
                lap.lap_distance = distance
                lap.driver_status = 4
                lap.pit_status = 2 if number == 3 and offset >= 20 else 0
                lap.num_pit_stops = int(number > 3 or number == 3 and offset >= 20)
                write(lap_packet, frame)
                writer.write_datagram(
                    make_telemetry_packet(frame_identifier=frame, overall_frame_identifier=frame),
                    sender, captured_unix_ns=frame,
                )
                if malformed_on_start and number == 1 and offset == 10:
                    writer.write_datagram(event_packet(frame, "RDFL").pack()[:-1], sender, captured_unix_ns=frame)
                    writer.write_datagram(damage_packet(frame).pack()[:-1], sender, captured_unix_ns=frame + 1)
                if offset != 20:
                    continue
                if number in (3, 13):
                    tyres(frame + 1, initial_compound + 1)
                if events:
                    control = {
                        6: ("SCAR", 1, 0), 7: ("SCAR", 1, 3),
                        8: ("SCAR", 2, 0), 9: ("RDFL", 0, 0) if red_event else None,
                    }.get(number)
                    if control:
                        write(event_packet(frame + 2, *control), frame + 2)
                    if number == 8:
                        write(event_packet(frame + 3, "SCAR", 2, 3), frame + 3)
                    if number == 9 and not red_event:
                        session.header.frame_identifier = frame + 2
                        session.header.overall_frame_identifier = frame + 2
                        session.num_red_flag_periods = 1
                        write(session, frame + 2)
                    if number == 1 and green_caution_counters:
                        session.header.frame_identifier = frame + 3
                        session.header.overall_frame_identifier = frame + 3
                        session.num_safety_car_periods = session.num_virtual_safety_car_periods = 1
                        write(session, frame + 3)
                if damage:
                    write(damage_packet(frame + 4, 20 if number >= 10 else 0, wear=number + 0.5), frame + 4)
                    if malformed and number == 2:
                        raw = damage_packet(frame + 5).pack()
                        writer.write_datagram(raw[:-1], sender, captured_unix_ns=frame + 5)
            if number == 1 and red_in_unpaired_tail:
                write(event_packet(base + 31, "RDFL"), base + 31)
                tail = packet(PacketLapData, 2, base + 32)
                tail.lap_data[0].current_lap_num = number
                tail.lap_data[0].current_lap_time_in_ms = official - 1
                tail.lap_data[0].lap_distance = 99.9
                tail.lap_data[0].driver_status = 4
                write(tail, base + 32)  # Intentionally no corresponding Car Telemetry packet.
        write(history, 10_000)


class RaceDecoderTests(unittest.TestCase):
    def test_exact_event_shape_and_safety_car_enums(self):
        value = event_packet(8, "SCAR", 2, 3)
        self.assertEqual(len(value.pack()), 45)
        decoded = decode_race_event(value.pack())
        self.assertEqual((decoded.code, decoded.safety_car_type, decoded.safety_car_event), ("SCAR", 2, 3))
        self.assertEqual(decode_race_event(event_packet(8, "RDFL").pack()).code, "RDFL")
        for raw in (value.pack()[:-1], event_packet(8, "SCAR", 4, 3).pack(), event_packet(8, "????").pack()):
            with self.assertRaises(PacketDecodeError):
                decode_race_event(raw)
        value.header.game_year = 24
        with self.assertRaisesRegex(PacketDecodeError, "game year"):
            decode_race_event(value.pack())

    def test_damage_uses_selected_car_and_separates_wear_from_damage(self):
        value = damage_packet(8, damage=25, wear=30, player=21)
        self.assertEqual(len(value.pack()), 1041)
        decoded = decode_race_damage(value.pack())
        self.assertEqual(dict(decoded.damage.fields)["front_left_wing_damage"], 25)
        self.assertEqual(decoded.damage, decode_race_damage(damage_packet(9, 25, wear=32, player=21).pack()).damage)
        with self.assertRaisesRegex(PacketDecodeError, "tyre wear"):
            decode_race_damage(damage_packet(8, wear=float("nan")).pack())
        with self.assertRaisesRegex(PacketDecodeError, "percentage"):
            decode_race_damage(damage_packet(8, damage=101).pack())
        value.car_damage_data[21].ers_fault = 2
        with self.assertRaisesRegex(PacketDecodeError, "fault"):
            decode_race_damage(value.pack())

    def test_pit_counter_selected_car_and_range(self):
        value = packet(PacketLapData, 2, 17, player=4)
        value.lap_data[4].num_pit_stops = 3
        value.lap_data[4].pit_status = 2
        self.assertEqual(decode_race_pit_evidence(value.pack()).pit_stops, 3)
        value.lap_data[4].pit_status = 3
        with self.assertRaisesRegex(PacketDecodeError, "pit status"):
            decode_race_pit_evidence(value.pack())


class RacePolicyTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.capture = self.root / "race.f1cap"

    def test_opt_in_exclusions_and_stint_boundaries(self):
        write_race(self.capture)
        with self.assertRaisesRegex(ValueError, "race policy is experimental"):
            analyze_capture(self.capture)
        result = analyze_capture(self.capture, allow_experimental_session_mode=True)
        self.assertEqual((result.session.policy.name, result.session.policy.status), ("race", "experimental"))
        self.assertEqual(result.reference_lap, 1)
        self.assertEqual([item.comparison_lap for item in result.comparisons], [2])
        rows = {lap.lap_number: lap for lap in result.session.race_evidence.laps}
        self.assertEqual(rows[3].exclusion_reasons, ("pit lap", "tyre-transition lap"))
        self.assertEqual(rows[6].exclusion_reasons, ("safety-car lap",))
        self.assertEqual(rows[7].exclusion_reasons, ("safety-car lap",))
        self.assertEqual(rows[8].exclusion_reasons, ("virtual-safety-car lap",))
        self.assertEqual(rows[9].exclusion_reasons, ("red-flag lap",))
        self.assertEqual(rows[10].exclusion_reasons, ("damage-change lap",))
        self.assertEqual(rows[13].exclusion_reasons, ("tyre-transition lap",))
        self.assertEqual({number: rows[number].stint_id for number in (1, 2, 4, 5, 11, 12, 14, 15)},
                         {1: 1, 2: 1, 4: 2, 5: 2, 11: 2, 12: 2, 14: 3, 15: 3})
        self.assertEqual([lap.lap_number for lap in result.session.laps], [1, 2, 4, 5, 11, 12, 14, 15])
        self.assertEqual(analysis_document(result)["race_evidence"]["laps"][2]["exclusion_reasons"],
                         ["pit lap", "tyre-transition lap"])

    def test_explicit_cross_stint_comparison_records_both_stints(self):
        write_race(self.capture)
        common = dict(reference_lap=1, comparison_laps=(4,), allow_experimental_session_mode=True)
        with self.assertRaisesRegex(ValueError, "outside a complete condition cohort"):
            analyze_capture(self.capture, **common)
        result = analyze_capture(self.capture, allow_cross_context=True, **common)
        comparison = result.comparisons[0]
        differences = {row.field: (row.reference, row.comparison) for row in comparison.policy_context.differences}
        self.assertEqual(differences["race.stint_id"], (1, 2))
        self.assertEqual(differences["race.pit_stops_at_start"], (0, 1))
        for name in ("one", "two"):
            write_analysis_bundle(result, self.root / name)
            verify_bundle(self.root / name)
        self.assertEqual({path.name: path.read_bytes() for path in (self.root / "one").iterdir()},
                         {path.name: path.read_bytes() for path in (self.root / "two").iterdir()})

    def test_missing_or_malformed_required_evidence_fails_closed(self):
        for events, damage in ((False, True), (True, False)):
            path = self.root / f"{events}-{damage}.f1cap"
            write_race(path, events=events, damage=damage)
            with self.assertRaisesRegex(ValueError, "requires valid Event and Car Damage"):
                analyze_capture(path, allow_experimental_session_mode=True)
        write_race(self.capture, malformed=True)
        session = prepare_session(self.capture, allow_experimental_session_mode=True)
        self.assertEqual(session.validations[1].reason, "malformed race evidence during lap")
        self.assertEqual(dict(session.race_evidence.decode_errors)[10], 1)

    def test_session_red_flag_counter_can_cover_a_missing_event_until_resume(self):
        write_race(self.capture, red_event=False, resume_lap=11)
        session = prepare_session(self.capture, allow_experimental_session_mode=True)
        rows = {lap.lap_number: lap for lap in session.race_evidence.laps}
        self.assertIn("red-flag lap", rows[9].exclusion_reasons)
        self.assertIn("red-flag lap", rows[10].exclusion_reasons)
        self.assertNotIn("red-flag lap", rows[11].exclusion_reasons)

    def test_raw_lap_data_tail_can_extend_the_race_evidence_window(self):
        write_race(self.capture, red_in_unpaired_tail=True)
        session = prepare_session(self.capture, allow_experimental_session_mode=True)
        self.assertEqual(session.validations[0].reason, "red-flag lap")
        self.assertEqual(session.race_evidence.laps[0].exclusion_reasons, ("red-flag lap",))
        self.assertEqual(session.normalization.unmatched_lap_packets, 1)

    def test_caution_counters_exclude_a_lap_even_if_both_status_and_events_miss_it(self):
        write_race(self.capture, green_caution_counters=True)
        session = prepare_session(self.capture, allow_experimental_session_mode=True)
        self.assertEqual(session.race_evidence.laps[0].exclusion_reasons,
                         ("safety-car lap", "virtual-safety-car lap"))
        self.assertFalse(session.validations[0].eligible)

    def test_malformed_packets_at_the_timed_start_are_not_ignored(self):
        write_race(self.capture, malformed_on_start=True)
        session = prepare_session(self.capture, allow_experimental_session_mode=True)
        self.assertEqual(session.validations[0].reason, "malformed race evidence during lap")
        self.assertEqual(dict(session.race_evidence.decode_errors), {2: 0, 3: 1, 10: 1})
