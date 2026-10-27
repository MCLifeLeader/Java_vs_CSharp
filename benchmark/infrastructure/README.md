# Disposable database infrastructure

The measurement controller's `controller/infrastructure.py` owns container and
database creation, readiness, ownership labels, credentials and cleanup.
`provision.py` reuses that lifecycle. The two images in `images.lock.json` were
resolved through official registry manifest/config APIs on 2026-10-27 for native
Linux AMD64. Image metadata is recorded; neither image was started to resolve
the pins. SQL Server's edition, terms and EULA acceptance must be recorded by
the operator before startup.

The active campaign starts with no database resource allocation. Review the
host's physical/logical cores, CPU topology and memory, then add disjoint app/DB
allocations and exact image digests. `resource-profile.example.json` illustrates
the specification's 16-core host layout and refuses direct execution. It does
not assert that this machine has that budget. SQL Server and PostgreSQL use
the same allocation in separate engine campaigns, never simultaneously.

From the repository root, review without creating resources:

```powershell
python benchmark/infrastructure/provision.py plan --engine postgresql
python benchmark/infrastructure/provision.py plan --engine sqlserver
```

Every campaign starts while holding a checkout-specific kernel lock. It first
recovers stale resources recorded by this checkout, then creates an exclusive
run scratch directory. Review or request the same recovery explicitly:

```powershell
python benchmark/infrastructure/provision.py clean-start --dry-run
python benchmark/infrastructure/provision.py clean-start
```

Both commands return JSON with `ready`, `status`, `actions` and `blockers`;
exit code 0 means clean, and 2 means cleanup or review is still needed. Dry-run
only reads leases, filesystem identities and Docker ownership labels. It does
not create a lock file, stop a process or remove a resource. The main
`benchmarkctl.py clean-start` command exposes the same operation.

Leases record the checkout identity, random owner token and owner PID plus its
OS process creation identity. Container intent is saved before `docker create`,
and the returned container ID is saved before `docker start`. Recovery requires
matching checkout, token and process-birth labels; it removes only those owned
containers and their anonymous volumes. It never invokes Docker prune or
changes shared Toolbox services. A live owner, unreadable identity, replaced
temporary path or unleased legacy staging directory blocks a new run. Docker
can be absent for a non-database run when no known container lease requires it.

Child processes are contained in Windows kill-on-close JobObjects or dedicated
Linux process sessions. Completion closes the containment handle so build-tool
descendants cannot survive a finished command. A crashed controller releases
its kernel lock, and the next start recovers stale leases before doing work.
Workers receive `BENCH_SCRATCH_ROOT` and create unique private I/O directories;
they never reuse another run's files. Recovery verifies filesystem identity
before removing leased scratch. Evidence, results, tools, immutable inputs,
traces and `.work/quarantine` remain preserved. Ambiguous legacy resources need
explicit attribution and review; familiar directory names alone do not grant
ownership.

An execution command additionally needs a reviewed campaign configuration,
compiled database worker, generated canonical CSV directory and explicit JSONL
request file. The wrapper creates a random owned database in one disposable,
labeled container, runs the worker, captures redacted evidence and cleans up.
It exposes only a random loopback port and does not use Toolbox shared services.
Network reset is restricted to the session that initialized that exact owned
database and a matching active ownership lease. Data remains on the image's
ordinary storage; tmpfs and relaxed durability do not substitute for the durable
network baseline.

`compose.yaml` is a declarative reference for the same images and boundaries.
The controller uses its lifecycle rather than invoking Compose. If adapting
Compose for a diagnostic profile, choose exactly one engine profile, supply
private generated credentials and explicit limits, record the new profile,
and clean up only that project's owned resources. `ACCEPT_EULA` defaults to `N`;
setting it to `Y` is an operator action.
