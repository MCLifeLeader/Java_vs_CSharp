# Work state

Updated: 2026-10-27 (America/Denver).

## Latest checkpoint: clean-start assurance

The user prioritizes a verified clean starting state over formal teardown, accepts
database/Docker effects in measured stack performance, and explicitly confirmed
Python is fine for shared support. No instruction mandated Python; it was the
implementation choice for orchestration, fixtures, independent oracles and tests.
Java/C# remain the measured workload implementations.

Clean-start recovery now uses an exclusive checkout lock, process birth identity,
repository/container labels and filesystem identities. It recovers stale leased
processes, containers and scratch; uncertain ownership blocks a new run. Active
runs, other repositories, tools, immutable inputs, traces and evidence are protected.
Controller children use Windows kill-on-close jobs or dedicated Linux sessions.
Run/verification scratch and credentials are leased. Clean builds and fresh input
generation precede execution; every I/O PREPARE stages/verifies canonical bytes.
Database startup/reset verifies the complete canonical state, reset is atomic,
and failed reset quarantines the session until recovery succeeds.

Resume fixes preserve original finalized records/runtime receipts. New attempts
archive separate receipts. Java database packaging now pins output timestamps
and its JAR plugin so clean builds can retain identical fingerprints.

Focused checks passed: 21 clean-start methods, 20 database methods, 6 worker methods,
6 JUnit methods, 30 suite-controller methods, 8 execution methods, 14 controller methods and 5 service
methods. Latest standalone PostgreSQL four-cell reset-failure/recovery smoke passed
and cleaned up all owned resources. Consolidated exact-source evidence follows
under `docs/verification/2026-10-27-clean-start/`; older evidence remains historical.

Reviewed old staging/CsvProbe leftovers were moved to ignored `.work/quarantine`.
Automatic approval review rejected recursive deletion of the old build stage
with "blocked by policy" and no more specific reason. Files were retained in
quarantine; deletion was not retried through another mechanism. Quarantine is
excluded from builds, fixtures and recovery deletion. Receipts remain in
`.work/legacy-cleanup-receipt.json` and `.work/legacy-csv-quarantine-receipt.json`.

Commands: `python benchmark/controller/benchmarkctl.py clean-start --dry-run`
(read-only), `... clean-start` (owned recovery), and
`python benchmark/validation/verify.py --java`. Full campaign authorization,
SQL Server EULA/runtime verification and SQLite native parity are unchanged.

## Resume point

