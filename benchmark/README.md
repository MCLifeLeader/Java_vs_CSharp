# Java and C# benchmark suite

The supplied [benchmark plan](specification.md) is implemented on **.NET 10 LTS
(C# 14) and Microsoft OpenJDK 25 LTS**. The catalog contains 55 workload IDs and
180 parameter definitions across computation, collections, allocation,
concurrency, serialization, I/O, HTTP, databases, startup/deployment, native
interop and language mechanisms. Optional tuned profiles remain separate.

Both stacks have real workload implementations, ASP.NET Core / Spring MVC
services, EF Core / Hibernate and ADO.NET / JDBC database paths. The Python
controller generates common inputs, checks independent oracles, schedules
paired processes, collects worker CPU/RSS, preserves evidence, resumes matching
campaigns and reports completeness before scoring. BenchmarkDotNet and JMH
corroborate the common externally timed microbenchmark batches.

**No performance ranking has been measured.** Smoke checks establish functional
behavior. A scored campaign requires a reviewed resource allocation, successful
pilot, frozen inputs and weights, compatible engines and admitted measurements.
The complete campaign can take several days. The original C#15/Java27 track is
deferred by the user's LTS decision.

## Setup

Run commands from the repository root. Install .NET SDK 10.0.402, Microsoft
OpenJDK 25.0.3+9-LTS and Python 3.15.1. Exact versions and compatibility gates are in
[versions.lock.json](config/versions.lock.json). The SDK pin is inherited from
[global.json](dotnet/global.json); runtime roll-forward is disabled. The app
needs both .NET Core and ASP.NET Core 10.0.14. Other Python versions are unverified.

On Windows x64, install hash-pinned Microsoft JDK, Maven 3.9.11, a project-local ASP.NET runtime
and Zig compiler, then activate the pinned project-local JDK and compile the
single native library used by both stacks:

```powershell
python benchmark/tools/bootstrap.py
. ./benchmark/tools/activate.ps1
python benchmark/native/build.py
```

Downloads stay under ignored `benchmark/.work/tools`. The activation affects
only the current shell; the controller/verification runner can select the local
JDK automatically. Bootstrap does not change machine SDK/JDK installations.
On Linux, install the locked runtimes and a C
compiler, bootstrap with `--component maven`, and run the same native build
script. Set `BENCH_CC` for a specific C compiler. Actual tool and native binary
identities are recorded.

Build the complete suite and retained protocol1 regression workers:

```powershell
Push-Location benchmark/dotnet/Suite
dotnet restore Suite.csproj --locked-mode
dotnet build Suite.csproj -c Release --no-restore
Pop-Location
& benchmark/.work/tools/apache-maven-3.9.11/bin/mvn.cmd -B -q -f benchmark/java/database/pom.xml install
& benchmark/.work/tools/apache-maven-3.9.11/bin/mvn.cmd -B -q -f benchmark/java/suite/pom.xml package
Push-Location benchmark/dotnet
dotnet restore Worker.csproj --locked-mode
dotnet build Worker.csproj -c Release --no-restore
Pop-Location
javac --release 25 -d benchmark/java/build benchmark/java/src/BenchmarkWorker.java
python benchmark/data/generate_suite.py --output benchmark/.work/suite-data/smoke --scale smoke
```

Use `mvn` on Linux or set `BENCH_MAVEN` to its executable. NuGet lock files pin
transitive packages; the Java runtime inventory records shipped JAR hashes.
Controller runs also build the complete suite automatically.

## Verify and run

```powershell
python benchmark/validation/verify.py --java
python benchmark/controller/benchmarkctl.py preflight
python benchmark/controller/benchmarkctl.py inventory --format json
python benchmark/controller/benchmarkctl.py run --stage smoke --suite --case C01,K01 --campaign primitive-smoke
python benchmark/controller/benchmarkctl.py run --stage smoke --suite --case D01,D02,D03,D04,D05,D06,D07,D08 --engine sqlite --campaign sqlite-smoke
python benchmark/controller/benchmarkctl.py run --stage smoke --all --campaign complete-smoke
python benchmark/controller/benchmarkctl.py run --stage full --all --dry-run
```

`--suite` selects protocol2 for a subset; `--all` selects it by default.
`--legacy` retains the original C01/K01 correctness protocol. Smoke uses scaled
fixtures and one repetition by default. Full inventory expansion includes five
independent paired repetitions and network database workers 1/8/32, with SQLite
at one worker. All 55 cases expand to 4,750 planned rows before pilot changes.

Complete smoke reports unavailable infrastructure explicitly and returns nonzero
for blocked or invalid rows. It does not substitute SQLite for another engine.
Checked-in worker and HTTP integration tests exercise both languages using
independent outputs and complete committed-state comparisons.

`verify.py` writes `.work/verification.json`: source commit/content inventory,
authored definitions, discovered cases, executed results by framework/area and
separate subtest expansion. Workload parameters and repeated benchmark runs are
not authored tests. Coverage is unavailable without a measured coverage report.

Pilot, full execution, report and resume use:

```powershell
python benchmark/controller/benchmarkctl.py run --stage pilot --all --campaign paired-pilot
python benchmark/controller/benchmarkctl.py run --stage full --all --campaign paired-full --pilot benchmark/results/paired-pilot/pilot-lock.json
python benchmark/controller/benchmarkctl.py report --campaign paired-full
python benchmark/controller/benchmarkctl.py run --stage smoke --all --campaign complete-smoke --resume
```

Review the pilot lock and resource configuration before freezing. Defaults do
not authorize the full resource budget. Resume requires matching source, input,
binary, host and configuration fingerprints. Changed work starts a new
campaign. Generated inputs, checkpoints and raw logs are ignored. Curate
verification under `docs/verification/` when committing evidence; keep
credentials out of public manifests and command arguments.

## Equivalence and measurement

The three [source PDFs](../docs/) were read in full; [sources.json](config/sources.json)
records their hashes. [Workload contracts](docs/workload-contracts.md) define
algorithms, encodings, checksums and preparation boundaries.
[Database contracts](docs/database-contract.md) define schema, SQL, entity state,
transactions, traces, pools and concurrency. [Parity evidence](docs/parity.md)
separates mechanisms from scientific admission.

The generator owns randomness, relational snapshots, disjoint finite phase
traces, exact JSON/CSV sizes, Unicode, protobuf and gzip vectors. Python oracles
compare complete results and committed database state. Preparation and full
semantic checks occur outside timed batches. Full batches require frozen equal
work, at least one second per batch, stable warmup, paired processes, enforced
limits and OS resource evidence. HTTP arrivals include queue delay, failures and
unsent requests; the load generator must demonstrate capacity before ranking.
Runtime-specific allocation counters remain diagnostic.

`prepare_memory_bytes` freezes the memory available for mutable input copies
outside timing (default 256 MiB); it must fit the enforced process memory limit.
The pilot must demonstrate sufficient headroom for one-second batches. HTTP
phase traces are independently sized by `http_trace_operations` and use disjoint
order IDs, so a load pilot cannot silently recycle a mutation trace.

The score model preserves domain, engine, access and metric weights, missing
mandatory evidence, whole-pair bootstrap uncertainty and sensitivity views.
Partial smoke cannot produce a full index. Qualitative PDF assessments stay
separate from measured scores.

## Infrastructure and current gates

Follow [infrastructure setup](infrastructure/README.md) for SQL Server and
PostgreSQL. Image manifests are immutable. Provisioning uses random owned
databases, loopback ports, disjoint budgets, labels and ownership leases; reset
and cleanup reject unowned targets. SQL Server requires recorded operator EULA
acceptance. No shared Toolbox database is reset.

Four SQLite language/access cells pass semantic validation. Pinned native
versions differ (.NET 3.53.3; Java 3.53.4), so the primary SQLite aggregate remains
excluded until engine identity aligns. All four PostgreSQL language/access cells
passed live contracts against the pinned disposable PostgreSQL 18.6 image.
Run `python benchmark/validation/verify_postgresql.py` for that standalone
integration check; its cell executions are separate from unittest totals.
The four SQL Server cells compile but remain unverified. No SQL Server terms
have been accepted in this session. A dedicated Linux scored campaign has not
been run.

Deployment accounting records artifact hashes, package license metadata and
measured runtime-inclusive bytes. Boundaries are disclosed: required
.NET Core/ASP.NET Core versus the untrimmed installed JDK, including tools. Trimmed
images and AOT are separate profiles. Missing or inherited license metadata
remains a review gap.

Read [WORKLOG.md](../docs/WORKLOG.md) to resume. It records the branch, commits,
verification and next action. Follow the
[Toolbox wiki](https://github.com/ICS-Eng/Edu-Developer-Toolbox/wiki) as development
guidance; keep benchmark infrastructure disposable and repository-owned.
