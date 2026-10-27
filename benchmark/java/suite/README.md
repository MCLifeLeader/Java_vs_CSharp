# Java 25 LTS suite

This project implements the non-database worker operations and a real Spring Boot
MVC/Tomcat H01-H06 service. Database endpoints invoke the sibling database module
directly. Startup, packaging and build cases run through the common controller.
The catalog's 55 authored case definitions and 180 parameter definitions describe
both stacks; implementation presence does not establish measurement admission.

## Build

From the repository root. If the pinned JDK is absent, provision its verified
archive, then activate tools for this PowerShell process:

```powershell
python benchmark/tools/bootstrap.py --component jdk
. ./benchmark/tools/activate.ps1
python benchmark/java/suite/bootstrap_maven.py
$maven = Join-Path $PWD 'benchmark/.work/tools/apache-maven-3.9.11/bin/mvn.cmd'
& $maven -f benchmark/java/database/pom.xml install
& $maven -f benchmark/java/suite/pom.xml package
python benchmark/java/suite/verify_dependencies.py
```

The pinned runtime dependencies and SHA-256 values are listed in
[dependencies.lock.json](dependencies.lock.json). This runtime JAR inventory does
not replace the full packaging SBOM/license gate. Key versions are Spring Boot
3.5.7, Tomcat 10.1.48, Jackson 2.20.1, protobuf/protoc 4.33.2, JMH 1.37 and Jakarta
Persistence 3.2.0. The explicit JMH annotation processor is required on JDK 25.

The persistent worker accepts the shared PREPARE/CHECK/BATCH protocol:

```powershell
java --enable-native-access=ALL-UNNAMED -jar benchmark/java/suite/target/suite.jar --data benchmark/.work/suite-data/smoke --scale smoke --workers 4
```

See [workload contracts](../../docs/workload-contracts.md) for algorithms,
fixtures, setup and timing boundaries, semantic outputs and server routes.
Set `BENCH_NATIVE_LIBRARY` to the common library produced by
`python benchmark/native/build.py` for N01/N02. TLS tests trust only the supplied
controller fixture certificate; persistent transfer setup happens in PREPARE.

## Verification and resume

The independent Java protocol smoke can be repeated after generating fixtures:

```powershell
python benchmark/java/suite/verify_smoke.py --native benchmark/.work/bench.dll
python -m unittest discover -s benchmark/validation -p test_suite_http.py -v
python benchmark/java/suite/run_jmh.py --data benchmark/.work/suite-data/smoke --case C01 --parameter transform --output benchmark/.work/java-jmh-smoke.json --smoke
```

On 2026-10-27, Maven executed six authored JUnit 5 CSV/FNV contract methods with
six passing, zero failing and zero skipped; evidence is
`target/surefire-reports/benchmark.suite.CsvContractTest.txt`. Java protocol smoke
independently checked all 94 non-database parameter variants and their BATCH2
outputs: 94 passing, zero failing, zero skipped. These are expanded operation
checks, not 94 authored JUnit methods. Evidence is
`benchmark/.work/java-suite-validation.json`; the script checks full semantic
SHA-256 and units against the language-neutral oracle, plus repeated batch totals.

The shared HTTP suite defines three unittest methods with language/access subtests.
On 2026-10-27 all three passed, with zero failures and zero skips. Eight owned
framework/cell server executions made 118 asserted HTTP requests, 59 per language.
The cases verify all JSON sizes, malformed input, complete database responses and
state against an independent oracle, injected transaction rollback, committed-ID
audit, provider metadata, request quiescence and reset/reopening admission.
Its request inventory is written to
`benchmark/.work/test-suite-http-evidence.json`. The repository verification runner
records exact source commit, working-tree source hashes, discovery and execution
separately; use that record for the integrated suite's current totals.

The JMH smoke completed one real fork with no warmup and one 100 ms measurement.
It proves harness startup and is excluded from performance evidence. A normal
corroboration run uses five forks, three warmup and five measurement iterations.
No scored campaign or performance conclusion follows from these functional runs.
Code coverage is unavailable because no measured coverage report was produced.
