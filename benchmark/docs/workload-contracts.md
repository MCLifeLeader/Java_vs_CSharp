# Full-suite implementation contracts

Active runtimes: .NET 10 LTS and Microsoft OpenJDK 25 LTS. This document refines
the supplied specification's unresolved algorithms. It does not freeze a scored
campaign. All integers below wrap modulo 2^64 for checksums; domain arithmetic
is checked unless the case explicitly measures wrapping.

## Worker protocol and scales

Launch with `--data DIR --scale smoke|S --workers N [--profile controlled|idiomatic]`.
The process loads canonical inputs before emitting a JSON readiness object:
`status=ready, protocol=2, language, runtime, supported=[case IDs]`.
`CHECK\tcase\tparameter` executes one operation and returns JSON fields `status=ok`,
`case_id`, `parameter_id`, `checksum` (16 lowercase hex), `work_units`, `unit`,
`semantic_hash` (SHA-256 of the canonical returned bytes), and `correctness=true`.
`BATCH\tcase\tparameter\titerations` executes that operation repeatedly and returns
the same identity plus `iterations`, **total** `work_units`, `unit`, `correctness`,
and the modulo sum of per-operation checksums. BATCH computes no SHA-256 except
where SHA-256 is the workload C05. No console output occurs inside an operation.
`PREPARE\tcase\tparameter\titerations` prepares fresh mutable inputs outside timing; queued mutable setup is capped by `BENCH_PREPARE_BYTES` (default256MiB) and returns `status=prepared` or an explicit error.
`QUIT` closes owned resources. Unsupported parameters fail explicitly.

Unless specified otherwise, returned bytes are UTF-8 text with LF separators and
no final LF, integers use invariant decimal, and booleans are lowercase. FNV-1a64
(offset 14695981039346656037, prime 1099511628211) consumes byte outputs when a
case has no natural scalar. CHECK may include `details` and output size; its SHA
is outside the future timed boundary. Scalar result bytes are unsigned decimal.
Stack-specific package/GC diagnostics use checksum 1 for a successful operation
and expose diagnostic values separately; those values are not semantic parity.

Smoke is a functional scale only: 4,096 primitive integers, 1,000 business/list/map
records, matrices at min(requested N,16), 1,000 invoices, 10,000 retained linked
records, 1,000 graph tasks, 16/32 I/O in-flight requests, 64 KiB files, 8 KiB CSV,
and 1,000 allocation objects per window. S uses the plan's sizes. Every scaled parameter
retains its requested parameter ID and reports actual units. No smoke result is
admitted into a performance index. Corpus-sized parameters are capped at available
fixture bytes at smoke scale. Seeds and canonical bytes come from Python only.

## Shared fixtures

`integers.i64le`: signed int64 little endian. `ascii.txt`, `unicode.txt`: valid UTF-8.
`text-1m.bin`, `random-1m.bin`: common gzip/crypto buffers. `document-1024.json`,
`document-65536.json`, `document-1048576.json`: nested JSON objects with `id` int64,
`name` string, `group` int64, `amount_cents` int64, `active` bool and `children`
array of `{id:int64,label:string}`. Extra `padding` string may establish byte size.
JSON equivalence is parsed-value equality, with canonical CHECK hashes computed
from the original canonical fixture bytes after validated round trip.
`csv-10m.csv`: UTF-8, CRLF, header `id,name,group,amount_cents,active`, RFC4180 quoting,
including quoted commas, double quotes, Unicode and embedded newlines.
`protobuf.bin`: protobuf `Record { int64 id=1; string name=2; repeated int64 values=3
[packed=true]; bool active=4; }`. A checked-in `.proto` fixes the field contract.

## Compute, collections and memory

