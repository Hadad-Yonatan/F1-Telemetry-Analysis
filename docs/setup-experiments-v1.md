# Controlled setup experiments

Create a schema-1 manifest using indexed bundle IDs (the SHA-256 of each
`analysis.json`), group labels, and the eligible lap numbers to compare:

```json
{
  "schema_version": 1,
  "control": {
    "bundle_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "label": "Control",
    "laps": [3, 5, 6]
  },
  "candidate": {
    "bundle_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "label": "Candidate",
    "laps": [2, 4, 5]
  },
  "changed_setup_fields": ["front_wing"]
}
```

Replace the illustrative bundle IDs. Run:

```bash
f1-telemetry-experiment run experiment-manifest.json \
  --database data/library/telemetry.sqlite3 \
  --output-dir data/experiments/example
```

The output directory must be new. The command re-verifies both bundles,
writes `experiment.json` and a self-contained `report.html`, and
registers the result. The result ID hashes the exact JSON bytes. Repeating
the same inputs produces identical artifacts and does not duplicate the
index entry.

Each group needs at least three distinct, eligible, non-overlapping laps
with one stable setup fingerprint. Both groups must use the same in-game
track and length, validated Time Trial policy, car/team performance,
weather code, assists, and tyre compounds. Damage must be disabled and
there must be no recorded cautions. Across the selected laps, the observed
air temperature spread cannot exceed 2 °C, track temperature 3 °C, or
lap-start fuel 0.25 kg. The actual setup changes must exactly match the
declaration, and `fuel_load` cannot be declared. Missing evidence is not
proof of a match. Required setup, session, participant, and car-status
evidence must decode without conflicts or errors. The recorded running
brake bias must agree with the selected setup.

Method 1.0.0 preserves raw lap and sector times, matched catalog turn
durations and unique complete events. Each dynamics observation is the
median for a lap over distances shared by all selected laps. If both
bundles have compatible map geometry, line metrics use one common driven
reference: the lowest-numbered control lap. Maps are transformed back to
world coordinates before comparing lateral deviations. Different distance
grids or missing support produce unavailable values, never zeros.
Per-metric differences require at least three observed laps in each
group. Differences are candidate minus control medians; MAD is unscaled.
These measurements describe associations, not setup instructions.

SQLite schema 4 indexes the summaries. Browse them under **Setup
experiments** in the loopback library or at `/api/v1/experiments`.
The original JSON and HTML remain on disk. A library rebuild retains
indexed experiments if both source bundles and the unchanged experiment
files remain available.
