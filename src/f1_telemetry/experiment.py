"""Run a controlled setup experiment against locally verified analysis bundles."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Sequence

from f1_telemetry.experiment_metrics import evaluate_experiment
from f1_telemetry.experiment_model import ExperimentError, ExperimentManifest, load_manifest
from f1_telemetry.library import (
    DEFAULT_DATABASE, _begin_library_write, _database_path, _open_database, _verify_indexed_bundle, _writer_lock,
)
from f1_telemetry.library_experiments import EXPERIMENT_FILENAME, EXPERIMENT_REPORT_FILENAME, index_experiment


def _display(value: float | None) -> str:
    return "Unavailable" if value is None else f"{value:.6f}".rstrip("0").rstrip(".")


def render_experiment_html(document: dict) -> str:
    control = html.escape(document["control"]["label"])
    candidate = html.escape(document["candidate"]["label"])
    changes = "".join(
        f"<li>{html.escape(item['field'])}: {_display(item['control'])} → {_display(item['candidate'])}</li>"
        for item in document["setup_changes"]
    )
    rows = []
    for metric in document["metrics"]:
        label = html.escape(metric["metric"] + " (" + metric["unit"] + ")")
        values = [
            metric["control"]["count"], _display(metric["control"]["median"]), _display(metric["control"]["mad"]),
            metric["candidate"]["count"], _display(metric["candidate"]["median"]), _display(metric["candidate"]["mad"]),
            _display(metric["candidate_minus_control"]),
        ]
        cells = "".join(f"<td>{value}</td>" for value in values)
        rows.append(f'<tr><th scope="row">{label}</th>{cells}</tr>')
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Controlled setup experiment</title>
<style>
body{{max-width:74rem;margin:2rem auto;padding:0 1rem;font:1rem/1.5 system-ui,sans-serif;color:#172035;background:#f7f8fa}}
h1,h2{{line-height:1.2}}.table{{overflow:auto}}table{{border-collapse:collapse;width:100%;background:#fff}}
th,td{{padding:.5rem .65rem;text-align:right;border-bottom:1px solid #b6c0d0}}th:first-child{{text-align:left}}
caption{{font-weight:700;text-align:left;padding:.5rem 0}}thead th{{background:#dbe4f3}}a{{color:#143b71}}
</style></head><body><main>
<h1>Controlled setup experiment</h1>
<p>Control: {control} · Candidate: {candidate} · Method {document["method_version"]}</p>
<p>{html.escape(document["interpretation"])}</p>
<h2>Declared setup differences</h2><ul>{changes}</ul>
<h2>Observed measurements</h2>
<p>Differences are candidate minus control medians. At least three observed laps per group are required.</p>
<div class="table"><table><caption>Counts, medians, and unscaled median absolute deviations (MAD)</caption>
<thead><tr><th scope="col">Metric</th><th scope="col">Control n</th><th scope="col">Control median</th>
<th scope="col">Control MAD</th><th scope="col">Candidate n</th><th scope="col">Candidate median</th>
<th scope="col">Candidate MAD</th><th scope="col">Difference</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>
<p><a href="experiment.json">View raw per-lap measurements and conditions</a></p>
</main></body></html>
'''


def _source(connection: sqlite3.Connection, bundle_id: str):
    row = connection.execute("SELECT root, manifested FROM bundles WHERE bundle_id=?", (bundle_id,)).fetchone()
    if row is None:
        raise ExperimentError(f"source bundle is not indexed: {bundle_id}")
    return _verify_indexed_bundle(connection, bundle_id, *row)


def run_experiment(manifest: ExperimentManifest, database: str | Path, output_dir: str | Path) -> str:
    """Verify sources, build both artifacts, and register them as one operation."""
    output = Path(output_dir).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"output directory already exists: {output}")
    database_path = _database_path(database)
    temporary = None
    published = None
    identity = None
    try:
        with _writer_lock(database_path), closing(
            _open_database(database_path, read_only=False, allow_previous=True)
        ) as connection, connection:
            _begin_library_write(connection)
            control = _source(connection, manifest.control.bundle_id)
            candidate = control if manifest.control.bundle_id == manifest.candidate.bundle_id else _source(
                connection, manifest.candidate.bundle_id,
            )
            document = evaluate_experiment(manifest, control, candidate)
            raw = (json.dumps(document, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
            report = render_experiment_html(document).encode()
            experiment_id = hashlib.sha256(raw).hexdigest()
            output.parent.mkdir(parents=True, exist_ok=True)
            output = output.parent.resolve() / output.name
            temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
            for name, data in ((EXPERIMENT_FILENAME, raw), (EXPERIMENT_REPORT_FILENAME, report)):
                with (temporary / name).open("xb") as file:
                    file.write(data)
            index_experiment(connection, output, experiment_id, hashlib.sha256(report).hexdigest(), document)
            if output.exists() or output.is_symlink():
                raise FileExistsError(f"output directory already exists: {output}")
            stats = temporary.stat()
            identity = stats.st_dev, stats.st_ino
            published = output
            temporary.rename(output)
            temporary = None
    except BaseException:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
        if published is not None and not published.is_symlink():
            try:
                stats = published.stat()
            except FileNotFoundError:
                pass
            else:
                if (stats.st_dev, stats.st_ino) == identity:
                    # A signal can arrive immediately after SQLite commits.
                    # In that case the indexed output is already complete.
                    committed = False
                    try:
                        with closing(_open_database(database_path, read_only=True)) as check:
                            committed = check.execute(
                                "SELECT 1 FROM experiments WHERE root=? AND experiment_id=?",
                                (str(published), experiment_id),
                            ).fetchone() is not None
                    except (OSError, sqlite3.Error, ValueError):
                        pass
                    if not committed:
                        shutil.rmtree(published, ignore_errors=True)
        raise
    return experiment_id


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("manifest", type=Path)
    run.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    run.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        experiment_id = run_experiment(load_manifest(args.manifest), args.database, args.output_dir)
    except (OSError, ValueError, sqlite3.Error, OverflowError) as error:
        print(f"Experiment error: {error}", file=sys.stderr)
        return 1
    print(f"Experiment {experiment_id} written: {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
