# Session library schema 1

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

`f1-telemetry-library init` creates schema 1 and is safe to repeat.
`import BUNDLE...` verifies the requested bundles and indexes them in one
transaction. A repeated identity with the same artifact hashes is a no-op;
the same identity with different artifacts is an error. The database keeps
an absolute bundle root and relative artifact names, plus indexed sessions,
captures, laps, setups, comparisons, regions, events, and corner associations.
It stores no capture or report bytes. F1 session UIDs use unsigned decimal
text, so even the largest valid UID is stored exactly.

`verify` checks SQLite integrity and foreign keys, then compares indexed
source artifacts with the on-disk bundles. `rebuild [BUNDLE_ROOT]` finds
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
Algorithm versions match exactly.

`PageRequest` is 1-based, with 25 rows by default and at most 100.
Sessions are sorted by capture timestamp descending, then bundle ID
ascending. The service exposes session, lap, setup, corner, comparison,
region, and artifact records; it neither changes the database nor reads
or serves artifact contents.
