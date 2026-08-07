# F1 25 corner catalog sources

The bundled catalog uses F1 25 Time Trial telemetry for every committed game
distance. Published circuit lengths are not substituted for the Session packet
value. FIA maps establish turn count, numbering, and driven order; public names
are used only when an official Formula 1 or circuit-operator source supports
them.

## Batch 1 capture evidence

All captures were recorded in dry, forward-layout Time Trial sessions. F1CAP
integrity was checked by reading every framed datagram to the end of the file.
Every eligible lap is officially valid and structurally complete. Decode errors,
unmatched Lap Data frames, and unmatched Car Telemetry frames are zero for all
six captures.

| Track | Capture SHA-256 | Session UID | ID | Session length | Eligible laps | Reference | Datagrams | Maximum absolute finish-delta error |
|---|---|---:|---:|---:|---|---:|---:|---:|
| Albert Park | `28ceb8a029f00d206822490bdb52190f7bb9fa691f4da1eeddbb63d7c6041cf2` | `1693055011379803854` | 0 | 5276 m | 1, 6, 8, 9, 10, 12, 14 | 12 | 943203 | `7.327471962526033e-15` s |
| Shanghai International Circuit | `596ac1a55d9a27315d5fa642f72552bd6ae6e6b88849476b38be195ba689fb2a` | `7061360348336385546` | 2 | 5441 m | 4, 6, 7, 8 | 8 | 589197 | `1.0436096431476471e-14` s |
| Bahrain International Circuit | `27a1d93cfb81fd49130b04b104657bd25333ac6ab715169f8ea2199b496cef33` | `11601542360970387586` | 3 | 5408 m | 1, 3, 5, 6, 7 | 5 | 390950 | `2.1094237467877974e-15` s |
| Circuit de Barcelona-Catalunya | `1a255e20b0916d32ce362441c3aabd449fc07e2f88f25541f004702150558d5a` | `6734796157995414007` | 4 | 4664 m | 1, 3, 6, 9 | 9 | 570436 | `6.217248937900877e-15` s |
| Circuit Gilles-Villeneuve | `b5ab12035e9c97b587005d67b02e47a683dbd955a89071d7494c9a32bf9bb3b3` | `10059139476219167272` | 6 | 4371 m | 3, 6, 7, 8 | 7 | 350959 | `8.992806499463768e-15` s |
| Silverstone | `46b911d0295a05a3ed920e09449017b8040b4a19e3903c641280cde99e3a0bcd` | `18101233778443579667` | 7 | 5890 m | 1, 3, 4, 6 | 6 | 231887 | `1.6653345369377348e-15` s |

The local capture basenames are `melbourne-20260804.f1cap`,
`shanghai-20260805.f1cap`, `sakhir-20260808.f1cap`,
`catalunya-20260805.f1cap`, `montreal-20260808.f1cap`, and
`silverstone-20260805.f1cap`. Raw captures and calibration outputs remain
ignored and are not package inputs.

## Batch 1 calibration review

Each eligible lap was resampled at one metre. Review artifacts contain the
per-metre median speed, steering, brake, throttle, local minimum-speed, and
throttle-reapplication traces. Only the steering review trace uses the existing
11-metre centred smoothing. Motion-packet driven lines were overlaid with every
candidate apex as a second check against the official map.

Apexes are direction-consistent steering peaks. Entry and exit boundaries use
direction-consistent sustained steering for at least five metres; adjacent
turns split at the steering-direction change or the intervening steering
minimum when both turns share a direction. Every FIA-numbered turn was reviewed,
including the flat final turn at Sakhir and flat Turn 12 at Montréal.
Committed values are rounded to three decimals.

## Official maps and names

- Albert Park: [2025 FIA circuit map](https://www.fia.com/system/files/decision-document/2025_melbourne_event_-_circuit_map_-_melbourne_2025_0.pdf) and [Formula 1 Albert Park guide](https://www.formula1.com/en/page/australian-grand-prix-guide).
- Shanghai International Circuit: [2025 FIA circuit map](https://www.fia.com/system/files/decision-document/2025_chinese_grand_prix_-_event_notes_-_circuit_map_pit_lane_and_quarantine_zone.pdf) and [Formula 1 circuit page](https://www.formula1.com/en/information/china-shanghai-international-circuit-shanghai.7ESK3ZQ5DHqihx3DpPZKWL).
- Bahrain International Circuit: [2025 FIA circuit map](https://www.fia.com/system/files/decision-document/2025_bahrain_grand_prix_-_event_notes_-_circuit_map_v4.pdf) and [circuit-operator profile](https://www.bahraingp.com/about-bahrain-international-circuit/).
- Circuit de Barcelona-Catalunya: [2025 FIA circuit map](https://www.fia.com/system/files/decision-document/2025_spanish_grand_prix_-_race_director_event_notes_-_circuit_map_pit_lane_quarantine_zone_and_red_zones.pdf) and [circuit-operator profile](https://www.circuitcat.com/en/about-us/).
- Circuit Gilles-Villeneuve: [2025 FIA circuit map](https://www.fia.com/system/files/decision-document/2025_canadian_grand_prix_-_event_notes_-_circuit_map_pit_lane_emergency_exits_map_ers_battery_containment_area_red_zones.pdf) and [promoter circuit history](https://gpcanada.ca/en/about-us/the-circuit/).
- Silverstone: [2025 FIA circuit map](https://www.fia.com/system/files/decision-document/2025_british_grand_prix_-_event_notes_-_circuit_map_v2.pdf) and Formula 1's [official corner-name history](https://www.formula1.com/en/latest/article/whats-in-a-name-the-history-behind-silverstones-iconic-corners.4Q3miKVCXBkz5rKtyYnL5z).

The first five definitions use `Unnamed` because the selected official sources
do not assign authoritative public names to every numbered corner. Silverstone
uses the Formula 1 names, including the source-backed Maggotts/Becketts/Chapel
and Club complexes.
