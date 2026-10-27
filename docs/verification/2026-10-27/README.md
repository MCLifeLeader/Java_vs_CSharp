# Verification — 2026-10-27

Source: `5c1bcecca8827f9efdae9a5cf983692c5a5e4f76` on `feat/benchmark-foundation`.
Source was clean and unchanged throughout verification. The subsequent evidence
commit adds only this directory; implementation files are unchanged.

Command: `python benchmark/validation/verify.py --java` (exit 0).
Both .NET Release and Java package builds succeeded before execution. .NET build
reported zero warnings/errors. Host: Windows x64, Python 3.15.1,
.NET 10.0.14 / SDK 10.0.402, Microsoft JDK 25.0.3+9-LTS.

[Machine-readable inventory](validation.json) contains exact file hashes,
authored definitions, discovered IDs, individual results and JUnit XML hashes.
Python definitions use AST method counting; discovery/execution uses unittest.
Java definitions count `@Test` methods and cases/results come from a fresh
Maven/Surefire execution. C# production contracts are exercised through Python
integration tests; no separate xUnit/NUnit/MSTest suite is claimed.

| Framework / area | Authored definitions | Discovered cases | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: | ---: | ---: |
| unittest / analysis | 9 | 9 | 9 | 0 | 0 |
| unittest / controller | 13 | 13 | 13 | 0 | 0 |
| unittest / csv_contract | 3 | 3 | 3 | 0 | 0 |
| unittest / database_contract | 12 | 12 | 12 | 0 | 0 |
| unittest / database_oracle | 3 | 3 | 3 | 0 | 0 |
| unittest / database_workers | 3 | 3 | 3 | 0 | 0 |
| unittest / execution | 7 | 7 | 7 | 0 | 0 |
| unittest / generator | 6 | 6 | 6 | 0 | 0 |
| unittest / infrastructure | 5 | 5 | 5 | 0 | 0 |
| unittest / inventory | 18 | 18 | 18 | 0 | 0 |
| unittest / reporting | 12 | 12 | 12 | 0 | 0 |
| unittest / services | 3 | 3 | 3 | 0 | 0 |
| unittest / suite_controller | 26 | 26 | 26 | 0 | 0 |
| unittest / suite_fixtures | 7 | 7 | 7 | 0 | 0 |
| unittest / suite_http | 3 | 3 | 3 | 0 | 0 |
| unittest / suite_workers | 3 | 3 | 3 | 0 | 0 |
| unittest / toolchain | 4 | 4 | 4 | 0 | 0 |
| unittest / workers | 5 | 5 | 5 | 0 | 0 |
| **unittest total** | **142** | **142** | **142** | **0** | **0** |
| JUnit 5 / CSV contract | 6 | 6 | 6 | 0 | 0 |

Parameter expansions are separate: **333 unittest
subtest results**, all passing. They are not added to authored definitions or method
totals. HTTP requests, benchmark process repetitions, standalone scripts and
4,750 planned workload rows are also separate. No browser or snapshot totals
are claimed. **Coverage: unavailable**; no measured coverage report exists.

[Controller smoke](controller-smoke.json) ran C01 and K01 against the frozen
source: six selected language/parameter rows completed, zero failures/blocks.
This is separate from test method totals and provides no performance score.

Additional functional evidence, produced during integration before source freeze:

- [PostgreSQL](postgresql-functional.json): four language/access cells passed,
  including transaction failures, AUDIT, committed-ID reconciliation, full state,
  parallel reads and reset. Owned resources cleaned up. Original source/base
  commit, per-file hashes, input/binary hashes and raw receipt hashes are retained;
  this is a standalone script, not four extra unittest methods.
- [Native harnesses](harness-integration.json): BenchmarkDotNet five-launch and
  JMH five-fork checks passed for C01. No matched performance admission or score;
  resource budget was unset. Receipts and output hashes are retained separately.
- [Packaging/lifecycle](lifecycle-functional.json): both stacks completed package,
  one-method incremental build and packaged-service startup checks. Runtime bytes
  use disclosed boundaries; these are functional checks and inventories.

[Full dry-run](full-dry-run.json): 55 workload IDs and 4,750 planned rows,
with explicit admission blockers and no processes executed.

All 55 workload IDs are implemented. No full scored campaign has run. SQL Server
runtime remains unverified; SQLite native builds differ; PostgreSQL performance
receipts, pilot budgets and load-generator capacity still require dedicated work.
See [resume state](../../WORKLOG.md) and [suite commands](../../../benchmark/README.md).
