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
