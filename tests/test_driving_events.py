import unittest
from dataclasses import dataclass

from f1_telemetry.driving_events import (
    DrivingEvent,
    detect_driving_events,
    match_driving_events,
    matched_event_for_region,
    unmatched_events_for_region,
)
from f1_telemetry.lap_analysis import (
    AnalysisConfig,
    ResampledLap,
    compare_laps,
)


@dataclass(frozen=True)
class Telemetry:
    distance: tuple[float, ...]
    speed_kph: tuple[float, ...]
    throttle: tuple[float, ...]
    brake: tuple[float, ...]


def telemetry(length: int = 400) -> Telemetry:
    distance = tuple(float(value) for value in range(length + 1))
    speed = [200.0] * (length + 1)
    throttle = [1.0] * (length + 1)
    brake = [0.0] * (length + 1)
    return Telemetry(distance, tuple(speed), tuple(throttle), tuple(brake))


def with_values(
    source: Telemetry,
    *,
    brake_ranges: tuple[tuple[int, int, float], ...] = (),
    throttle_ranges: tuple[tuple[int, int, float], ...] = (),
    speed_ranges: tuple[tuple[int, int, float], ...] = (),
    speed_values: tuple[tuple[int, float], ...] = (),
) -> Telemetry:
    brake = list(source.brake)
    throttle = list(source.throttle)
    speed = list(source.speed_kph)
    for start, end, value in brake_ranges:
        brake[start : end + 1] = [value] * (end - start + 1)
    for start, end, value in throttle_ranges:
        throttle[start : end + 1] = [value] * (end - start + 1)
    for start, end, value in speed_ranges:
        speed[start : end + 1] = [value] * (end - start + 1)
    for index, value in speed_values:
        speed[index] = value
    return Telemetry(
        source.distance,
        tuple(speed),
        tuple(throttle),
        tuple(brake),
    )


def event(
    index: int,
    *,
    start: float,
    end: float,
    apex: float,
    speed: float,
    throttle: float | None,
    coast: float,
    peak: float = 0.8,
) -> DrivingEvent:
    return DrivingEvent(
        event_index=index,
        braking_start_distance=start,
        braking_end_distance=end,
        braking_distance=end - start,
        peak_brake=peak,
        minimum_speed_distance=apex,
        minimum_speed_kph=speed,
        throttle_reapplication_distance=throttle,
        coast_distance=coast,
        analysis_end_distance=(throttle + 100 if throttle is not None else end + 250),
    )


class DrivingEventDetectionTests(unittest.TestCase):
    def test_extracts_braking_minimum_speed_coast_and_throttle(self) -> None:
        lap = with_values(
            telemetry(),
            brake_ranges=((100, 130, 0.8),),
            throttle_ranges=((100, 169, 0.0), (155, 160, 1.0)),
            speed_values=((150, 120.0),),
        )

        events = detect_driving_events(lap, AnalysisConfig())

        self.assertEqual(len(events), 1)
        detected = events[0]
        self.assertEqual(detected.braking_start_distance, 100.0)
        self.assertEqual(detected.braking_end_distance, 130.0)
        self.assertEqual(detected.braking_distance, 30.0)
        self.assertEqual(detected.peak_brake, 0.8)
        self.assertEqual(detected.minimum_speed_distance, 150.0)
        self.assertEqual(detected.minimum_speed_kph, 120.0)
        self.assertEqual(detected.throttle_reapplication_distance, 170.0)
        self.assertEqual(detected.coast_distance, 23.0)
        self.assertEqual(detected.analysis_end_distance, 270.0)

    def test_thresholds_reject_spikes_and_accept_boundary_values(self) -> None:
        lap = with_values(
            telemetry(150),
            brake_ranges=(
                (10, 23, 0.20),
                (70, 71, 1.0),
                (100, 112, 0.19),
            ),
        )

        events = detect_driving_events(lap, AnalysisConfig())

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].braking_start_distance, 10.0)
        self.assertEqual(events[0].peak_brake, 0.20)

    def test_hysteresis_and_gap_merging_keep_one_event(self) -> None:
        lap = with_values(
            telemetry(150),
            brake_ranges=((20, 40, 0.8), (44, 70, 0.8)),
        )
        config = AnalysisConfig(brake_off_sustain_m=1.0)

        events = detect_driving_events(lap, config)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].braking_start_distance, 20.0)
        self.assertEqual(events[0].braking_end_distance, 70.0)

    def test_missing_sustained_throttle_and_short_coast_are_recorded(self) -> None:
        lap = with_values(
            telemetry(180),
            brake_ranges=((20, 40, 0.8),),
            throttle_ranges=((20, 180, 0.0),),
            speed_ranges=((41, 180, 70.0),),
        )

        detected = detect_driving_events(lap, AnalysisConfig())[0]

        self.assertIsNone(detected.throttle_reapplication_distance)
        self.assertEqual(detected.coast_distance, 0.0)