| Case | Concrete operation and checksum/result |
| --- | --- |
| C01 transform | Existing unsigned add/multiply/xor/rotate, sum; input count units. |
| C02 n128/n512 | Matrices A[i,j]=((i*17+j*13)%97)/97.0, B[i,j]=((i*7+j*19)%89)/89.0; i/j/k loops, no FMA. Return invariant sum rounded to 6 decimals with HALF_EVEN; full CHECK values compared within 1e-9 abs+relative. FLOP count=2*N^3. |
| C03 invoices | Invoice i base cents=100+i%10000, quantity=1+i%5, discount=(i%7)/100, tax=.0825; round discounted subtotal and tax separately to 2 decimal HALF_EVEN; return sum of final integer cents. |
| C04 bits2048 | modulus=2^2048-159, base=2^1024+12345, exponent=65537; return unsigned big-endian ModPow bytes. |
| C05 bytes1024/bytes1048576 | SHA-256 common random buffer prefix, checksum FNV digest; byte count units. |
| C06 compress/decompress-text/incompressible | Common corpus; gzip default portable level 6 where exposed; decompress prebuilt gzip fixture, validate original bytes. Return original roundtrip bytes, expose compressed size as diagnostic. Compression ratio gate remains. |
| K01 scan/sort | Existing primitive scan and signed ascending fresh-copy sort; all-output SHA in CHECK, weighted checksum in BATCH; PREPARE clones a fresh primitive array per iteration outside timing. |
| K02 append/iterate/remove | Reference records `(id=i,name='r'+i,group=i%17,amount=i*3,active=i%2==0)`; exact initial capacity N. Remove every second record from end toward zero; checksum sum of remaining IDs; append/iterate checksum sum of all IDs. |
| K03 ascii/int64 insert/lookup/remove | PREPARE lazily builds ASCII/preboxed-int64 key traces and maps outside timing. Read-only lookup reuses one map across iterations; remove receives a fresh prepared map per iteration. N keys `k`+8-digit i or int64 i; values=i*3. Lookup fixed trace i=0..2N-1 (50% hits); remove even keys; checksum summed values or remaining values. |
| K04 union/intersection prebuilt/build-inclusive | Sets [0,N) and [N/2,N+N/2); returned sorted IDs; checksum sum IDs. Prebuilt setup held outside each operation. |
| K05 encode/decode/search ascii/unicode | Strict UTF-8; ordinal nonoverlapping search for `benchmark` (ASCII) or `世界` (Unicode). Encode starts with cached text; decode performs one strict decode and keeps the result alive without timed re-encode. Both return FNV of original UTF-8 bytes after CHECK validation; search uses cached text and returns integer count. |
| K06 integer-parse/regex-extract | Parse ASCII decimal lines -500..499 repeated; reject malformed/overflow; regex `[A-Za-z]+|[0-9]+` over ascii.txt, checksum total UTF-8 matched bytes. Corpus strings and integer grammar validation are prepared once. Regex compiled once, no captures/backtracking extensions. |
| M01 allocate-release | Record with 8 int64 fields (64 logical bytes), values i..i+7; allocate escaping into temporary array then consume field0; sum IDs. |
| M02 survival-1/10/50pct | Allocate same 64-byte logical records, ten windows each containing N records0..N-1, retaining every100/pth record and at most the most recent three retained windows; consume field0 and return ten times the ID sum. Controller schedules 120s at S and records collector diagnostics separately. |
| M03 retain-query | Linked records `(id,next)` plus six int64 logical fields; retain N records, query chain ten traversals, sum IDs each traversal. |
| M04 copy/slice/parse-size | Fresh allocate/copy byte baseline, slice is also copy; parse sums bytes. Return byte sum of resulting buffer; no pooled/zero-copy substitution. |
| M05 limit-512MiB/2048MiB | Same mixed allocation as M02 survival10; external controller enforces equal process budget and offered work. Deadline/OOM results preserved, no forced GC in operation. |

## Concurrency and serialization

| Case | Concrete operation and checksum/result |
| --- | --- |
| A01 task-graph | N tasks, bounded queue1024; task i computes wrapping `(i+1)*(i+3)`; fixed workers; sum. |
| A02 pipeline | Half workers producers/half consumers (minimum1 each), bounded queue1024, 1KiB payload; sequential message IDs; checksum sum IDs, complete count validated. |
| A03 atomic/lock | Each worker increments shared counter10,000 (smoke100); return final exact count. |
| A04 inflight1000/10000 | Shared real HTTP delay service, fixed platform pool in controlled or C#async/Java virtual threads in idiomatic; consume one common response per request. No Thread.Sleep surrogate. |
| A05 deadline/child-failure | Bounded child tasks with common deadline/failure injection; cancel remaining children and await quiescence. Return count of leaked children (must0), plus failure/deadline observed=true. Speed diagnostic. |
| S01 serialize/deserialize-size | Jackson versus System.Text.Json reflection baseline, strict parse, roundtrip required; canonical parsed document output. |
| S02 encode/decode | Shared protobuf schema/vector; reencoded bytes exactly equal canonical vector (field order1,2,3,4). |
| S03 pipeline/imperative | N standard reference records; keep active records, map amount*2, group by group ID and sum, sort ascending group, canonical lines `group:sum`. |
| S04 parse/emit | RFC4180 parse/emit canonical CSV; preserve all fields including embedded newlines. Parse consumes cached decoded text and parsed rows without timed emission; emit performs one encoding of prebuilt rows. Each operation consumes one FNV checksum; CHECK validates exact rows and canonical emitted bytes. |

## I/O, HTTP, native and diagnostic cases

