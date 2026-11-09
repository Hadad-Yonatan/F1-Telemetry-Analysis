# Session context schema 1

Analysis collects optional F1 25 context from the existing Session and History
scan. Core Session decoding, eligibility, the selected reference, lap timing,
and comparison formulas retain their existing behavior. An optional packet
with malformed context contributes a diagnostic instead of invalidating a lap.

`analysis.json` adds `session_context` with the selected session and player,
first available session settings, one shared timeline, per-lap timeline
references, and per-packet-kind diagnostics. A missing value is JSON null.
Session UID and capture nanoseconds in this object are decimal strings for
exact representation; the existing schema-1 session metadata is unchanged.

The timeline is sorted by overall frame identifier, capture timestamp, and
packet kind. Context comes from the selected session and player. Tyre Sets
must also address the selected player. At the same overall frame and packet
kind, the latest capture timestamp wins; ties use capture record order.
Same-frame repeated observations count as duplicates and conflicting ones
as conflicts. Unchanged values across frames are stored once. Context keeps
weather, temperatures, rules, assists, and caution state; participant identity;
setup; fuel, tyre compound, and driving conditions; fitted tyre information;
and Time Trial bests. Session countdowns and live engine and ERS measurements
are not context timeline fields.

For each eligible lap, `start_indices` points to the latest snapshot at or
before the retained first timed frame, keyed by packet kind. `change_indices`
points to changes after that frame through the last retained lap frame.
`missing_at_start` lists unavailable packet kinds. Conditions are unstable if
a tracked condition changes, stable when the required starting settings,
participant, setup, and car status are present and conditions do not change,
and unknown when required context is incomplete. An absent value followed by
the first known value does not prove a change. Routine fuel consumption,
tyre wear and age, clock progression, and Time Trial bests remain visible in
the timeline without marking the lap unstable.
If retained samples do not share the selected session and player, context is
withheld for that lap and `unavailable_reason` explains why. The retained
sample selection and comparison are unaffected.

A setup fingerprint is SHA-256 of the ASCII domain `f1-25-setup-v1` followed by
a NUL byte and compact UTF-8 JSON containing all 23 player setup fields. Keys
are sorted, integers use JSON numbers, and binary32 values use the exact
Python `float.hex()` representation. The next front-wing value and live
status are excluded. Raw setup values remain available beside the fingerprint.
