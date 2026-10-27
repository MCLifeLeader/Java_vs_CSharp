# Shared database contract foundation

Status: DDL, raw SQL and real provider implementations exist for SQL Server,
PostgreSQL and SQLite in both .NET and Java, in raw and ORM modes. The four SQLite
cells have passed the independent dictionary-oracle integration check through
D01–D08, checkout, conflicts, rollback and reset. All four PostgreSQL18.6 cells
also passed those contracts against the pinned Linux AMD64 image, including an
eight-worker read batch and full-state reset. SQL Server paths compile but have
not been executed against a live server, so those four cells remain unverified.
No result is admitted for scoring. The stable SQLite native builds differ:
.NET embeds SQLite 3.53.3 and Java Xerial embeds 3.53.4, with separately recorded
source IDs, compile options and library hashes. Correctness smoke does not
resolve this native-build parity gap.

The [benchmark specification](../specification.md), sections 5–8, supplies the
logical contract. These files refine the unresolved naming and binding details.
Future workers must read the shared SQL; independent SQL copies are not allowed.

## Provider modules and active runtimes

The user selected .NET 10 LTS and Java 25 LTS. The .NET module lives in
`dotnet/Database/`, exposes `BenchmarkDatabase.DatabaseSession`, and uses EF Core
SQL Server/SQLite 10.0.14, Npgsql EF 10.0.3, Microsoft.Data.SqlClient 7.1.1,
System.Security.Cryptography.Pkcs 10.0.14 and SQLitePCLRaw bundle 2.1.13. The
explicit SqlClient/PKCS pins avoid vulnerable transitive PKCS 9.0.11 found during
the first restore; no audit warning was suppressed. Exact transitive resolution
and package content hashes are preserved in its `packages.lock.json`.

