import unittest
from dataclasses import replace

from f1_telemetry.consistency import Distribution, analyze_consistency
from f1_telemetry.corner_catalog import TrackDefinition, TurnDefinition
from f1_telemetry.driving_events import DrivingEvent
from f1_telemetry.lap_analysis import DEFAULT_ANALYSIS_CONFIG
from tests.test_analysis_output import make_sample, prepared_session


def event(index: int, minimum_speed_distance: float, *, complete: bool = True):
    return DrivingEvent(
        index, 0.0, 2.0, 2.0, 0.8, minimum_speed_distance, 90.0,
        9.0 if complete else None, 1.0, 10.0,
    )


class ConsistencyTests(unittest.TestCase):
    def setUp(self):
        session = prepared_session()
        self.session = replace(
            session,
            track_length=10,
            laps=tuple(
                replace(
                    trace,
                    samples=(make_sample(1, trace.lap_number, 4.5, 900, 120),),
                )
                for trace in reversed(session.laps)
            ),
        )
        self.track = TrackDefinition(
            session.track_id, "Test circuit", 10,
            (
                TurnDefinition("test-t01", 1, "<Corner>", "right", 0.0, 3.0, 5.0),
                TurnDefinition("test-t02", 2, "Second", "left", 5.0, 8.0, 10.0),
            ),
            (),
        )

    def analyze(self, track=None, events=None):
        return analyze_consistency(self.session, track, DEFAULT_ANALYSIS_CONFIG, known_events=events)

    def test_timing_uses_attempt_order_median_unscaled_mad_and_least_squares(self):
        result = self.analyze()
        self.assertEqual(result.status, "available")
        self.assertEqual([lap.lap_number for lap in result.laps], [1, 3, 5])
        self.assertEqual(result.lap_time_s, Distribution(1.5, 2.5, 2.0, 0.5))
        self.assertEqual(result.trend.inputs, ((1, 2.0), (2, 1.5), (3, 2.5)))
        self.assertAlmostEqual(result.trend.slope_s_per_attempt, 0.25)
        self.assertAlmostEqual(result.trend.intercept_s, 1.5)
        self.assertEqual(len(result.sector_times_s), 3)
        self.assertEqual(result.corner_reason, "no_catalog_track")
        self.assertEqual(result.corners, ())

    def test_two_laps_preserve_raw_timings_but_no_summary(self):
        self.session = replace(self.session, laps=self.session.laps[:2])
        result = self.analyze(self.track)
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.reason, "fewer_than_three_eligible_laps")
        self.assertEqual([lap.lap_number for lap in result.laps], [3, 5])
        self.assertIsNone(result.lap_time_s)
        self.assertIsNone(result.trend)
        self.assertEqual(result.corners, ())

    def test_exact_fractional_timing_includes_finish_and_assigns_boundary_once(self):
        result = self.analyze(self.track, {1: (event(0, 5),), 3: (event(0, 0),), 5: (event(0, 2),)})
        first, second = result.corners
        self.assertAlmostEqual(first.samples[0].duration_s, (900 + 0.5 * (2000 - 900) / 5.5) / 1000)
        self.assertAlmostEqual(first.samples[0].duration_s + second.samples[0].duration_s, 2.0)
        self.assertEqual(first.event_status, "available")
        self.assertEqual(first.event_summaries[0].distribution, Distribution(0.0, 0.0, 0.0, 0.0))
        self.assertEqual(second.event_status, "unavailable")
        self.assertTrue(all(sample.event_status == "missing" for sample in second.samples))

    def test_incomplete_ambiguous_and_missing_events_stay_raw(self):
        result = self.analyze(self.track, {
            1: (event(2, 2), event(1, 4), event(0, 0)),
            3: (event(0, 3, complete=False),),
            5: (),
        })
        corner = result.corners[0]
        self.assertEqual([sample.event_status for sample in corner.samples], ["ambiguous", "incomplete", "missing"])
        self.assertEqual(corner.samples[0].selected_event_index, 1)
        self.assertEqual(tuple(item.event_index for item in corner.samples[0].events), (0, 1, 2))
        self.assertEqual(corner.samples[1].selected_event_index, 0)
        self.assertIsNone(corner.samples[2].selected_event_index)
        self.assertEqual(corner.event_summaries, ())
        self.assertEqual(corner.event_reason, "requires_one_complete_event_per_eligible_lap")

    def test_unique_complete_candidate_counts_even_when_raw_selection_is_incomplete(self):
        result = self.analyze(self.track, {
            1: (event(0, 3, complete=False), event(1, 4)),
            3: (event(0, 3), event(1, 4, complete=False)),
            5: (event(0, 2),),
        })
        corner = result.corners[0]
        self.assertEqual(corner.event_status, "available")
        self.assertEqual([sample.selected_event_index for sample in corner.samples], [0, 0, 0])
        self.assertEqual([sample.summary_event_index for sample in corner.samples], [1, 0, 0])
        self.assertEqual(len(corner.samples[0].events), 2)
        minimum_speed = next(item for item in corner.event_summaries if item.metric == "minimum_speed_distance")
        self.assertEqual(minimum_speed.distribution, Distribution(2, 4, 3, 1))