class DrivingEventMatchingTests(unittest.TestCase):
    def test_matches_nearest_event_and_records_unmatched_events(self) -> None:
        references = (
            event(0, start=50, end=80, apex=100, speed=120, throttle=130, coast=5),
            event(1, start=250, end=280, apex=300, speed=130, throttle=330, coast=0),
        )
        comparisons = (
            event(0, start=40, end=70, apex=90, speed=118, throttle=125, coast=5),
            event(1, start=60, end=90, apex=110, speed=118, throttle=135, coast=5),
            event(2, start=600, end=630, apex=650, speed=100, throttle=680, coast=5),
        )

        result = match_driving_events(references, comparisons, AnalysisConfig())

        self.assertEqual(len(result.matches), 1)
        self.assertEqual(result.matches[0].comparison.event_index, 0)
        self.assertEqual(result.unmatched_reference_event_indices, (1,))
        self.assertEqual(result.unmatched_comparison_event_indices, (1, 2))

    def test_generates_every_directional_evidence_label(self) -> None:
        reference = (
            event(
                0,
                start=100,
                end=140,
                apex=150,
                speed=120,
                throttle=180,
                coast=10,
            ),
        )
        later = (
            event(
                0,
                start=110,
                end=170,
                apex=160,
                speed=115,
                throttle=190,
                coast=25,
            ),
        )
        earlier = (
            event(
                0,
                start=90,
                end=110,
                apex=140,
                speed=125,
                throttle=170,
                coast=0,
            ),
        )

        later_labels = match_driving_events(
            reference,
            later,
            AnalysisConfig(),
        ).matches[0].evidence_labels
        earlier_labels = match_driving_events(
            reference,
            earlier,
            AnalysisConfig(),
        ).matches[0].evidence_labels

        self.assertEqual(
            later_labels,
            (
                "braked later",
                "braked over a longer distance",
                "coasted longer",
                "full throttle occurred later",
                "lower minimum speed",
            ),
        )
        self.assertEqual(
            earlier_labels,
            (
                "braked earlier",
                "braked over a shorter distance",
                "coasted less",
                "full throttle occurred earlier",
                "higher minimum speed",
            ),
        )

    def test_handles_missing_throttle_and_below_threshold_differences(self) -> None:
        reference = (
            event(0, start=100, end=140, apex=150, speed=120, throttle=None, coast=0),
        )
        comparison = (
            event(0, start=101, end=141, apex=151, speed=121, throttle=180, coast=1),
        )

        match = match_driving_events(
            reference,
            comparison,
            AnalysisConfig(),
        ).matches[0]

        self.assertIsNone(match.differences.throttle_reapplication_distance)
        self.assertEqual(
            match.evidence_labels,
            ("event differences below thresholds",),
        )

    def test_associates_largest_overlap_and_leaves_unmatched_region_empty(self) -> None:
        reference = (
            event(0, start=100, end=140, apex=150, speed=120, throttle=180, coast=0),
            event(1, start=300, end=340, apex=350, speed=120, throttle=380, coast=0),
        )
        matches = match_driving_events(
            reference,
            reference,
            AnalysisConfig(),
        ).matches

        self.assertEqual(matched_event_for_region(120, 250, matches), 0)
        self.assertEqual(matched_event_for_region(250, 390, matches), 1)
        self.assertIsNone(matched_event_for_region(500, 550, matches))
        self.assertEqual(
            unmatched_events_for_region(120, 250, reference, (0,)),
            (0,),
        )
        self.assertEqual(
            unmatched_events_for_region(500, 550, reference, (0, 1)),
            (),
        )

    def test_assigns_loss_region_sector_from_midpoint(self) -> None:
        distance = tuple(float(value) for value in range(301))
        reference_elapsed = tuple(value / 100 for value in distance)
        comparison_elapsed = tuple(
            reference_elapsed[index] + max(0.0, (value - 200) / 1000)
            for index, value in enumerate(distance)
        )

        def lap(lap_number: int, elapsed: tuple[float, ...], time_ms: int):
            return ResampledLap(
                lap_number=lap_number,
                official_lap_time_ms=time_ms,
                distance=distance,
                elapsed_s=elapsed,
                speed_kph=(200.0,) * len(distance),
                throttle=(1.0,) * len(distance),
                brake=(0.0,) * len(distance),
                steering=(0.0,) * len(distance),
                gear=(6,) * len(distance),
            )

        result = compare_laps(
            lap(1, reference_elapsed, 3_000),
            lap(2, comparison_elapsed, 3_100),
            sector2_distance=100,
            sector3_distance=200,
        )

        self.assertTrue(result.regions)
        self.assertTrue(all(region.sector == 3 for region in result.regions))


if __name__ == "__main__":
    unittest.main()