The Java module in `java/database/` exposes
`benchmark.database.DatabaseSession`, uses Hibernate ORM/community dialect
7.4.14.Final with Jakarta Persistence 3.2, HikariCP 7.1.0, Xerial 3.53.4.0,
PostgreSQL JDBC 42.7.14 and Microsoft SQL Server JDBC 13.6.1.jre11. Hibernate
7.4 explicitly lists Java 25 compatibility on its
[release page](https://hibernate.org/orm/releases/7.4/).
EF providers must match the EF major version;
[Microsoft's provider guidance](https://learn.microsoft.com/en-us/ef/core/providers/)
and [Npgsql EF 10 release notes](https://www.npgsql.org/efcore/release-notes/10.0.html)
were checked before selecting these pins. All moving references were reviewed
on 2026-10-27; compilation and functional results are the evidence for the
implemented SQLite paths.

Both modules initialize mappings/factories outside timed execution, prewarm the
same 16 network connections (one SQLite worker connection), and create a fresh
tracked context/session for each logical ORM operation. Raw mode uses real
ADO.NET/JDBC. SQL Server/PostgreSQL have 16-connection provider/Hikari pools with
explicit 15-second acquisition/command timeouts. SQLite holds one process-owned
anchor plus one leased worker connection. There is no cross-process in-memory
SQLite connection. SQLite calls are synchronous on both sides. Context pooling,
lazy loads, second-level/query result caches, optional auto-prepare and command
batching are disabled.

The ORM D04 paths use LINQ/HQL projection, D03/D07 explicitly fetch customer and
lines in one query, D05 uses a version-qualified read/modify/write transaction,
and D06/D07 perform six logical modifications without batching. The Java ORM
maps numeric `@Version`; EF's numeric version is incremented in application
code. Optional `expected_version` inputs simulate rejected stale client requests;
Java rejects a known mismatch before issuing writes. These diagnostics must not
be substituted for a matched intentional-contention measurement profile.

## Schema and values

All engines use the logical names `customer`, `product`, `orders`, `order_line`
and lower snake case columns. IDs are positive, client-assigned signed int64;
versions start at zero and increment in application code. Cents, timestamps,
versions, status, quantities and line numbers bind explicitly as signed int64.
There are no generated IDs, rowversion columns, cascading deletes or ORM schema
migrations. The application must reject overflow before binding an arithmetic
result. Customer balance may be negative; prices and order totals may not.

The schema makes all fields required. Unique indexes enforce customer email,
product SKU and `(order_id, line_no)`. The only additional indexes are the two
specified order cursors and product FK index. Foreign keys use `NO ACTION`.
No order FK index beyond the unique order/line index is added.

Baseline strings use printable ASCII U+0020–U+007E, at least one character and
no trailing space, with email ≤254, SKU ≤64 and names ≤128 characters/bytes.
The no-trailing-space restriction avoids SQL Server's padded string equality
differing from PostgreSQL/SQLite uniqueness. Both language applications must
validate every character, length and NUL before binding; SQL checks supplement
that shared validation. Rich Unicode, alternative collations and serialization
tests are separate cases. SQL Server uses `VARCHAR` with
`Latin1_General_100_BIN2`, PostgreSQL `VARCHAR` with `"C"`, SQLite `TEXT` with
`BINARY`. String parameters must use those matching provider types and declared
maximum lengths, rather than inferring sizes from each input.

SQLite tables are `STRICT, WITHOUT ROWID`: this prevents SQLite's special
`INTEGER PRIMARY KEY` null-to-generated-rowid behavior and enforces explicit
non-null client IDs. This storage choice is part of the engine profile and must
be identical in both language processes. It does not imply native SQLite build
parity. Record native version, source ID, compile options and library SHA-256
separately for each language provider before admission.

## SQL files and transaction boundaries

Files use `:name` parameter markers. ADO.NET adapters may translate these to
provider-specific named markers; JDBC adapters translate to ordered `?` markers
and bind every occurrence in SQL order. Only markers/binding syntax may change.
No literal substitution is permitted. `D02` embeds its fixed page size of 50;
SQL Server uses `TOP (50)` and the other engines use `LIMIT 50`.

| Case | Shared files | Required application behavior |
| --- | --- | --- |
| D01 | `D01.sql` | One customer SELECT; materialize all five fields. |
| D02 | `D02.sql` | One SELECT returning at most 50 complete order rows, ordered by ascending `(created_epoch_ms,id)` strictly after the supplied cursor, filtered by status. |
| D03 | `D03.sql` | One joined SELECT returning five rows; reconstruct one full order/customer/five-line graph, preserving line number order and all fields. |
| D04 | `D04.sql` | One grouped SELECT, start-inclusive/end-exclusive UTC-millisecond range; returns ordered status/count/int64-total groups. Canonical windows must contain all four statuses; fewer groups fail parity validation. ORM uses equivalent LINQ/HQL projection. |
| D05 | `D05-read.sql`, `D05-update.sql` | Begin transaction, SELECT old customer, compute checked balance+fixed-delta and old-version+1, UPDATE with old-version predicate, require affected rows =1, commit. A zero count is an optimistic conflict and rolls back. |
| D06 | `D06-order.sql`, `D06-line.sql` | Begin transaction, INSERT one order at version 0, execute the line INSERT five times for contiguous line numbers 1–5, require six successful logical inserts, commit. Roll back the whole graph after any failure. |
| D07 | `D07-load.sql`, `D07-child.sql`, `D07-parent.sql` | Begin transaction, load the graph once, DELETE each of the five child IDs separately, DELETE parent with loaded old-version predicate, require one affected row for each deletion, commit. A stale parent must roll back child deletions. |
| D08 | composition, no SQL substitute | Consume an independent precomputed trace with exactly 60% D01, 15% D02, 10% D05, 10% D06, 5% D07. Keep failed operations in their intended category; never replace them with reads. |

H04 dispatches D01 and H05 dispatches D03/D06. H06 adds five product reads from
`H06-product.sql`, one customer read, six inserts and one version-qualified
balance update in a single transaction. ORM uses equivalent tracked product and
customer entity queries. Its frozen statement contract is 13 logical statements
before transaction scaffolding. It returns created order ID, total cents and
new customer version. An injected failure rolls back the entire checkout.

Parameters named `customer_id`, `order_id`, `line_id`, `product_id`, `cursor_id`,
`status`, `cursor_created_epoch_ms`, `start_epoch_ms`, `end_epoch_ms`,
`created_epoch_ms`, `balance_cents`/`new_balance_cents`, `total_cents`, `version`,
`old_version`, `new_version`, `line_no`, `quantity`, `unit_price_cents` all bind as
int64, without implicit numeric conversion. Raw updates supply computed new
values rather than replacing D05 with a single atomic balance increment.

Five-line totals are checked by the independent oracle against
`sum(quantity * unit_price_cents)` within signed int64. This cross-row invariant
and exactly-five-lines invariant are validated after commit, rather than through
triggers that would alter only one stack's work. Full result graph hashing and
catalog inspection remain outside timing. Statement and actual round-trip
counts must be captured separately in future untimed provider diagnostics.

## Connections and limits

SQL Server and PostgreSQL writes explicitly use `READ COMMITTED`. Preserve
durable engine settings and record SQL Server `READ_COMMITTED_SNAPSHOT`, server
versions, realized catalog, SQL, plans, driver pools and retry behavior. No
query template here starts, drops or creates a database. The controller provisions
an allowlisted disposable database and verifies its lease before initialization
or reset. See `infrastructure/README.md` for immutable image pins and the
controller-owned lifecycle. SQL Server operator EULA acceptance is required
before startup; this change accepts no EULA.

SQLite uses a named URI such as
`file:benchmark_contract_<unique-id>?mode=memory&cache=shared`. Keep one anchor
open for the repetition. Open one worker connection; there is no concurrent
writer profile here. Apply `foreign_keys=ON`, `read_uncommitted=0`,
`busy_timeout=5000` on **every** connection. `journal_mode=MEMORY` is recorded;
the true in-memory database is nondurable and does not use WAL. Closing the final
anchor destroys the database. Different language processes load independent
databases from identical canonical bytes. Shared-cache URIs do not share across
processes. EF InMemory/H2 do not substitute for any engine.

## Reproduction

Build the two independent database modules from `benchmark/`:

```powershell
dotnet build dotnet/Database/Database.csproj -c Release
.\.work\tools\apache-maven-3.9.11\bin\mvn.cmd -f java/database/pom.xml -q install
```

The thin Java artifact is available to the Spring application as
`benchmark:database-worker:1.0.0`; the standalone JSONL worker is the
`database-worker-1.0.0-all.jar` shaded classifier. Integration must preserve the
database module's exact provider versions when a Spring dependency BOM also
manages Hibernate or JDBC libraries.

The standalone worker takes four arguments: benchmark directory, engine, access
mode and canonical CSV directory. It emits one `ready` object with metadata and
initial state hash, then accepts one JSON object per line. A request such as
`{"case_id":"D05","input":{"customer_id":1,"delta_cents":125}}` executes
one operation. D06 requires order fields and an explicit array of five lines.
`STATE`, `METADATA`, `RESET` and `QUIT` need only `case_id`. `RESET` retains the
anchor and warmed factory and reimports only the four known tables. A network
reset is allowed only when this same session initialized the empty, explicitly
allowlisted controller-owned database; a preexisting network database cannot
be reset through this API.

`BATCH` takes `workload`, `trace`, positive `iterations` and optional `workers`
(1/8/32 for network engines, exactly one for SQLite). Canonical trace
`worker_partition` ownership is enforced before timed work, including rejection
of cross-worker customer writes or repeated insert/delete IDs. A trace entry is
either an input object or `{case_id,input}`. Results contain attempts, successes,
categorized failures, a latency for every attempt in nanoseconds, total batch
wall time and a blackhole-consumed last result. Individual timers span connection
acquisition through materialization/commit/context disposal. State hashing occurs
only on the separate untimed `STATE` command. Finite mutation traces may execute
only once, and D08 batches validate the frozen mix before execution. This timing
protocol supplies raw evidence; its `admitted` field remains false until the
controller validates the full campaign's correctness/parity/resource gates.

`AUDIT` is an untimed serial control: `{"case_id":"AUDIT","workload":"D08",
"trace":[{"case_id":"D01","input":{"customer_id":1}}]}` returns
`{"operations":1,"results":[{"case_id":"D01","result":{...}}]}` inside
the normal response envelope. Each call accepts1..1000 individual D01-D07/H06
operations, including mutations. Calls share state, so the controller can compare
bounded chunks against the independent oracle and then `RESET` before warmup.
The complete operation types are validated before any execution. An execution
failure ends the audit call; earlier operations remain committed and require a
reset. It does not change `BATCH` or its timer boundary.

`COMMITTED` with an `ids` array returns sorted unique existing order IDs. Its
public equivalents are C# `CommittedOrders(IEnumerable<long>)` and Java
`committedOrders(List<Long>)`. The untimed control leases one connection and uses
parameterized queries of at most500 IDs per statement. HTTP orchestration uses
it after all scheduled requests finish to reconcile actual commits, including
requests whose client response failed or timed out.

Network connections come only from secret environment variable
`BENCH_DB_CONNECTION`; Java may also use `BENCH_DB_USER`/`BENCH_DB_PASSWORD`.
`BENCH_DB_ALLOWED_HOSTS` must explicitly list the exact target host, and
`BENCH_DB_OWNED_NAME` must be a `bench_` prefix followed by a 32-hex controller
token and match the actual database name. `BENCH_DB_OWNERSHIP_FILE` must identify
an absolute local controller lease whose active status, engine, hosts, database
list and owner token match `BENCH_DB_OWNER_TOKEN`. Workers verify that lease
before connection and again before reset. Without these, the modules refuse to
connect. `BENCH_DB_INITIALIZE=1` permits applying schema/import only to an empty
allowlisted owned database; it never creates or drops a database. TLS settings
must be chosen equivalently by the controller and recorded before measurement.
The modules neither accept SQL Server terms nor start a database container.

From the repository root:

```powershell
python -m unittest discover -s benchmark/validation -p test_database_contract.py -v
python -m unittest discover -s benchmark/validation -p test_database_workers.py -v
python -m unittest discover -s benchmark/validation -p test_database_oracle.py -v
```

The 12 schema/query test methods use synthetic small populations and the Python
standard library's SQLite. They check catalog constraints, case-sensitive uniqueness, length/NUL
and ASCII rejection, signed 64-bit boundaries, no cascade, single-query graph
shape, deterministic keyset pages, grouped totals, optimistic version conflicts,
six-write insertion rollback and transactional deletion rollback. Python SQLite
is a schema/query reference check only; its version is separate from the
Microsoft.Data.Sqlite/Xerial native engines. The first integration test method
executes both language workers in raw and ORM modes (four cell executions),
comparing ordered results and complete committed-state hashes against the
production dictionary oracle in `validation/database_oracle.py`. It tests all
eight database workload IDs, stale updates/deletes, duplicate inserts, injected
rollback, continued D08 attempts after a conflict, checkout, reset, and the batch
counter/latency contract. A second method checks denied network targets, missing
ownership leases and invalid finite trace/worker requests in both languages. These are
correctness tests, not independent performance repetitions. Coverage percentages
are unavailable.

A third worker method checks all four cells' full ordered audit outputs across
two stateful chunks, mutation results and final state, commit reconciliation
across more than500 IDs, duplicate/missing IDs, empty inputs, invalid controls
before mutation and reset restoration.

Three additional dictionary-oracle methods verify its indexed five-line graph
lookup, valid bulk insertion/deletion and index restoration after partial insert
or injected delete rollback. This index keeps full finite-trace validation from
scanning the entire line population for each order. It does not call a provider.

An optional standalone live PostgreSQL integration command creates four leased
databases in one disposable pinned container and removes them afterward:

```powershell
python benchmark/validation/verify_postgresql.py --config benchmark/.work/postgresql-smoke.config.json
```

The explicit configuration selects the immutable image, disjoint application/DB
cores and memory budgets; it does not enable a full campaign. The 2026-10-27
execution used application CPUs0/1 with2GiB and PostgreSQL CPUs2/3 with1GiB.
All four cells passed ordered outputs, negative mutation cases, complete state
hashes, checkout and reset. The refreshed run also passed an eight-operation
read/write audit and reconciliation across more than500 scheduled order IDs.
Evidence is `.work/postgresql-controls-validation.json` and its four cell
transcripts; it includes realized catalog and durability settings. The earlier
run is `.work/postgresql-validation.json`, with separate catalog evidence. This is a
standalone contract script with four cell executions, separate from the18
authored database unittest methods. It is functional evidence, not a measured
performance repetition or provider statement/round-trip audit.
