# F1 Telemetry Analysis

Tools for capturing and analyzing F1 25 UDP telemetry without discarding the
original packet data.

## UDP connectivity probe

The first milestone is to verify that the Xbox telemetry stream reaches this
Mac. Close any other telemetry receiver first, then run:

```bash
source .venv/bin/activate
PYTHONPATH=src python -m f1_telemetry.probe
```

The car must already be on track. The probe waits for up to 15 seconds and
exits successfully as soon as exact F1 25 `Lap Data` and `Car Telemetry`
packets arrive; `Motion` is also preferred. It prints progress while no packets
arrive and reports a configuration checklist on failure. Use `--timeout 0` to
restore continuous listening until Control+C. The probe only counts headers
and does not save packet data.

If no data arrives, verify that UDP Telemetry is enabled in F1 25, the game's
UDP IP Address is the Mac's current LAN IPv4 address, both devices are on the
same non-guest network, the UDP port is `20777`, macOS firewall or VPN software
allows the traffic, and no other telemetry receiver is using that port.

## Lossless capture

Record a short session to a timestamped file under `data/captures`:

```bash
source .venv/bin/activate
PYTHONPATH=src python -m f1_telemetry.capture
```

The recorder runs the same 15-second connection test before creating the
output file. It buffers and then writes every preflight datagram after the test
passes, so the check does not discard received packets. A failed or interrupted
test leaves no capture file. Use `--connection-timeout 0` to disable the guard.
Stop a successful recording safely with Control+C. A specific new output path
can be supplied as the first argument. Existing captures are never overwritten.
The versioned F1CAP format stores timestamps and sender metadata alongside each
unchanged UDP payload; its binary layout is documented in
[`docs/capture-format-v1.md`](docs/capture-format-v1.md).

Verify a capture and inspect its packet counts offline:

```bash
PYTHONPATH=src python -m f1_telemetry.replay data/captures/example.f1cap
```

To send the original payloads to another local UDP listener, add
`--destination 127.0.0.1:20778`. Add `--realtime` to preserve the original
timing, or omit it to replay as quickly as possible.

## Normalize player telemetry

Validate and join Lap Data with Car Telemetry using session and frame identity:

```bash
PYTHONPATH=src python -m f1_telemetry.normalize data/captures/example.f1cap
```

Add `--output data/normalized/example.csv` to create a CSV. The columns and
their units are defined in
[`docs/normalized-sample-v1.md`](docs/normalized-sample-v1.md). Generated CSV
and capture files under `data/` are ignored by Git.

## Compare valid laps

Validate official lap history, discard incomplete and invalid laps, reconstruct
the final attempt after lap restarts, and compare every valid lap with the
fastest valid lap:

```bash
PYTHONPATH=src python -m f1_telemetry.lap_analysis data/captures/example.f1cap
```

The analyzer resamples traces on a one-metre distance grid, verifies the final
delta against the official lap-time difference, and ranks continuous time-loss
regions. The rules and thresholds are documented in
[`docs/lap-analysis-v1.md`](docs/lap-analysis-v1.md).

Create a reproducible analysis bundle in a new directory:

```bash
PYTHONPATH=src python -m f1_telemetry.lap_analysis \
  data/captures/example.f1cap --output-dir data/analysis/example
```

The bundle contains canonical session and comparison metadata in
`analysis.json`, a complete distance-aligned CSV trace for each comparison,
a matching six-panel SVG, a deterministic `report.md`, and a self-contained
interactive `report.html`. Open the HTML file directly from disk; its fonts,
styles, scripts, and rounded trace data are embedded and it makes no network
requests. Six synchronized panels support comparison selection, cursor
inspection, channel visibility, region navigation, and distance zoom. Both
reports cover every loss region, including aggregate trace evidence, matched
driving-event measurements, and explicit unmatched events.
Existing output directories are never overwritten. Use `--reference-lap` to
replace the default fastest eligible reference and repeat `--comparison-lap`
to select specific eligible laps.

The analyzer uses the bundled, versioned F1 25 corner catalog for Albert Park,
Shanghai, Sakhir, Catalunya, Monaco, Montréal, Silverstone, Spa, and Monza.
Matching regions are labeled with official turn numbers and named complexes in
console, JSON, and Markdown output. Unknown tracks retain sector and
metre-range labels. Supply `--corner-catalog PATH` to replace the bundled
catalog for one run, or `--no-corner-catalog` to disable corner association.
A known track ID with a different Session-packet track length is rejected so a
catalog cannot silently label the wrong layout. The strict catalog contract
and calibration method are documented in
[`docs/lap-analysis-v1.md`](docs/lap-analysis-v1.md#corner-catalog).
Capture identities and official map/name sources are recorded in
[`docs/corner-catalog-sources.md`](docs/corner-catalog-sources.md).

Create a dependency-free SVG comparison against the fastest eligible lap:

```bash
PYTHONPATH=src python -m f1_telemetry.plot data/captures/example.f1cap \
  --comparison-lap 6 --output data/analysis/lap-comparison.svg
```

The plot contains aligned speed, delta, throttle, brake, steering, and gear
panels. Detected time-loss regions are shaded on the delta panel.

## Validate across circuits

The tracked cross-circuit manifest locks capture identity, session invariants,
lap selection, and per-capture quality gates. Run the suite entirely offline:

```bash
PYTHONPATH=src python -m f1_telemetry.validation run \
  validation/cross-circuit-v1.json \
  --capture-dir data/captures \
  --output-dir data/validation/cross-circuit-v1
```

The command atomically writes deterministic `validation.json` and
`validation.md` files. Exit status 0 means every check passed, 1 means analysis
completed but at least one invariant or quality gate failed, and 2 means an
input or capture-identity error prevented a valid run. Existing output
directories are never overwritten.

After recording a new circuit, print a candidate case for review:

```bash
PYTHONPATH=src python -m f1_telemetry.validation baseline \
  data/captures/monza-YYYYMMDD.f1cap --case-id monza-time-trial
```

Raw captures and generated validation directories remain local and ignored.
The manifest is the only validation artifact committed to the repository.

Run the automated tests with:

```bash
PYTHONPATH=src python -m unittest discover -s tests
```
