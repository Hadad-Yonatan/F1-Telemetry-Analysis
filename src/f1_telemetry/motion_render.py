"""Pure rendering helpers for the completed Motion result."""

from __future__ import annotations

from f1_telemetry.motion_analysis import MotionResult, Point


def _path(points: tuple[Point | None, ...], breaks: tuple[bool, ...]) -> str:
    commands = []
    started = False
    for point, gap in zip(points, breaks, strict=True):
        if point is None:
            started = False
        else:
            connected = started and not gap
            commands.append(f"{'L' if connected else 'M'}{point[0]:.6f},{-point[1]:.6f}")
            started = True
    return " ".join(commands)


def track_map_svg(result: MotionResult, comparison_laps: set[int]) -> bytes:
    layers = tuple(lap for lap in result.laps if lap.lap_number == result.reference_lap or lap.lap_number in comparison_laps)
    points = tuple(point for lap in layers for point in lap.positions_m if point is not None)
    header = '<svg xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="title"'
    if not points:
        return (
            header + ' viewBox="0 0 640 100">\n'
            '<title id="title">Track map unavailable</title>\n'
            '<text x="16" y="54">No usable reference timing-line anchor.</text>\n</svg>\n'
        ).encode()
    low_x, high_x = min(point[0] for point in points), max(point[0] for point in points)
    low_y, high_y = min(-point[1] for point in points), max(-point[1] for point in points)
    margin = max(1.0, 0.04 * max(high_x - low_x, high_y - low_y))
    view_box = " ".join(f"{value:.6f}" for value in (
        low_x - margin, low_y - margin, high_x - low_x + 2 * margin, high_y - low_y + 2 * margin,
    ))
    paths = []
    for lap in sorted(layers, key=lambda lap: lap.lap_number == result.reference_lap):
        reference = lap.lap_number == result.reference_lap
        color = "#17634d" if reference else "#365ca8"
        label = f"{'Reference' if reference else 'Comparison'} lap {lap.lap_number}"
        paths.append(
            f'<path d="{_path(lap.positions_m, lap.break_before)}" fill="none" stroke="{color}" '
            f'stroke-width="{2 if reference else 1}" vector-effect="non-scaling-stroke"><title>{label}</title></path>'
        )
    return ("\n".join([
        header + f' viewBox="{view_box}">',
        f'<title id="title">Track map aligned to reference lap {result.reference_lap}</title>',
        *paths,
        f'<circle cx="0" cy="0" r="{margin / 8:.6f}" fill="#8b1b38"><title>Reference timing line</title></circle>',
        '</svg>',
    ]) + "\n").encode()
