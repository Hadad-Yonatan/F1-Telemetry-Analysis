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

Run the automated tests with:

```bash
PYTHONPATH=src python -m unittest discover -s tests
```
