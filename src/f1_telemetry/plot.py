"""Dependency-free SVG plots for distance-aligned lap comparisons."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from f1_telemetry.lap_analysis import (
    LapComparison,
    PreparedSession,
    analyze_capture,
)

WIDTH = 1400
HEIGHT = 1180
LEFT = 90
RIGHT = 30
TOP = 80
PANEL_HEIGHT = 150
PANEL_GAP = 25
PLOT_WIDTH = WIDTH - LEFT - RIGHT


@dataclass(frozen=True)
class Panel:
    title: str
    minimum: float
    maximum: float
    reference: tuple[float, ...] | None
    comparison: tuple[float, ...]
    unit: str = ""


def _lap_time(milliseconds: int) -> str:
    minutes, remainder = divmod(milliseconds, 60_000)
    return f"{minutes}:{remainder / 1000:06.3f}"


def _points(
    distance: tuple[float, ...],
    values: tuple[float, ...],
    track_length: int,
    top: float,
    minimum: float,
    maximum: float,
) -> str:
    scale = maximum - minimum or 1.0
    stride = max(1, len(distance) // 1800)
    indexes = list(range(0, len(distance), stride))
    if indexes[-1] != len(distance) - 1:
        indexes.append(len(distance) - 1)
    return " ".join(
        f"{LEFT + distance[index] / track_length * PLOT_WIDTH:.1f},"
        f"{top + PANEL_HEIGHT - (values[index] - minimum) / scale * PANEL_HEIGHT:.1f}"
        for index in indexes
    )


def write_comparison_svg(
    session: PreparedSession,
    result: LapComparison,
    output: Path,
) -> None:
    """Write aligned speed, input, gear, and delta panels to a new SVG."""
    reference = result.reference
    comparison = result.comparison
    delta = result.delta_s
    delta_padding = max(0.05, (max(delta) - min(delta)) * 0.08)
    panels = (
        Panel(
            "Speed",
            0,
            max(max(reference.speed_kph), max(comparison.speed_kph)) + 10,
            reference.speed_kph,
            comparison.speed_kph,
            "km/h",
        ),
        Panel(
            "Delta (comparison - reference)",
            min(delta) - delta_padding,
            max(delta) + delta_padding,
            None,
            delta,
            "s",
        ),
        Panel("Throttle", 0, 1, reference.throttle, comparison.throttle),
        Panel("Brake", 0, 1, reference.brake, comparison.brake),
        Panel("Steering", -1, 1, reference.steering, comparison.steering),
        Panel(
            "Gear",
            -1,
            8,
            tuple(float(value) for value in reference.gear),
            tuple(float(value) for value in comparison.gear),
        ),
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as file:
        write: Callable[[str], int] = file.write
        write(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
            f'height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">\n'
        )
        write(
            "<style>"
            "text{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',"
            "sans-serif;fill:#202124}"
            ".title{font-size:22px;font-weight:600}"
            ".label{font-size:14px}.small{font-size:12px;fill:#5f6368}"
            ".grid{stroke:#d8dce2;stroke-width:1}"
            ".axis{stroke:#80868b;stroke-width:1}"
            ".ref{fill:none;stroke:#1677b8;stroke-width:2}"
            ".cmp{fill:none;stroke:#d65f00;stroke-width:2}"
            ".delta{fill:none;stroke:#7b3fb2;stroke-width:2}"
            ".loss{fill:#d93025;fill-opacity:.08}"
            "</style>\n"
        )
        write('<rect width="100%" height="100%" fill="#fff"/>\n')
        write(
            f'<text x="{LEFT}" y="32" class="title">'
            f'Lap {reference.lap_number} vs lap {comparison.lap_number}</text>'
            f'<text x="{LEFT}" y="57" class="label">'
            f'Reference {_lap_time(reference.official_lap_time_ms)} · '
            f'Comparison {_lap_time(comparison.official_lap_time_ms)} · '
            f'Delta {result.official_delta_s:+.3f} s</text>\n'
        )
        write(
            f'<line x1="{WIDTH - 360}" y1="29" x2="{WIDTH - 320}" '
            f'y2="29" class="ref"/><text x="{WIDTH - 310}" y="34" '
            f'class="label">Lap {reference.lap_number}</text>'
        )
        write(
            f'<line x1="{WIDTH - 190}" y1="29" x2="{WIDTH - 150}" '
            f'y2="29" class="cmp"/><text x="{WIDTH - 140}" y="34" '
            f'class="label">Lap {comparison.lap_number}</text>\n'
        )

        for panel_index, panel in enumerate(panels):
            panel_top = TOP + panel_index * (PANEL_HEIGHT + PANEL_GAP)
            write(
                f'<text x="{LEFT}" y="{panel_top - 9}" class="label">'
                f'{panel.title}</text>\n'
            )
            for fraction in (0, 0.5, 1):
                y = panel_top + PANEL_HEIGHT * (1 - fraction)
                value = panel.minimum + fraction * (panel.maximum - panel.minimum)
                write(
                    f'<line x1="{LEFT}" y1="{y:.1f}" '
                    f'x2="{WIDTH - RIGHT}" y2="{y:.1f}" class="grid"/>'
                )
                write(
                    f'<text x="{LEFT - 8}" y="{y + 4:.1f}" '
                    f'text-anchor="end" class="small">'
                    f'{value:.1f}{panel.unit}</text>\n'
                )
            for sector_distance in (session.sector2_distance, session.sector3_distance):
                x = LEFT + sector_distance / session.track_length * PLOT_WIDTH
                write(
                    f'<line x1="{x:.1f}" y1="{panel_top}" x2="{x:.1f}" '
                    f'y2="{panel_top + PANEL_HEIGHT}" class="grid"/>\n'
                )
            if panel_index == 1:
                for region in result.regions:
                    x = LEFT + region.start_distance / session.track_length * PLOT_WIDTH
                    width = (
                        (region.end_distance - region.start_distance)
                        / session.track_length
                        * PLOT_WIDTH
                    )
                    write(
                        f'<rect x="{x:.1f}" y="{panel_top}" '
                        f'width="{width:.1f}" height="{PANEL_HEIGHT}" '
                        'class="loss"/>\n'
                    )
            if panel.reference is not None:
                points = _points(
                    reference.distance,
                    panel.reference,
                    session.track_length,
                    panel_top,
                    panel.minimum,
                    panel.maximum,
                )
                write(f'<polyline points="{points}" class="ref"/>\n')
            points = _points(
                comparison.distance,
                panel.comparison,
                session.track_length,
                panel_top,
                panel.minimum,
                panel.maximum,
            )
            css_class = "delta" if panel_index == 1 else "cmp"
            write(f'<polyline points="{points}" class="{css_class}"/>\n')

        axis_y = TOP + len(panels) * (PANEL_HEIGHT + PANEL_GAP) - PANEL_GAP
        for distance in range(0, session.track_length + 1, 1000):
            x = LEFT + distance / session.track_length * PLOT_WIDTH
            write(
                f'<line x1="{x:.1f}" y1="{axis_y}" x2="{x:.1f}" '
                f'y2="{axis_y + 6}" class="axis"/>'
            )
            write(
                f'<text x="{x:.1f}" y="{axis_y + 22}" '
                f'text-anchor="middle" class="small">{distance}</text>\n'
            )
        write(
            f'<text x="{LEFT + PLOT_WIDTH / 2:.1f}" y="{axis_y + 43}" '
            'text-anchor="middle" class="label">Lap distance (m)</text>\n'
        )
        write("</svg>\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create an SVG lap comparison plot.")
    parser.add_argument("capture", type=Path)
    parser.add_argument("--comparison-lap", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        analysis = analyze_capture(
            args.capture,
            comparison_laps=(args.comparison_lap,),
        )
        write_comparison_svg(
            analysis.session,
            analysis.comparisons[0],
            args.output,
        )
        print(f"Comparison SVG written: {args.output}")
    except (OSError, ValueError) as error:
        raise SystemExit(f"Could not create plot: {error}") from error


if __name__ == "__main__":
    main()