Full-suite implementation is complete on `feat/benchmark-foundation`, using
.NET 10 LTS (C# 14) and Microsoft OpenJDK 25 LTS. The next required operational
work is to resolve campaign admission gaps, freeze a pilot, then run a scored
campaign. No language performance ranking has been measured. Read
`benchmark/README.md`, `benchmark/config/campaign.json` and the curated
`docs/verification/` record before resuming. That record identifies the exact
verified source commit and framework/area counts; benchmark repetitions are
separate from authored tests. Coverage is unavailable.

## User decisions and provenance

- Build the full Java/C# suite, create a branch and commit, preserve resume state,
  and use sub-agents. The original C# 15/Java 27 track is deferred by the explicit
  .NET 10 / Java LTS choice.
- On 2026-10-27, the user accepted database-engine effects and Docker overhead
  as part of the benchmark environment. Measure the observed application stack
  without estimating/subtracting those costs. Keep engine/access/container-host
  profiles, image pins, resource budgets and state/cache policies comparable
  between languages. Report measured application and database-container CPU
  separately; their sum is not complete Docker Desktop/VM/host overhead. This
  decision does not change campaign resource authorization or other admission
  gates, and does not require a bare-metal control run.
- Read all supplied PDFs: Part 1 (14 pages), Part 2 (23 pages), benchmark plan
  (21 pages). `benchmark/config/sources.json` retains hashes/page counts;
  `benchmark/specification.md` preserves the benchmark contract. Qualitative
  PDF scores remain separate from measured performance.
- Use the Edu Developer Toolbox wiki as guidance:
  https://github.com/ICS-Eng/Edu-Developer-Toolbox/wiki
  Reviewed wiki commit `4c6b7fc92550faa9fca665fe5869d6fef217fabc` and Toolbox
  source `618951fbd522f1a52ea68af130512cc5931a536a`. Benchmark infrastructure
  is disposable and repository-owned; shared Toolbox state is not reset.
- Git history: source PDFs committed as `d00692e`; GitHub Java ignore template
  merged into the existing Visual Studio ignore file in `7568074`. No push or
  PR requested. The implementation and verification commits follow those.
- Wiki updates must include exact framework/area test totals and source/evidence,
  distinguish definitions/cases/executions/expansions, and never infer coverage
  from counts. These rules are preserved in root `AGENTS.md`.

## Completed scope

- All 55 workload IDs, 180 parameter definitions, 4,750 planned full expanded
  rows. This inventory is not an executed test count.
- Both worker stacks implement 36 non-database IDs / 94 parameter variants;
  full independent CHECK outputs and repeated BATCH consumption are verified.
- ASP.NET Core / Spring MVC services implement HTTP, startup and packaged-service
  workloads. Controls support active-request accounting, quiescence, reset,
  metadata and committed-order reconciliation, including failed arrivals.
- EF Core / Hibernate and ADO.NET / JDBC paths implement D01-D08 and H06 for
  SQLite, PostgreSQL and SQL Server, with transaction/state oracles and owned
  reset/cleanup checks. Untimed AUDIT supports full typed trace validation.
- Shared deterministic fixtures include relational snapshots, JSON/CSV/protobuf,
  Unicode, compression, separate finite HTTP phase traces and common native C ABI.
- Common external timing, paired fresh processes, warmup/calibration, OS CPU/RSS,
  process limits, immutable evidence/resume, engine/native admission, complete
  weighted reporting and whole-pair bootstrap uncertainty are implemented.
- BenchmarkDotNet 0.15.8 and JMH 1.37 provide separate microbenchmark corroboration.
  Real five-launch/five-fork integration checks passed. They do not establish
  scientific admission or a cross-language ranking on their own.
- Both stacks passed owned packaging, incremental build and packaged-service
  checks, with artifact hashes, dependency/license metadata and measured
  runtime-inclusive bytes. Missing/inherited license metadata remains disclosed.
- Sub-agents owned Java/catalog, database contracts and controller/reporting;
  primary owned C#, fixtures/oracle, native ABI, setup and integration. All
  agents finished and froze their source before consolidated verification.

## Environment and reproducible commands

Windows x64; Python 3.15.1; .NET SDK 10.0.402; required .NET Core and ASP.NET Core
10.0.14; Microsoft JDK 25.0.3+9-LTS; Maven 3.9.11; Zig 0.17.0. Project-local
hash-pinned helpers live under ignored `benchmark/.work/tools`. The machine JDK
became unavailable during the session; the exact pinned Microsoft JDK was
restored locally without changing global installations. Activation only affects
the current process and children. Controller/verification select it when needed.

From the repository root:

```powershell
python benchmark/tools/bootstrap.py
. ./benchmark/tools/activate.ps1
python benchmark/native/build.py
# Build commands and locked restores are in benchmark/README.md.
python benchmark/validation/verify.py --java
python benchmark/controller/benchmarkctl.py inventory --format json
python benchmark/controller/benchmarkctl.py preflight
python benchmark/controller/benchmarkctl.py run --stage smoke --suite --case C01,K01 --campaign primitive-smoke
python benchmark/controller/benchmarkctl.py run --stage full --all --dry-run
```

Verification writes `benchmark/.work/verification.json`, with exact source hashes,
AST definitions, unittest discovery/execution, JUnit XML evidence and separate
subtest results. Curated evidence is committed under `docs/verification/`.
Standalone PostgreSQL check:
`python benchmark/validation/verify_postgresql.py`; its four-cell executions are
not added to unittest totals. Resource provisioning/cleanup is owned and guarded.

## Verified functional scope and evidence

Final dedicated database run: 18 authored methods passed (12 schema/query,
3 oracle, 3 cross-language worker), zero failed/skipped. Latest Java validation:
94 variants and 6 JUnit methods passed. HTTP: 3 authored methods passed, expanding
118 asserted requests across eight disposable server executions. The final
consolidated record is authoritative for counts and exact source identity.

All four PostgreSQL language/access cells passed against pinned PostgreSQL 18.6
Linux AMD64, including AUDIT, committed-ID chunks, rollback/stale/duplicate writes,
parallel reads, full state and reset. Owned containers/databases/leases were
cleaned up. Evidence: `.work/postgresql-controls-validation.json`, four cell
transcripts and embedded realized catalog. This is functional evidence, without
performance admission. Earlier CLI HTTP smoke passed 84 rows. A broad all-case
smoke was stopped while binaries were changing; do not report it as completed.

Raw build/harness/lifecycle logs remain ignored under `.work`. Curated verification
includes source inventories and evidence hashes. Do not publish credentials or
private connection strings. Benchmark output/checkpoints remain ignored.

## Remaining admission gaps and next action

1. SQL Server: four implementation cells compile; runtime execution remains
   unverified. EULA acceptance is false. Do not accept terms implicitly.
2. SQLite: .NET native 3.53.3 differs from Java 3.53.4 (source/options/binary
   hashes also differ). Correctness passes; primary paired results stay excluded.
3. PostgreSQL: functional cells pass; provider commands/round trips, generated SQL,
   query plans, statistics/preheat and frozen diagnostic receipts still need a
   dedicated campaign pilot. Functional smoke does not replace those receipts.
4. Configure disjoint app/DB CPUs and memory, preparation-copy headroom, HTTP
   generator capacity, stable thermal/storage conditions and all pilot settings.
   Full campaign authorization remains false in defaults. A complete campaign
   can take days; no multi-day measurement was started.
5. Dedicated native Linux language/C-library execution and macOS execution remain
   unverified. Current clients are Windows; PostgreSQL server is Linux AMD64.
6. Finish license review if distributing artifacts. Current dependency inventory
   explicitly identifies missing metadata and is not a complete formal SBOM audit.

Next: select and authorize campaign resources, resolve native/engine gaps, run a
new named pilot, inspect/freeze its lock, then execute the repeated campaign.
Resume requires matching source/config/input/binary/host fingerprints. A changed
implementation starts a new campaign. Use the report command to inspect missing
mandatory evidence; never convert partial smoke into a full performance index.
