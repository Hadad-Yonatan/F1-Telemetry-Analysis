import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from struct import pack_into

from f1.packets import PacketCarTelemetryData, PacketMotionData, PacketMotionExData

from f1_telemetry.capture_format import CaptureWriter
from f1_telemetry.composite_motion import CompositeFrameNormalizer, normalize_composite_capture, resample_motion_channel
from f1_telemetry.decode import PacketDecodeError
from f1_telemetry.motion_decode import WHEEL_ORDER, decode_motion, decode_motion_ex
from f1_telemetry.normalize import normalize_capture, write_normalized_csv
from tests.test_decode_normalize import make_lap_packet, make_telemetry_packet, populate_header, record


def motion_packet(player=0, position=12.0, session_time=12.5, **identity):
    packet = PacketMotionData()
    populate_header(packet.header, 0, player_car_index=player, **identity)
    packet.header.session_time = session_time
    if player < 22:
        car = packet.car_motion_data[player]
        car.world_position_x = position
        car.world_position_y = -5.0
        car.world_position_z = 120.0
        car.world_forward_dir_z = 32767
        car.world_right_dir_x = -32767
        car.world_velocity_x = -32.0
        car.g_force_lateral = 2.0
        car.g_force_longitudinal = -4.0
        car.yaw = -1.25
    return packet


def motion_ex_packet(player=0, session_time=12.5, **identity):
    packet = PacketMotionExData()
    populate_header(packet.header, 13, player_car_index=player, **identity)
    packet.header.session_time = session_time
    packet.wheel_slip_ratio[:] = [-0.25, 0.5, 0.75, 1.0]
    packet.wheel_slip_angle[:] = [0.0, 0.125, 0.25, 0.375]
    packet.front_wheels_angle = -0.125
    return packet


def composite(*packets):
    joiner = CompositeFrameNormalizer()
    for timestamp, payload in enumerate(packets):
        joiner.accept(record(payload, timestamp))
    return joiner.finish()


class MotionDecoderTests(unittest.TestCase):
    def test_motion_uses_the_selected_player_and_documented_direction_scale(self):
        raw = motion_packet(player=21).pack()
        self.assertEqual(len(raw), 1349)
        decoded = decode_motion(raw)
        self.assertEqual(decoded.identity.player_car_index, 21)
        self.assertEqual(decoded.motion.position_m, (12.0, -5.0, 120.0))
        self.assertEqual(decoded.motion.velocity_mps, (-32.0, 0.0, 0.0))
        self.assertEqual(decoded.motion.forward, (0.0, 0.0, 1.0))
        self.assertEqual(decoded.motion.right, (-1.0, 0.0, 0.0))
        self.assertEqual(decoded.motion.g_force, (2.0, -4.0, 0.0))
        self.assertEqual(decoded.motion.orientation_rad, (-1.25, 0.0, 0.0))

    def test_motion_ex_uses_documented_wheel_order(self):
        raw = motion_ex_packet(player=4).pack()
        self.assertEqual(len(raw), 273)
        decoded = decode_motion_ex(raw)
        self.assertEqual(decoded.identity.player_car_index, 4)
        self.assertEqual(WHEEL_ORDER, ("rear_left", "rear_right", "front_left", "front_right"))
        self.assertEqual(decoded.motion_ex.wheel_slip_ratio, (-0.25, 0.5, 0.75, 1.0))
        self.assertEqual(decoded.motion_ex.wheel_slip_angle, (0.0, 0.125, 0.25, 0.375))
        self.assertEqual(decoded.motion_ex.front_wheels_angle_rad, -0.125)

    def test_strict_identity_size_and_finite_fields(self):
        for make, decode in ((motion_packet, decode_motion), (motion_ex_packet, decode_motion_ex)):
            for field, value in (
                ("packet_format", 2024), ("game_year", 24), ("packet_version", 0),
                ("packet_id", 3), ("player_car_index", 22), ("session_time", float("nan")),
                ("session_time", -1.0),
            ):
                with self.subTest(decoder=decode, field=field, value=value):
                    packet = make()
                    setattr(packet.header, field, value)
                    with self.assertRaises(PacketDecodeError):
                        decode(packet.pack())
            with self.assertRaises(PacketDecodeError):
                decode(make().pack()[:-1])
        packet = motion_packet(position=float("inf"))
        with self.assertRaisesRegex(PacketDecodeError, "finite"):
            decode_motion(packet.pack())
        packet = motion_packet()
        packet.car_motion_data[0].world_right_dir_x = -32768
        with self.assertRaisesRegex(PacketDecodeError, "direction"):
            decode_motion(packet.pack())
        ex = motion_ex_packet()
        ex.wheel_camber_gain[3] = float("nan")  # also validate fields not used in analysis
        with self.assertRaisesRegex(PacketDecodeError, "finite"):
            decode_motion_ex(ex.pack())


