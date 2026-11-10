# Composite Motion normalization, schema 1

The optional composite normalizer joins player Motion and Motion Ex onto
the unchanged normalized-v1 sample. The core CLI and CSV remain unchanged.
Calling `normalize_composite_capture(path)` returns the core result, a
composite sample per core row in its original order, and coverage counts.
Motion and Motion Ex can each be absent without making the core row invalid.

The EA *Data Output from F1 25*, version 3, specifies Motion packet ID 0
(1,349 bytes) and Motion Ex packet ID 13 (273 bytes), both packet version 1.
Motion uses the header's selected car out of 22; Motion Ex is already
player-only. Coordinates are world X, Y, Z in metres; direction components
divide by 32,767; the G tuple is lateral, longitudinal, vertical. Wheel
channels are rear left, rear right, front left, front right. Both decoders
reject wrong identities, sizes, and non-finite fields. They keep packed
values internally so a full session does not allocate a Python object for
every float.

Joining requires session UID, frame ID, overall frame ID, player index, and
exact binary32 session time to agree. Nonexact Lap Data / Car Telemetry
joins and duplicate complete core identities retain their v1 rows but
cannot supply motion values. A repeated copy of any previously seen
selected-player body counts as a duplicate; a new distinct alternate body
marks the group conflicted and unusable. A frame whose four
non-time identity fields agree but whose clock differs is a mismatch.
An optional identity without a corresponding four-field core identity is
an orphan. Coverage counts exact joins, blocked, conflicted, malformed,
and missing data separately for both optional streams.

Distance interpolation uses only observed channel values, does not
extrapolate, and returns null across a gap larger than ten metres.

## Locked local coverage gate

The existing Monaco F1CAP has 183,040 exact joins across all four streams,
with no optional decode failures. Run the SHA-locked gate without making
another recording:

```bash
F1_TELEMETRY_MONACO_CAPTURE=data/captures/monaco-20260803.f1cap \
  PYTHONPATH=src python -m unittest \
  tests.test_composite_motion.CompositeMotionTests.test_locked_monaco_has_183040_exact_composite_joins
```
