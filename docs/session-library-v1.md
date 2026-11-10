# Session library

Analysis writes `bundle.json` after the other outputs and before publishing
the bundle directory. The manifest is JSON schema 1 with a `bundle_id` and an
`artifacts` list sorted by path. Each entry contains a relative basename,
`size_bytes`, and lowercase SHA-256. The bundle ID hashes the exact bytes of
`analysis.json`; the manifest does not hash itself.

Import verifies required artifacts and matches the manifest against the
actual file set, sizes, and hashes. Symlinked artifacts, unsafe paths,
duplicates, and unexpected files are rejected. Legacy schema-1 bundles
without a manifest may lack Markdown and HTML. They must have the expected
CSV and SVG for every comparison, and each CSV must have the documented
columns, ordered finite samples, and the expected finish distance and delta.
Their current artifact hashes are saved at first import; there is no
pre-import hash evidence for legacy artifacts.

`f1-telemetry-library init` creates the current SQLite schema (4) and is
safe to repeat. It also migrates older schemas in a transaction. Migration
retains existing rows and backfills consistency or motion from verified
bundles that already registered the corresponding artifact. A failed
backfill leaves the previous schema intact. `import` also upgrades an
existing index when needed, in the same transaction as the import.
`import BUNDLE...` verifies the requested bundles and indexes them in one
transaction. A repeated identity with the same artifact hashes is a no-op;
the same identity with different artifacts is an error. The database keeps
an absolute bundle root and relative artifact names, plus indexed sessions,
captures, laps, setups, comparisons, regions, events, corner associations,
normalized consistency samples and summaries, and Motion coverage and
paired summaries.
It stores no capture or report bytes. F1 session UIDs use unsigned decimal
text, so even the largest valid UID is stored exactly.

`verify` checks SQLite integrity and foreign keys, then compares indexed
source artifacts and consistency records with the on-disk bundles.
`rebuild [BUNDLE_ROOT]` finds
bundles in deterministic path order and replaces the index only once the
temporary database is complete and verified. Hidden working directories are
skipped. Write commands cooperate through a sibling lock file. Do not edit the
database with other tools while an index command is running.

All commands accept `--database PATH` after the command name. Defaults are
`data/library/telemetry.sqlite3` and `data/analysis`. If sources move, rebuild
from their new root. If the existing database is unreadable or belongs to a
different application or schema, the CLI refuses to overwrite it. Keep a
copy if needed, then rebuild to a fresh path. Raw captures and generated
reports remain separate from the disposable library index.

## Read-only queries

`f1_telemetry.library_service.LibraryService` queries the index without
opening it for writing. `SessionFilters` combines every supplied filter
with AND. Date bounds are inclusive UTC capture dates. The team filter
uses the recorded team ID as the car identifier. Mode groups come from
the recorded F1 25 session type; unknown modes remain unavailable. A
setup filter matches if any eligible lap has that setup fingerprint.
Algorithm versions match exactly. `consistency` and `corner_consistency`
filter independently by `available`, `unavailable`, or `not-indexed`.
Older bundles without the artifact are not indexed, not unavailable.

`PageRequest` is 1-based, with 25 rows by default and at most 100.
Sessions are sorted by capture timestamp descending, then bundle ID
ascending. The service exposes session, lap, setup, corner, comparison,
region, consistency, and artifact records; it neither changes the database nor reads
or serves artifact contents.

## Local interface

Install the optional `library` extra and run `f1-telemetry-library serve`.
It defaults to `127.0.0.1:8765`; `--host` accepts only `localhost`,
`127.0.0.1`, or `::1`. The chosen database path appears on the page.
The service never opens SQLite for writing and has no write or upload routes.

The list at `/` and `GET /api/v1/sessions` (also `/api/v1/bundles`) accept
`track_id`, `created_from`, `created_through`, `mode`, `team_id`,
`setup_fingerprint`, `algorithm_version`, `consistency`, `corner_consistency`,
`page`, and `page_size`. Dates
use `YYYY-MM-DD`; repeated or unknown filter parameters are errors.
Detail paths use `/api/v1/sessions/<bundle-id>` and the `laps`,
`comparisons`, `corners`, and `artifacts` collections.
Comparison regions are under `comparisons/<lap-number>/regions`; setup
details are at `/api/v1/setups/<fingerprint>`. The `consistency` detail
under a session returns the indexed result, including raw lap and corner
samples and trend inputs; it returns 404 for a bundle with no such result.
The `motion` detail returns indexed coverage and paired motion summaries;
raw traces remain in the bundle. It returns 404 when motion was not indexed.
The `/api/v1/experiments` collection and ID detail expose read-only
[setup experiment](setup-experiments-v1.md) summaries.
JSON responses include
`schema_version: 1`; UID and capture nanoseconds remain decimal strings.

Files at `/artifacts/<bundle-id>/<name>` must match the indexed path, size,
and hash from a verified manifest. Legacy bundle metadata remains available
without HTTP file links. Symlinks are refused and a file that changed after import is
unavailable until the source is restored or the library rebuilt. Standalone
HTML reports open in a sandboxed frame without same-origin access. The history
page restricts report frame navigation to the local origin. Direct HTML and
SVG artifact requests download the files.
