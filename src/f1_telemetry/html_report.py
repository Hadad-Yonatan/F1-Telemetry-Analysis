"""Deterministic, self-contained HTML rendering for analysis results."""

from __future__ import annotations

import base64
import html
import json
from importlib import resources
from pathlib import Path

from f1_telemetry.corner_catalog import format_corner_label
from f1_telemetry.lap_analysis import AnalysisResult, LapComparison, ResampledLap
from f1_telemetry.report import comparison_stem

REPORT_VERSION = 1
_ASSET_DIRECTORY = "html_assets"


def _asset_bytes(name: str) -> bytes:
    return (
        resources.files("f1_telemetry")
        .joinpath(_ASSET_DIRECTORY, name)
        .read_bytes()
    )


def _asset_text(name: str) -> str:
    return _asset_bytes(name).decode("utf-8")


def _rounded(values: tuple[float, ...], digits: int) -> list[float]:
    return [round(value, digits) for value in values]


def _lap_trace(lap: ResampledLap) -> dict[str, object]:
    return {
        "lap_number": lap.lap_number,
        "elapsed_s": _rounded(lap.elapsed_s, 6),
        "speed_kph": _rounded(lap.speed_kph, 6),
        "throttle": _rounded(lap.throttle, 6),
        "brake": _rounded(lap.brake, 6),
        "steering": _rounded(lap.steering, 6),
        "gear": [int(value) for value in lap.gear],
    }


def _comparison_trace(comparison: LapComparison) -> dict[str, object]:
    trace = _lap_trace(comparison.comparison)
    trace["delta_s"] = _rounded(comparison.delta_s, 6)
    return trace


def _region_label(result: AnalysisResult, comparison_index: int, region_index: int) -> str:
    region = result.comparisons[comparison_index].regions[region_index]
    track = result.catalog_selection.track
    if track is not None and region.corner_context is not None:
        return format_corner_label(track, region.corner_context)
    return (
        f"Sector {region.sector}, "
        f"{region.start_distance:.3f}\u2013{region.end_distance:.3f} m"
    )


def _report_payload(
    result: AnalysisResult,
    document: dict[str, object],
) -> dict[str, object]:
    if not result.comparisons:
        raise ValueError("HTML report requires at least one comparison")
    reference = result.comparisons[0].reference
    for comparison in result.comparisons:
        if comparison.reference != reference:
            raise ValueError("HTML report comparisons must share one reference trace")
        if comparison.comparison.distance != reference.distance:
            raise ValueError("HTML report traces must share one distance grid")

    initial = min(
        result.comparisons,
        key=lambda comparison: (
            abs(comparison.official_delta_s),
            comparison.comparison_lap,
        ),
    )
    presentation = {
        "initial_comparison_lap": initial.comparison_lap,
        "session_uid_label": str(result.session.session_uid),
        "comparisons": [
            {
                "comparison_lap": comparison.comparison_lap,
                "artifact_links": {
                    "csv": f"{comparison_stem(comparison)}.csv",
                    "svg": f"{comparison_stem(comparison)}.svg",
                },
                "region_labels": [
                    _region_label(result, comparison_index, region_index)
                    for region_index in range(len(comparison.regions))
                ],
            }
            for comparison_index, comparison in enumerate(result.comparisons)
        ],
    }
    return {
        "report_version": REPORT_VERSION,
        "analysis": document,
        "presentation": presentation,
        "traces": {
            "distance_m": _rounded(reference.distance, 3),
            "reference": _lap_trace(reference),
            "comparisons": [
                _comparison_trace(comparison) for comparison in result.comparisons
            ],
        },
    }


