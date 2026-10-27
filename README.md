# Java versus C#

An independent comparison of language capabilities and equivalent application
performance. The active implementation uses **.NET 10 LTS and Java 25 LTS**.

[Benchmark setup and commands](benchmark/README.md) ·
[Work state and resume instructions](docs/WORKLOG.md) ·
[Implementation specification](benchmark/specification.md)

The three [source PDFs](docs/) define the qualitative assessments and benchmark
plan. The suite implements all 55 workload IDs and 180 parameter definitions,
with independent oracles, real HTTP frameworks, raw/ORM database paths,
native interop, paired execution and completeness-gated reporting.
No performance ranking has been measured; functional smoke and campaign
admission remain separate.

Use the [Edu Developer Toolbox wiki](https://github.com/ICS-Eng/Edu-Developer-Toolbox/wiki)
as development guidance. Benchmark databases and test state belong to this
repository's disposable environment.
