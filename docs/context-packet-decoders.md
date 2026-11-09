# F1 25 context packet decoders

`f1_telemetry.context_decode` provides frozen Python values from six F1 25
version 1 packets. Each decoder validates the exact packet format (2025), game
year (25), packet version (1), type, byte length, player slot (0–21), and finite
nonnegative session time before returning values. The packet lengths and field
encodings come from EA's *Data Output from F1® 25 Game*, v3.

| Packet (ID) | Bytes | Decoder | Result |
| --- | ---: | --- | --- |
| Session (1) | 753 | `decode_session_context` | Mode and session type, weather and temperatures, assists, rules, car performance, caution state and counts |
| Participants (4) | 1284 | `decode_participants` | Player identity, car/team, platform, telemetry and online-name settings, and livery colours |
| Car Setups (5) | 1133 | `decode_car_setup` | All 23 fields of the selected setup, plus the separate next front-wing value |
| Car Status (7) | 1239 | `decode_car_status` | Player fuel, assists, tyre compounds and age, DRS, engine, ERS and flag state |
| Tyre Sets (12) | 231 | `decode_tyre_sets` | The packet's explicitly addressed car and 20 tyre-set slots |
| Time Trial (14) | 101 | `decode_time_trial` | The distinct player session best, personal best, and rival data sets |

The header's player index selects rows in Participants, Car Setups, and Car
Status. Participants' active count describes the number of occupied slots, not
an upper bound on which slot may be occupied. Tyre Sets cycles across cars; use
its returned `car_index` to choose the player's snapshots. Time Trial likewise
retains each role's own car slot and the packet's validity flag. It does not
change lap eligibility.

Missing packets are handled by the caller. Within packets, blank rows and
unknown/no-value sentinels are represented as `None`. Blank Tyre Sets rows
retain their original positions; a pointer to a blank row is not evidence of a
fitted tyre. Time Trial may send such rows even for the player's car. A zero
Session gearbox-assist code, observed in captured Session packets, is also
unavailable; documented assist codes 1–3 are preserved. Session game mode 0,
also observed in captures, maps to unavailable; unlisted nonzero codes are
rejected. Car status flags
distinguish unknown (`None`) from no flag (`0`). Participant names use only the
NUL-terminated UTF-8 bytes the game sent and can be absent. Driver ID 255
identifies a network human with no mapped driver ID.

Malformed packets raise `PacketDecodeError`. Setup floats preserve the original
binary32 value in a Python float without rounding; no inferred car-specific
wing or suspension limits are applied. Values are validated where the wire
contract specifies a range; unsupported or unpublished car, team, driver and
game-mode labels are not guessed.

The core Session decoder and normalized CSV do not depend on these decoders.
Analysis uses them to attach [session context](session-context-v1.md).