def _safe_json(document: dict[str, object]) -> str:
    serialized = json.dumps(
        document,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        serialized.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_html_report(
    result: AnalysisResult,
    document: dict[str, object],
) -> str:
    """Render an offline report from a completed result and canonical document."""
    payload = _safe_json(_report_payload(result, document))
    css = _asset_text("report.css")
    javascript = _asset_text("report.js")
    regular = base64.b64encode(
        _asset_bytes("AtkinsonHyperlegible-Regular.woff2")
    ).decode("ascii")
    bold = base64.b64encode(
        _asset_bytes("AtkinsonHyperlegible-Bold.woff2")
    ).decode("ascii")
    title = (
        html.escape(result.capture.basename, quote=True)
        .replace("\u2028", "&#8232;")
        .replace("\u2029", "&#8233;")
    )
    panels = "\n".join(
        f'''          <section class="chart-panel" data-channel="{channel}">
            <h3>{label}</h3>
            <svg class="telemetry-chart" data-channel="{channel}" role="img" aria-label="{label} telemetry chart" viewBox="0 0 900 150" preserveAspectRatio="none"></svg>
          </section>'''
        for channel, label in (
            ("speed_kph", "Speed"),
            ("delta_s", "Delta"),
            ("throttle", "Throttle"),
            ("brake", "Brake"),
            ("steering", "Steering"),
            ("gear", "Gear"),
        )
    )
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; connect-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; font-src data:; img-src data:; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'">
  <title>Telemetry report — {title}</title>
  <style>
@font-face {{font-family:"Atkinson Hyperlegible";font-style:normal;font-weight:400;font-display:block;src:url(data:font/woff2;base64,{regular}) format("woff2")}}
@font-face {{font-family:"Atkinson Hyperlegible";font-style:normal;font-weight:700;font-display:block;src:url(data:font/woff2;base64,{bold}) format("woff2")}}
{css}
  </style>
</head>
<body>
  <header class="page-header">
    <div>
      <p class="eyebrow">F1 25 telemetry</p>
      <h1>Driver analysis</h1>
      <p id="capture-name" class="capture-name"></p>
    </div>
    <nav aria-label="Report files">
      <a href="analysis.json">Analysis JSON</a>
      <a href="report.md">Markdown report</a>
      <a id="csv-link" href="">Selected CSV</a>
      <a id="svg-link" href="">Selected SVG</a>
    </nav>
  </header>

  <main>
    <section class="summary-card" aria-labelledby="session-heading">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Session</p>
          <h2 id="session-heading">Capture and processing</h2>
        </div>
        <label class="comparison-picker">Comparison
          <select id="comparison-select" aria-label="Comparison lap"></select>
        </label>
      </div>
      <dl id="session-summary" class="summary-grid"></dl>
      <div class="table-scroll">
        <table>
          <caption>Lap eligibility</caption>
          <thead><tr><th scope="col">Lap</th><th scope="col">Official</th><th scope="col">S1</th><th scope="col">S2</th><th scope="col">S3</th><th scope="col">Status</th></tr></thead>
          <tbody id="eligibility-body"></tbody>
        </table>
      </div>
    </section>

    <section class="comparison-summary" aria-labelledby="comparison-heading">
      <div>
        <p class="eyebrow">Selected comparison</p>
        <h2 id="comparison-heading"></h2>
      </div>
      <dl id="delta-summary" class="delta-grid"></dl>
    </section>

    <div class="analysis-layout">
      <aside class="regions-card" aria-labelledby="regions-heading">
        <div class="section-heading compact">
          <div><p class="eyebrow">Ranked differences</p><h2 id="regions-heading">Loss regions</h2></div>
        </div>
        <div id="regions" class="region-list"></div>
      </aside>

      <section class="charts-card" aria-labelledby="charts-heading">
        <div class="section-heading chart-heading">
          <div><p class="eyebrow">Distance aligned</p><h2 id="charts-heading">Telemetry traces</h2></div>
          <button id="reset-zoom" type="button">Reset view</button>
        </div>
        <fieldset class="channel-controls">
          <legend>Visible channels</legend>
          <label><input type="checkbox" value="speed_kph" checked> Speed</label>
          <label><input type="checkbox" value="delta_s" checked> Delta</label>
          <label><input type="checkbox" value="throttle" checked> Throttle</label>
          <label><input type="checkbox" value="brake" checked> Brake</label>
          <label><input type="checkbox" value="steering" checked> Steering</label>
          <label><input type="checkbox" value="gear" checked> Gear</label>
        </fieldset>
        <div id="chart-stack" class="chart-stack" tabindex="0" aria-label="Synchronized telemetry charts. Use arrow keys to move the cursor and Escape to reset zoom.">
{panels}
        </div>
        <div class="cursor-card" aria-live="polite">
          <p id="cursor-distance" class="cursor-distance"></p>
          <div class="table-scroll">
            <table>
              <thead><tr><th scope="col">Channel</th><th scope="col">Reference</th><th scope="col">Comparison</th><th scope="col">Delta</th></tr></thead>
              <tbody id="cursor-values"></tbody>
            </table>
          </div>
        </div>
      </section>
    </div>
  </main>
  <footer>Report format 1 · Analysis schema {result.config.schema_version} · Algorithm {html.escape(result.config.algorithm_version)}</footer>
  <script id="report-data" type="application/json">{payload}</script>
  <script>{javascript}</script>
</body>
</html>
'''


def write_html_report(
    result: AnalysisResult,
    document: dict[str, object],
    output: Path,
) -> None:
    """Write a deterministic HTML report without overwriting."""
    with output.open("x", encoding="utf-8", newline="\n") as file:
        file.write(render_html_report(result, document))
