# F1 Telemetry Analysis

Tools for capturing and analyzing F1 25 UDP telemetry without discarding the
original packet data.

## UDP connectivity probe

The first milestone is to verify that the Xbox telemetry stream reaches this
Mac. Close any other telemetry receiver first, then run:

```bash
source .venv/bin/activate
python src/f1_telemetry/probe.py
```

Drive for 15–30 seconds and stop the probe with Control+C. The final report
should show packet format `2025`, `Lap Data`, and `Car Telemetry`; `Motion` is
also preferred. The probe only counts headers and does not save packet data.

## Lossless capture

Record a short session to a timestamped file under `data/captures`:

```bash
source .venv/bin/activate
PYTHONPATH=src python -m f1_telemetry.capture
```

Stop safely with Control+C. A specific new output path can be supplied as the
first argument. Existing captures are never overwritten. The versioned F1CAP
format stores timestamps and sender metadata alongside each unchanged UDP
payload; its binary layout is documented in
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

Run the automated tests with:

```bash
PYTHONPATH=src python -m unittest discover -s tests
```