class CompositeMotionTests(unittest.TestCase):
    def test_out_of_order_join_and_normalized_csv_are_exact(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            capture = root / "sample.f1cap"
            with CaptureWriter(capture, created_unix_ns=1) as writer:
                for timestamp, raw in enumerate((
                    motion_ex_packet().pack(), make_telemetry_packet(), make_lap_packet(), motion_packet().pack(),
                    make_lap_packet(frame_identifier=9), make_telemetry_packet(frame_identifier=9),
                )):
                    writer.write_datagram(raw, ("192.0.2.10", 20_777), captured_unix_ns=timestamp + 1)
            result = normalize_composite_capture(capture)
            core = normalize_capture(capture)
            self.assertEqual(result.core, core)
            self.assertEqual(tuple(item.core for item in result.samples), core.samples)
            self.assertEqual(result.schema_version, 1)
            self.assertEqual(result.coverage.joined_both, 1)
            self.assertEqual(result.coverage.motion.missing_samples, 1)
            self.assertIsNone(result.samples[1].motion_ex)
            left, right = root / "left.csv", root / "right.csv"
            write_normalized_csv(core, left)
            write_normalized_csv(result.core, right)
            self.assertEqual(left.read_bytes(), right.read_bytes())

    def test_conflicts_missing_frames_and_orphans_never_supply_values(self):
        raw = motion_packet().pack()
        result = composite(
            make_lap_packet(), make_telemetry_packet(), raw, raw,
            motion_packet(position=22.0).pack(),
            motion_packet(frame_identifier=11).pack(),
            motion_packet(player=1).pack(),
            motion_packet(session_uid=124).pack(),
            motion_ex_packet(session_time=12.75).pack(),
            motion_ex_packet(overall_frame_identifier=21).pack(),
            motion_ex_packet().pack()[:-1],
        )
        self.assertIsNone(result.samples[0].motion)
        self.assertIsNone(result.samples[0].motion_ex)
        motion, extended = result.coverage.motion, result.coverage.motion_ex
        self.assertEqual((motion.duplicate_packets, motion.conflicting_packets, motion.conflicting_identities), (1, 1, 1))
        self.assertEqual(motion.orphan_identities, 3)
        self.assertEqual((extended.identity_mismatches, extended.orphan_identities, extended.decode_errors), (1, 1, 1))

    def test_nonexact_and_duplicate_core_joins_cannot_supply_motion(self):
        telemetry = PacketCarTelemetryData.unpack(make_telemetry_packet())
        # One binary32 ULP at 0.5 is below the v1 diagnostic tolerance.
        telemetry.header.session_time = 0.5000000596046448
        lap = bytearray(make_lap_packet())
        pack_into("<f", lap, 15, 0.5)
        result = composite(bytes(lap), telemetry.pack(), motion_packet(session_time=0.5).pack())
        self.assertEqual(result.core.stats.session_time_mismatches, 0)
        self.assertEqual(result.coverage.nonexact_core_samples, 1)
        self.assertEqual(result.coverage.motion.blocked_by_core, 1)
        self.assertIsNone(result.samples[0].motion)

        result = composite(
            make_lap_packet(), make_telemetry_packet(), make_lap_packet(), make_telemetry_packet(),
            motion_packet().pack(),
        )
        self.assertEqual(result.coverage.duplicate_core_identities, 1)
        self.assertEqual(result.coverage.motion.blocked_by_core, 1)
        self.assertEqual(tuple(sample.motion for sample in result.samples), (None, None))

    def test_repeat_of_any_conflicting_variant_is_a_duplicate(self):
        a, b, c = (motion_packet(position=value).pack() for value in (12.0, 13.0, 14.0))
        stats = []
        for packets in ((a, b, b, c), (b, b, c, a)):
            result = composite(make_lap_packet(), *packets, make_telemetry_packet())
            self.assertIsNone(result.samples[0].motion)
            stats.append(result.coverage.motion)
        self.assertEqual(stats[0], stats[1])
        self.assertEqual((stats[0].duplicate_packets, stats[0].conflicting_packets), (1, 2))

    def test_distance_interpolation_does_not_bridge_gaps_over_ten_metres(self):
        points = ((0.0, (0.0,)), (10.0, (20.0,)), (20.01, (25.0,)))
        grid = (-1.0, 0.0, 5.0, 10.0, 11.0, 20.01, 21.0)
        self.assertEqual(
            resample_motion_channel(points, grid),
            (None, (0.0,), (10.0,), (20.0,), None, (25.0,), None),
        )
        self.assertEqual(resample_motion_channel((), (0.0, 10.0)), (None, None))

    def test_unsorted_or_nonfinite_distances_cannot_extrapolate(self):
        with self.assertRaisesRegex(ValueError, "nondecreasing"):
            resample_motion_channel(((10.0, (0.0,)), (15.0, (5.0,))), (12.0, 0.0))
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            resample_motion_channel(((10.0, (0.0,)), (9.0, (5.0,))), (11.0,))
        with self.assertRaisesRegex(ValueError, "finite"):
            resample_motion_channel((), (float("nan"),))

    @unittest.skipUnless(os.getenv("F1_TELEMETRY_MONACO_CAPTURE"), "set F1_TELEMETRY_MONACO_CAPTURE for the local gate")
    def test_locked_monaco_has_183040_exact_composite_joins(self):
        capture = Path(os.environ["F1_TELEMETRY_MONACO_CAPTURE"])
        with capture.open("rb") as stream:
            self.assertEqual(
                hashlib.file_digest(stream, "sha256").hexdigest(),
                "56f3114011889dae232c18262312367e3d91a4ed750393cc6cc0166cdd89e9aa",
            )
        result = normalize_composite_capture(capture)
        self.assertEqual(result.coverage.core_samples, 183_040)
        self.assertEqual(result.coverage.joined_both, 183_040)
        self.assertEqual(result.coverage.nonexact_core_samples, 0)
        self.assertEqual(result.coverage.motion.decode_errors, 0)
        self.assertEqual(result.coverage.motion_ex.decode_errors, 0)