| Case | Concrete operation and checksum/result |
| --- | --- |
| I01 read/write-buffered/durable-flush | One controller-owned temporary file, sequential64KiB buffers, common repeated random corpus to requested1GiB (smoke64KiB); durable write forces file storage. Read profile records prior flush/cache state; return bytes processed and content checksum. |
| I02 random-4KiB | Fixed sequential trace offset `(i*7919 mod blocks)*4096`, 256 reads(smoke16); common one outstanding request. Sum first byte of each returned block, validate full bytes in CHECK. |
| I03 echo-1MiB | Shared TCP echo server, explicit loopback, keepalive connection, full framed send/read, complete common payload; FNV returned bytes. |
| I04 handshake/persistent-transfer | Shared TLS1.3 echo server, same cert/cipher policy, explicit supplied test cert trust; fresh handshake per op with session invalidation or a reused connection prepared outside timing for transfers, common payload. Trust context is prepared outside timing. |
| H01 get-* | ASP.NET Core versus SpringBoot MVC/Tomcat; GET/plaintext is exactly32 ASCII bytes `0123456789abcdef0123456789abcdef`, text/plain,200. |
| H02 get/post-size-* | GET/json?size=N returns canonical fixture; POST/json?size=N validates object and echoes canonical parsed value; application/json,200. |
| H03 get-* | GET/compute returns C01 checksum as16 lowercase hex UTF-8,200. |
| P01 H02/H04 | Controller launches new service process and validates first response, dependencies pre-ready; no worker RUN shortcut substitutes startup timing. |
| P02 idle30-load60 | Controller-owned startup/idle/load/resource timeline against H02. |
| P03 dependency-cached-clean | Controller invokes real package build on fresh owned staging tree; reports bytes and build time. Complete SBOM/license verification remains an explicit admission gate; no checksum comparison of artifact bytes across stacks. |
| P04 packaged-service | Controller launches framework-dependent packaged application; complete runtime-inclusive deployment size reported separately. |
| N01 count1/count1024 | Shared native C ABI `uint64_t bench_sum(const int64_t*, size_t)` sums signed values as uint64; identical first N signed int64 values i-512; native binary path from environment BENCH_NATIVE_LIBRARY. |
| N02 preallocated | Native1MiB buffer common bytes; `uint64_t bench_buffer(const uint8_t*,size_t)` unsigned sum; deterministic lifetime and free. |
| L01 representations | Numeric contents i0..N-1; primitive, boxed-reference, reference-generic and idiomatic-record variants all sum same IDs. The reference-generic and boxed-reference labels currently use the same fresh boxed-int64 reference-list implementation on both stacks and are diagnostic aliases, without extra score weight; layout/retained RSS reported separately. |
| L02 switch/pattern | Input tag i%4; result tag0=i+1,tag1=i*2,tag2=i^85,tag3=i-3; prebuilt tagged reference records; sum. |
| L03 failure0/0.1/1/10pct | N calls at stack depth3, fail when i%1000<threshold. New exception includes stack trace; catch adds1, success adds i; exact count checked. |
| L04 invocation/reflection | Target `long Target(long x)=>x*3+1`; N invocations, capture offset7 for capturing variant. Cached metadata or lookup per op as named. Sum returned values; no dynamic codegen. |
| L05 clean/no-op/one-method | Controller invokes actual independent rebuilds (10), dependency downloads excluded, one-method edit under owned temp copy; output validated. |

`services.json` in the fixture directory supplies allowlisted loopback delay/TCP/TLS
endpoints and the test certificate path. Services are language-neutral and owned
by the controller. Server logging must not print to the worker JSON stdout.
H04-H06 and all D cases are specified in the database contract, not duplicated here.

Serialization CHECK and BATCH consume FNV-1a of the original canonical JSON fixture, canonical protobuf vector, canonical ordered group lines, or canonical emitted CRLF CSV bytes, respectively. CHECK additionally validates actual roundtrip/parsed values; encode and decode timed operations remain distinct. Stack-specific JSON emitted sizes are diagnostic. Java uses generated protobuf4.33.2 runtime/protoc and Jackson2.20.1; C# pins its own named compatible runtime.

## Java build and harness

Run `python benchmark/java/suite/bootstrap_maven.py` to verify and unpack Maven3.9.11
into the ignored work directory. Install `benchmark/java/database/pom.xml` first,
then run Maven `-f benchmark/java/suite/pom.xml package` with JDK25. The worker is
`java --enable-native-access=ALL-UNNAMED -jar benchmark/java/suite/target/suite.jar`.

JMH uses `python benchmark/java/suite/run_jmh.py --data DIR --case C01
--parameter transform --output FILE`. This launches a regular dependency classpath
so JMH's forked JVMs find its generated harness. `--smoke` proves one fork starts
and completes; its short run is excluded from performance evidence. Standard
JMH corroboration uses five independent forks, three warmup and five measurement
iterations; it does not replace fixed-work controller evidence.

The HTTP service startup path prepares its typed JSON cache only; primitive/list/map,
CSV/protobuf, concurrency and native case setup is excluded. H03 loads integer
inputs lazily before its warmed measurement boundary. Loopback-only control routes
`GET /__benchmark/active`, `POST /__benchmark/quiesce`, `POST /__benchmark/reset`,
`GET /__benchmark/state`, `GET /__benchmark/metadata` and
`POST /__benchmark/committed` execute outside workload timing. Quiesce closes
application admission atomically and waits for in-flight application requests;
reset restores admission, including services without a database. Committed-ID
audit accepts at most500 positive IDs and enables exact post-timeout state checks.
