# Semantic parity and admission

Updated 2026-10-27. Active track: .NET 10 LTS / Microsoft OpenJDK 25 LTS.
Implementation and functional verification are separate from campaign admission.
No scored campaign has run.

| Obligation | Implementation and evidence |
| --- | --- |
| Common inputs | Python owns integer, Unicode/text/binary, exact JSON/CSV, protobuf/gzip and relational fixtures; manifests hash canonical bytes |
| Independent correctness | Python arbitrary-precision/math/serialization oracles and dictionary database model; complete CHECK output and committed-state comparisons |
| Numeric behavior | Explicit modulo2^64; common scalar matrix loop order/tolerance; decimal HALF_EVEN; exact signed int64 and Unicode |
| State lifetime | PREPARE supplies fresh mutable sort/list/map inputs outside timing; retained graphs, connections, platform workers and native buffers have explicit lifetimes |
| Measurement | Protocol2 PREPARE/CHECK/BATCH, externally timed equal work, BDN/JMH corroboration with five launches required |
| Serialization | Typed reflection JSON, generated protobuf, strict RFC4180 CSV; separate encode/decode/parse/emit operations and full semantic checks |
| Concurrency | Controlled fixed platform workers/bounded queues; real common delay service; async/virtual-thread profile labeled separately |
| I/O | Owned files, identical bytes/offsets/buffers, explicit durable flush, persistent TCP/TLS1.3 and nonresumed handshake variant |
| Native ABI | One C artifact for both languages, preallocated matching data, explicit lifetime, source/compiler/artifact provenance |
| HTTP | Real ASP.NET Core and Spring MVC/Tomcat, typed JSON and named provider/ORM paths; loopback owned state/reset controls |
| Database | D01-D08/H06 raw/entity paths, tracked state, concurrency tokens, rollback/stale/duplicate checks, finite traces and full-state oracle |
| SQLite | Four language/access cells pass semantics; native3.53.3 versus3.53.4 mismatch excludes primary aggregate; source/options/native metadata retained |
| Network engines | Modules compile; immutable images, ownership leases, disposable init/reset/cleanup and explicit budgets; live evidence tracked separately |
| Resources | Common per-PID CPU/RSS collector; Windows JobObject/affinity or operator-provided Linux cgroup; failed/missing evidence excludes ranking |
| Warmup/repetition | Stable warmup, disjoint finite traces, frozen pilot work and balanced seeded independent process pairs; smoke remains scaled/unscored |
| HTTP latency | Scheduled arrival through completion, including queue delay; errors/unsent work retained; generator capacity and frozen rate grid required |
| Deployment | Owned staging rebuild/package, artifact hashes/bytes, dependency/license inventory and disclosed runtime-inclusive distribution boundaries |
| Statistics | Five paired process estimates, whole-pair bootstrap, fixed metric weights, engine/access views and sensitivity profiles |
| Full score | All mandatory admission gates required; missing evidence preserved; smoke/pilot never ranked |

`validation/verify.py --java` records exact source inventory, authored test
definitions, discovered cases, executed results and separate subtests. Workload
parameter expansion is not authored tests. Coverage is unavailable without a
measured coverage report.

Checksums cheaply consume results and catch regressions; complete semantic
outputs and state digests supply additional evidence. Counts/checksums do not
establish general assertion quality. Functional correctness does not establish
equal engines, equal resources, generator headroom or a performance advantage.
