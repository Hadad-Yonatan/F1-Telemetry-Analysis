# Motion analysis, schema 1 / method 1.0.0

The analyzer CLI defaults to `--motion auto`. It uses the exact-frame
[composite normalizer](composite-motion-v1.md), leaving the timing and
driving-event comparison unchanged. If optional data is missing, auto
keeps the core analysis and reports missing motion values. Use
`--motion required` to require a reference timing-line anchor and shared
Motion and Motion Ex samples for each selected comparison. Use `--motion off`
to skip optional packets and artifacts. The Python `analyze_capture` API
defaults to off; pass `motion="auto"` or `motion="required"` to opt in.

An enabled bundle includes `motion.json` (all eligible laps, in lap order)
and `track-map.svg` (reference and selected comparisons). Their exact
SHA-256 digests are bound into `analysis.json`. The bundle manifest records
both files as well. The standalone HTML displays the completed data and
synchronizes the map with comparison selection, chart cursor and zoom,
keyboard navigation, and region selection. A gap is not shown as a line.

The map translates the reference timing-line point to (0, 0) and rotates
the world X/Z plane so the first valid reference forward vector points up.
If no retained sample is exactly on the line, the nearest usable preceding
and following observations can bracket it using continuous total distance.
Packets spanning a flashback or more than 10 metres cannot anchor the map.
The reference is the retained line driven on the selected lap.

Lateral deviation is measured against the horizontal reference trajectory
tangent (positive to the right of the driven direction in the rotated
coordinate system). Heading difference uses the two car forward vectors and
is wrapped to ±π radians, positive clockwise in the map plane. The output
also keeps lateral and longitudinal G, four-wheel slip ratio and angle,
and front-wheel angle. Wheel order is rear left, rear right, front left,
front right. No channel extrapolates or interpolates across a missing span
larger than 10 metres. A zero horizontal forward vector cannot supply a
heading observation, but its position and G readings remain usable.
The reference tangent and map segment breaks are based on observed motion
spacing, not on the configured display-grid spacing.
Paired distributions use only the same distances
valid on both laps; the paired difference is comparison minus reference
at each such distance.

The local library (SQLite schema 3) indexes coverage and paired summaries.
The complete traces remain in their verified bundle. Re-running
`f1-telemetry-library init` upgrades an older index transactionally.
