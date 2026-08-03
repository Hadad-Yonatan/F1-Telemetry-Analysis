# Lap analysis methodology, version 1

## Eligibility

A lap is eligible only when both conditions hold:

1. Session History validity bits `0x01`, `0x02`, `0x04`, and `0x08` are all set.
2. Official lap time and all three sector times are non-zero, and the sector sum
   agrees with the official lap time within 5 milliseconds.

This excludes incomplete laps even when some validity bits are set, and it
excludes completed laps that the game marks invalid.

## Restart reconstruction

In Time Trial, Restart Lap can keep the same lap number and continue increasing
both frame identifiers while resetting the lap timer. Consequently, grouping
only by lap number or deduplicating frame identifiers is insufficient.

For each officially eligible lap, the analyzer finds the last transition from
a zero lap timer to a positive timer within 100 metres of the timing line. All
earlier samples under that lap number are discarded as abandoned attempts. The
remaining samples must progress monotonically in distance and lap time and
reach within 10 metres of the official track length.

## Alignment and delta

Traces are linearly interpolated onto a common 1-metre lap-distance grid.
Continuous channels are interpolated; gear uses the nearest source sample. A
zero-distance time point and an official finish-time point at the Session
packet's track length anchor every lap.

The displayed delta is comparison elapsed time minus reference elapsed time.
A positive value means the comparison lap is behind. The final calculated
delta must match the official lap-time difference; any non-zero error is
reported.

## Time-loss regions

Delta changes are evaluated in 25-metre bins. A bin is considered a loss when
delta increases by at least 0.003 seconds. Adjacent loss bins are merged, and a
continuous region is reported when its total increase is at least 0.020
seconds. Regions are ranked by delta increase.

Evidence currently includes minimum speed and mean throttle and brake within
the region. These are descriptive trace differences, not proof of causation.
Braking-point and throttle-application timing will be added as the observation
logic matures.

Evidence labels use fixed numeric rules. A region is labeled for lower minimum
speed when the comparison is more than 2 km/h lower, for lower average
throttle when it is more than 0.05 lower, and for higher average brake when it
is more than 0.05 higher. A region that crosses none of those thresholds is
labeled as a mixed trace difference.

## Driving events

Algorithm version 1.1 adds deterministic braking and corner-exit evidence.
Braking starts after brake input remains at or above 0.05 for 3 metres and ends
after it remains at or below 0.02 for 5 metres. Interruptions of at most 5
metres are merged. Events shorter than 10 metres or with peak brake below 0.20
are rejected.

For every accepted event, minimum speed is searched from brake onset until the
earlier of the next braking event or 250 metres after brake release. Throttle
reapplication is the first 10 continuous metres at or above 0.80 after minimum
speed. Coasting is measured between brake release and throttle reapplication
where throttle is at most 0.05, brake at most 0.02, and speed at least 80 km/h;
coast spans shorter than 10 metres are ignored.

Reference and comparison events are paired by the nearest unmatched
minimum-speed location within 150 metres. Loss regions use the paired event
with the greatest window overlap, with the window extended 100 metres after
throttle reapplication. A region can instead reference an unmatched event from
one lap; the analyzer records this explicitly rather than pairing unrelated
events.

Event labels require at least 5 metres of braking-point difference, 10 metres
of braking-distance difference, 10 metres of coasting difference, 5 metres of
throttle-reapplication difference, or 2 km/h of minimum-speed difference.

## Stable output bundle

With `--output-dir`, the analyzer creates a new directory containing:

- `analysis.json`, with capture identity, algorithm parameters, session and
  eligibility metadata, selected laps, sector deltas, finish verification,
  loss regions, driving events, matching state, and numeric evidence;
- `report.md`, with lap eligibility, every ranked loss region, sector and
  distance context, factual event observations, and relative links to traces
  and plots;
- one `lap-XX-vs-YY.csv` per comparison, containing the complete aligned time,
  delta, speed, throttle, brake, steering, and gear traces; and
- one matching `lap-XX-vs-YY.svg` six-panel plot per comparison.

The complete bundle is written to a temporary sibling directory and renamed
into place only after every file succeeds. Existing destinations are rejected.
Distances are serialized to three decimal places, time and continuous
telemetry values to six decimal places, and integer fields remain integers.
JSON keys are sorted, non-finite numbers are rejected, and no execution time is
included, so repeated analysis of the same capture and configuration produces
byte-identical files.

## Cross-circuit validation

`f1-telemetry-validate` applies the same algorithm to a strict, versioned
manifest of local captures. Each case freezes the capture SHA-256 and byte
size, track identity and length, datagram and normalized-sample totals,
eligible laps, fastest reference, and comparison laps. Decode errors,
unmatched lap or telemetry frames, and finish-delta errors above 1 nanosecond
are always failures.

Quality measurements are calculated independently for every capture. Matched
region coverage divides regions with a matched driving event by all loss
regions. Event-evidence coverage also counts regions containing an explicit
one-sided event. Unmatched-event rate divides all unmatched reference and
comparison events by all detected reference and comparison events across the
lap pairs. A capture with no regions receives full region coverage; a capture
with no events receives a zero unmatched-event rate.

Version 1 gates require matched-region coverage of at least 0.65,
event-evidence coverage of at least 0.80, and unmatched-event rate of at most
0.30. Diagnostic counts are reported but not frozen in the manifest. The JSON
and Markdown validation files contain no execution timestamp and are created
through a temporary sibling directory followed by an atomic rename.
