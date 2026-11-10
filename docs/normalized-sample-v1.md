# Normalized player sample contract, version 1

One row represents the player car at one F1 25 game frame. A row exists only
when packet ID 2 (Lap Data) and packet ID 6 (Car Telemetry) share the same:

- session UID
- frame identifier
- overall frame identifier
- player car index

Including the overall frame identifier prevents pre-flashback and
post-flashback frames from being joined accidentally. Packet arrival order is
not part of the key.

The contract follows the F1 25 v3 UDP specification. Supported inputs are
packet format 2025, game year 25, packet version 1, with exact packet sizes of
1285 bytes for Lap Data and 1352 bytes for Car Telemetry.

## Columns

| Column | Type | Unit or meaning |
| --- | --- | --- |
| `captured_unix_ns` | integer | Later receive timestamp of the joined pair |
| `session_uid` | integer | Game session identifier |
| `frame_identifier` | integer | Game frame identifier; can go back after flashback |
| `overall_frame_identifier` | integer | Monotonic frame identifier across flashbacks |
| `session_time` | float | Game session timestamp, seconds |
| `player_car_index` | integer | Player entry in the 22-car arrays |
| `lap_number` | integer | Current lap number |
| `lap_distance` | float | Metres around current lap; may be negative before line |
| `total_distance` | float | Session distance in metres; may initially be negative |
| `current_lap_time_ms` | integer | Current lap elapsed time, milliseconds |
| `last_lap_time_ms` | integer | Previous lap time, milliseconds |
| `sector` | integer | `0` sector 1, `1` sector 2, `2` sector 3 |
| `speed_kph` | integer | Speed in kilometres per hour |
| `throttle` | float | `0.0..1.0` |
| `brake` | float | `0.0..1.0` |
| `steering` | float | `-1.0` full left to `1.0` full right |
| `clutch` | integer | `0..100` |
| `gear` | integer | `-1` reverse, `0` neutral, `1..8` forward |
| `engine_rpm` | integer | Engine revolutions per minute |
| `drs` | boolean | DRS off/on |
| `lap_invalid` | boolean | Current Lap Data invalid flag |
| `pit_status` | integer | `0` none, `1` pitting, `2` in pit area |
| `driver_status` | integer | Garage/flying/in/out/on-track state from the game |
| `result_status` | integer | Current result state from the game |

## Validation boundary

`lap_invalid` is a per-frame flag for the current lap. It does not prove that a
lap is complete. Completed-lap eligibility will additionally require a
non-zero official lap time, all expected sectors or equivalent structural
evidence, and the Session History validity bits. Partial and invalid laps must
not enter lap-comparison analysis.

The normalizer reports unmatched packet pairs and decode failures. It never
changes the raw capture, and a decode failure cannot remove the preserved UDP
bytes from F1CAP.

# Optional motion

The [composite Motion normalizer](composite-motion-v1.md) attaches optional
Motion and Motion Ex groups without changing any version-1 CSV field.
