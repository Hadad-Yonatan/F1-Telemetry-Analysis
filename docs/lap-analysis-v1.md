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

