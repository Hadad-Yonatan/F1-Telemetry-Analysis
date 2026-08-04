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
Braking-point and throttle-application timing are supplied by the separate
fixed driving-event rules below.

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

## Corner catalog

The bundled `corner-catalog-v1.json` is a strict, immutable F1 25 catalog for
Monaco, Spa-Francorchamps, and Monza. Its root records catalog schema version
1, catalog ID `f1-25-corners-v1`, packet format 2025, game year 25, and ordered
track definitions. The exact bundled bytes currently have SHA-256
`8d402be1e5eb28290b09f40fb3122d8e1c5de2c948b974ef31ac10bf01a7cdf1`.
Every saved analysis also records this digest, the selection status, and the
complete selected track definition so a result never depends on a later
installed catalog.

Each track definition contains its exact in-game track ID and Session-packet
length. Each ordered turn contains a stable lowercase ASCII ID, official turn
number, display name, left/right direction, and entry, apex, and exit distance.
Optional complexes contain ordered consecutive turn IDs. Catalog documents
reject unknown or missing fields, duplicate identifiers or turn numbers,
non-finite values, overlapping turn windows, invalid complex membership, and
the wrong packet format or game year.

Selection uses the exact track ID. A missing track continues without corner
labels. A matching ID whose catalog length differs from the Session packet is
fatal because it may represent a different layout. `--corner-catalog PATH`
completely replaces the bundled catalog for one run; `--no-corner-catalog`
records an explicit disabled selection.

A loss region overlaps a turn only when their shared distance is greater than
zero. All overlaps are recorded in catalog order. The primary turn has the
greatest overlap, with earlier entry distance and then stable turn ID resolving
ties. Every complex containing an overlapping turn is recorded. When a region
spans two or more turns in one complex, reports use the complex name and turn
range; otherwise they use the primary turn number and name. Regions with no
overlap retain the existing sector and metre-range description.

### Calibration methodology

The three bundled definitions were calibrated against the locked dry Time
Trial captures used by the cross-circuit validation suite. All eligible laps
were resampled at one metre. A local review trace used the per-metre median of
steering, speed, braking ranges, minimum-speed locations, and sustained
throttle reapplication. Only the calibration steering trace was smoothed, with
an 11-metre centered mean; analysis inputs and formulas were not changed.

Official turn order, numbering, direction, and names were reviewed against the
[Formula 1 Spa corner guide](https://www.formula1.com/en/latest/article/explained-how-every-corner-at-the-circuit-de-spa-francorchamps-got-its-name.1o6cyXf5F6q9VFFcgMqTqs.1o6cyXf5F6q9VFFcgMqTqs),
[Monza circuit guide](https://www.monzanet.it/en/circuit/),
[FIA 2025 Monaco circuit map](https://www.fia.com/system/files/decision-document/2025_monaco_event_-_circuit_map_-_monaco_2025_revised.pdf),
and [Formula 1 Monaco corner guide](https://www.formula1.com/en/latest/article/explained-how-every-monaco-corner-got-its-name.5ClYqWmfpeUpsRJA6mDNaX.5ClYqWmfpeUpsRJA6mDNaX).
Apexes use reviewed direction-consistent steering peaks. Entry and exit use
sustained steering transitions around the apex, with at least five continuous
metres reviewed; adjacent chicane turns split at the steering direction change.
All eligible laps were checked to avoid fitting the definition to the selected
reference lap. Committed distances are rounded to three decimals, ordered,
non-overlapping, and bounded by the locked in-game lengths: Monaco 3323 m, Spa
7007 m, and Monza 5798 m.

## Stable output bundle

With `--output-dir`, the analyzer creates a new directory containing:

- `analysis.json`, with capture identity, algorithm parameters, session and
  eligibility metadata, selected laps, sector deltas, finish verification,
  loss regions, driving events, matching state, numeric evidence, catalog
  identity, the selected track definition, and per-region corner IDs;
- `report.md`, with lap eligibility, every ranked loss region, sector and
  distance and corner context, factual event observations, and relative links
  to traces and plots;
- `report.html`, a self-contained interactive view of the same completed
  result with comparison selection, synchronized speed/delta/input/gear
  panels, cursor values, channel controls, and region-based zoom;
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

The HTML renderer receives the same canonical analysis document written to
`analysis.json`; presentation code does not derive sectors, regions, events,
evidence, or telemetry deltas. CSS, JavaScript, the Atkinson Hyperlegible
regular and bold fonts, and the rounded traces are embedded in the file. A
restrictive content-security policy prevents connections and external
resources, so the report opens directly with a local-file URL. The bundled
font files are redistributed under the SIL Open Font License 1.1, included
with the package as `AtkinsonHyperlegible-OFL.txt`. The typeface is published
by the [Braille Institute](https://www.brailleinstitute.org/freefont/).

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
