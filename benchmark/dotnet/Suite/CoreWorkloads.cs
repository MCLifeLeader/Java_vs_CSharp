using System.Buffers.Binary;
using System.Globalization;
using System.IO.Compression;
using System.Numerics;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Google.Protobuf;

namespace BenchmarkSuite;

public record WorkResult(ulong Checksum, long WorkUnits, string Unit, byte[] Bytes, object? Details = null)
{
    public static WorkResult Scalar(ulong value, long units, string unit = "items", object? details = null) =>
        new(value, units, unit, CoreWorkloads.SemanticBytesRequested ? Encoding.UTF8.GetBytes(value.ToString(CultureInfo.InvariantCulture)) : Array.Empty<byte>(), details);
    public static WorkResult Data(byte[] bytes, long units, string unit = "bytes", object? details = null) =>
        new(CoreWorkloads.Fnv(bytes), units, unit, bytes, details);
}

public sealed class LogicalRecord(long id)
{
    public long A = id, B = id + 1, C = id + 2, D = id + 3, E = id + 4, F = id + 5, G = id + 6, H = id + 7;
}
public sealed record BusinessRecord(long Id, string Name, int Group, long Amount, bool Active);
public sealed class Link(long id, Link? next) { public long Id = id; public Link? Next = next; public long B, C, D, E, F, G; }
public readonly record struct ValueRecord(long Id);
public sealed record Tagged(int Tag, long Value);
public sealed record DocumentChild(long Id, string Label);
public sealed record DocumentModel(long Id, string Name, long Group, long AmountCents, bool Active, DocumentChild[] Children, string Padding);

public sealed partial class CoreWorkloads : IDisposable
{
    [ThreadStatic] internal static bool SemanticBytesRequested;
    public readonly string DataDirectory;
    public readonly bool Smoke;
    public readonly int Workers;
    public readonly string Profile;
    public readonly long[] Integers;
    public readonly byte[] RandomBytes, TextBytes;
    public readonly BusinessRecord[] Records;
    readonly HashSet<long> setA, setB;
    readonly UTF8Encoding utf8 = new(false, true);
    readonly Regex tokens = new("[A-Za-z]+|[0-9]+", RegexOptions.CultureInvariant);
    readonly Dictionary<int, byte[]> documents = new();
    readonly byte[] protobuf;
    readonly Benchmark.Suite.Proto.Record protobufObject;
    readonly Dictionary<int, DocumentModel> documentObjects = new();
    readonly JsonSerializerOptions jsonOptions = new() { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower, UnmappedMemberHandling = System.Text.Json.Serialization.JsonUnmappedMemberHandling.Disallow };
    readonly Dictionary<string, byte[]> corpora = new();
    readonly Dictionary<string, byte[]> compressedCorpora = new();
    readonly Dictionary<string, string> corpusText = new();
    readonly string[] numericLines;
    readonly string csvText;
    readonly byte[] csvBytes;
    readonly List<string[]> csvRows;
    readonly List<BusinessRecord> recordList;
    readonly Tagged[] taggedInputs;
    string[]? asciiKeys;
    readonly long preparationBudget;
    Link? linkedHead;
    public int Count => Smoke ? 1000 : 100000;

    public CoreWorkloads(string data, bool smoke, int workers, string profile)
    {
        DataDirectory = Path.GetFullPath(data); Smoke = smoke; Workers = workers; Profile = profile;
        preparationBudget = Environment.GetEnvironmentVariable("BENCH_PREPARE_BYTES") is { } budget ? long.Parse(budget, CultureInfo.InvariantCulture) : 256L * 1024 * 1024;
        if (preparationBudget < 1) throw new ArgumentException("Preparation budget must be positive");
        byte[] binary = File.ReadAllBytes(Path.Combine(data, "integers.i64le"));
        if (binary.Length == 0 || binary.Length % 8 != 0) throw new InvalidDataException("Invalid int64 fixture");
        Integers = Enumerable.Range(0, binary.Length / 8).Select(i => BinaryPrimitives.ReadInt64LittleEndian(binary.AsSpan(i * 8, 8))).ToArray();
        RandomBytes = File.ReadAllBytes(Path.Combine(data, "random-1m.bin"));
        TextBytes = File.ReadAllBytes(Path.Combine(data, "text-1m.bin"));
        Records = Enumerable.Range(0, Count).Select(i => new BusinessRecord(i, "r" + i, i % 17, i * 3L, i % 2 == 0)).ToArray();
        setA = Enumerable.Range(0, Count).Select(i => (long)i).ToHashSet();
        setB = Enumerable.Range(Count / 2, Count).Select(i => (long)i).ToHashSet();
        foreach (int size in new[] { 1024, 65536, 1048576 }) { documents[size] = File.ReadAllBytes(Path.Combine(data, $"document-{size}.json")); documentObjects[size] = JsonSerializer.Deserialize<DocumentModel>(documents[size], jsonOptions)!; }
        protobuf = File.ReadAllBytes(Path.Combine(data, "protobuf.bin"));
        protobufObject = Benchmark.Suite.Proto.Record.Parser.ParseFrom(protobuf);
        foreach (string name in new[] { "ascii", "unicode" }) { corpora[name] = File.ReadAllBytes(Path.Combine(data, name + ".txt")); corpusText[name] = utf8.GetString(corpora[name]); }
        foreach (string name in new[] { "text-1m", "random-1m" }) compressedCorpora[name] = File.ReadAllBytes(Path.Combine(data, name + ".gz"));
        numericLines = File.ReadAllLines(Path.Combine(data, "integers.txt"));
        csvBytes = File.ReadAllBytes(Path.Combine(data, "csv-10m.csv")); csvText = utf8.GetString(csvBytes); csvRows = Csv.Parse(csvText);
        recordList = new List<BusinessRecord>(Records); taggedInputs = Enumerable.Range(0, Count).Select(i => new Tagged(i % 4, i)).ToArray();
    }

    public static ulong Fnv(ReadOnlySpan<byte> bytes)
    {
        ulong hash = 14695981039346656037UL;
        unchecked { foreach (byte value in bytes) hash = (hash ^ value) * 1099511628211UL; }
        return hash;
    }
    public static byte[] LongBytes(IEnumerable<long> values)
    {
        using var output = new MemoryStream();
        Span<byte> bytes = stackalloc byte[8];
        foreach (long value in values) { BinaryPrimitives.WriteInt64LittleEndian(bytes, value); output.Write(bytes); }
        return output.ToArray();
    }
    public static long Target(long value) => value * 3 + 1;
    public static ulong Transform(long[] values)
    {
        ulong total = 0;
        unchecked { foreach (long value in values) { ulong x = ((ulong)value + 0x9e3779b97f4a7c15UL) * 0xbf58476d1ce4e5b9UL; x ^= x >> 29; total += BitOperations.RotateLeft(x, 17); } }
        return total;
    }

    public WorkResult Execute(string id, string parameter, bool check)
    {
        SemanticBytesRequested = check;
        switch (id)
        {
            case "C01": return WorkResult.Scalar(Transform(Integers), Integers.Length);
            case "C02": return Matrix(parameter, check);
            case "C03":
                decimal invoices = 0;
                for (int i = 0; i < Count; i++)
                {
                    decimal subtotal = decimal.Round((100 + i % 10000) / 100m * (1 + i % 5) * (1m - (i % 7) / 100m), 2, MidpointRounding.ToEven);
                    invoices += subtotal + decimal.Round(subtotal * .0825m, 2, MidpointRounding.ToEven);
                }
                return WorkResult.Scalar((ulong)(invoices * 100), Count, "invoices");
            case "C04":
                var number = BigInteger.ModPow((BigInteger.One << 1024) + 12345, 65537, (BigInteger.One << 2048) - 159);
                return WorkResult.Data(number.ToByteArray(isUnsigned: true, isBigEndian: true), 1, "operations");
            case "C05":
                int size = parameter == "bytes1024" ? 1024 : 1048576;
                return WorkResult.Data(SHA256.HashData(RandomBytes.AsSpan(0, size)), size);
            case "C06": return Compression(parameter, check);
            case "K01":
                if (parameter == "scan")
                {
                    ulong total = 0; unchecked { foreach (long value in Integers) total += (ulong)value; }
                    return WorkResult.Scalar(total, Integers.Length);
                }
                long[] sorted = prepared.TryGetValue(id + parameter, out object? queued) && ((Queue<long[]>)queued).Count > 0 ? ((Queue<long[]>)queued).Dequeue() : (long[])Integers.Clone(); Array.Sort(sorted);
                ulong weighted = 0; unchecked { for (int i = 0; i < sorted.Length; i++) weighted += (ulong)sorted[i] * (ulong)(i + 1); }
                return new(weighted, sorted.Length, "items", check ? LongBytes(sorted) : Array.Empty<byte>());
            case "K02":
                var list = parameter == "iterate" ? recordList : parameter == "remove" && prepared.TryGetValue(id + parameter, out object? lists) && ((Queue<List<BusinessRecord>>)lists).Count > 0 ? ((Queue<List<BusinessRecord>>)lists).Dequeue() : new List<BusinessRecord>(Count);
                if (parameter != "iterate" && list.Count == 0) list.AddRange(Records);
                if (parameter == "remove") for (int i = list.Count - 1; i >= 0; i -= 2) list.RemoveAt(i);
                return WorkResult.Scalar(unchecked((ulong)list.Sum(record => record.Id)), Count);
            case "K03": return Map(parameter);
            case "K04":
                var a = parameter.EndsWith("build-inclusive") ? Enumerable.Range(0, Count).Select(i => (long)i).ToHashSet() : setA;
                var b = parameter.EndsWith("build-inclusive") ? Enumerable.Range(Count / 2, Count).Select(i => (long)i).ToHashSet() : setB;
                var result = new HashSet<long>(a);
                if (parameter.StartsWith("union")) result.UnionWith(b); else result.IntersectWith(b);
                long setTotal = 0; foreach (long value in result) setTotal += value;
                return new(unchecked((ulong)setTotal), result.Count, "items", check ? Encoding.UTF8.GetBytes(string.Join("\n", result.Order())) : Array.Empty<byte>());
            case "K05":
                string corpusName = parameter.EndsWith("ascii") ? "ascii" : "unicode";
                byte[] corpus = corpora[corpusName]; string text = corpusText[corpusName];
                if (parameter.StartsWith("search"))
                {
                    string needle = parameter.EndsWith("ascii") ? "benchmark" : "世界";
                    ulong found = 0; for (int pos = 0; (pos = text.IndexOf(needle, pos, StringComparison.Ordinal)) >= 0; pos += needle.Length) found++;
                    return WorkResult.Scalar(found, corpus.Length, "bytes");
                }
                if (parameter.StartsWith("encode")) return WorkResult.Data(utf8.GetBytes(text), corpus.Length);
                string decoded = utf8.GetString(corpus);
                if (check && !string.Equals(decoded, text, StringComparison.Ordinal)) throw new InvalidDataException("UTF-8 decode parity");
                GC.KeepAlive(decoded); return WorkResult.Data(corpus, corpus.Length);
            case "K06":
                if (parameter == "integer-parse")
                    return WorkResult.Scalar(unchecked((ulong)numericLines.Sum(line => long.Parse(line, NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture))), Smoke ? 1000 : 100000);
                string source = corpusText["ascii"];
                return WorkResult.Scalar((ulong)tokens.Matches(source).Sum(match => match.Length), TextBytes.Length, "bytes");
            case "M01":
                var objects = new LogicalRecord[Count];
                for (int i = 0; i < objects.Length; i++) objects[i] = new LogicalRecord(i);
                ulong allocated = 0; foreach (var value in objects) allocated += (ulong)value.A;
                GC.KeepAlive(objects); return WorkResult.Scalar(allocated, Count);
            case "M02": case "M05": return Churn(parameter);
            case "M03":
                int retainedCount = Smoke ? 10000 : 1000000;
                InitializeLinked();
                ulong linkedTotal = 0;
                for (int iteration = 0; iteration < 10; iteration++) for (Link? node = linkedHead; node is not null; node = node.Next) linkedTotal += (ulong)node.Id;
                GC.KeepAlive(linkedHead); return WorkResult.Scalar(linkedTotal, retainedCount * 10L);
            case "M04":
                int bufferSize = int.Parse(parameter.Split('-')[1], CultureInfo.InvariantCulture);
                byte[] copy = RandomBytes.AsSpan(0, bufferSize).ToArray();
                return WorkResult.Scalar(copy.Aggregate(0UL, (sum, value) => sum + value), bufferSize, "bytes");
            case "S01": return Json(parameter, check);
            case "S02":
                var message = parameter == "decode" ? Benchmark.Suite.Proto.Record.Parser.ParseFrom(protobuf) : protobufObject;
                byte[] encoded = parameter == "encode" || check ? message.ToByteArray() : protobuf;
                if (check && !encoded.AsSpan().SequenceEqual(protobuf)) throw new InvalidDataException("Protobuf parity");
                GC.KeepAlive(message); GC.KeepAlive(encoded); return WorkResult.Data(protobuf, protobuf.Length);
            case "S03":
                IEnumerable<(int Key, long Total)> groups;
                if (parameter == "pipeline") groups = Records.Where(r => r.Active).Select(r => (r.Group, Value: r.Amount * 2)).GroupBy(r => r.Group).Select(g => (g.Key, g.Sum(r => r.Value))).OrderBy(g => g.Key);
                else
                {
                    var amounts = new SortedDictionary<int, long>();
                    foreach (var record in Records) if (record.Active) amounts[record.Group] = amounts.GetValueOrDefault(record.Group) + record.Amount * 2;
                    groups = amounts.Select(pair => (pair.Key, pair.Value));
                }
                return WorkResult.Data(Encoding.UTF8.GetBytes(string.Join("\n", groups.Select(g => $"{g.Key}:{g.Total}"))), Count, "records");
            case "S04":
                var rows = parameter == "parse" ? Csv.Parse(csvText) : csvRows;
                byte[] csv = parameter == "emit" || check ? utf8.GetBytes(Csv.Emit(rows)) : csvBytes;
                if (check && !csv.AsSpan().SequenceEqual(csvBytes)) throw new InvalidDataException("CSV roundtrip parity");
                GC.KeepAlive(rows); return WorkResult.Data(csv, rows.Count - 1, "rows");
            case "L01": return Representations(parameter);
            case "L02":
                ulong tagTotal = 0;
                foreach (Tagged tagged in taggedInputs)
                    tagTotal += unchecked((ulong)(parameter == "pattern" ? tagged switch { { Tag: 0 } => tagged.Value + 1, { Tag: 1 } => tagged.Value * 2, { Tag: 2 } => tagged.Value ^ 85, _ => tagged.Value - 3 }
                        : tagged.Tag switch { 0 => tagged.Value + 1, 1 => tagged.Value * 2, 2 => tagged.Value ^ 85, _ => tagged.Value - 3 }));
                return WorkResult.Scalar(tagTotal, Count);
            case "L03":
                int threshold = parameter switch { "failure-0pct" => 0, "failure-0.1pct" => 1, "failure-1pct" => 10, _ => 100 };
                ulong exceptions = 0; int failureCount = 0;
                for (int i = 0; i < Count; i++) { try { exceptions += (ulong)ExceptionFrame1(i, i % 1000 < threshold); } catch (InvalidOperationException error) { _ = error.StackTrace; exceptions++; failureCount++; } }
                return WorkResult.Scalar(exceptions, Count, details: new { failures = failureCount });
            case "L04": return Invocations(parameter);
            default: return ExecuteExternal(id, parameter, check);
        }
    }

    WorkResult Matrix(string parameter, bool check)
    {
        int n = Smoke ? 16 : parameter == "n128" ? 128 : 512;
        double[] a = new double[n * n], b = new double[n * n], c = new double[n * n];
        for (int i = 0; i < n; i++) for (int j = 0; j < n; j++) { a[i * n + j] = ((i * 17 + j * 13) % 97) / 97.0; b[i * n + j] = ((i * 7 + j * 19) % 89) / 89.0; }
        for (int i = 0; i < n; i++) for (int j = 0; j < n; j++)
        {
            double cell = 0;
            for (int k = 0; k < n; k++) cell += a[i * n + k] * b[k * n + j];
            c[i * n + j] = cell;
        }
        double sum = c.Sum();
        return WorkResult.Scalar((ulong)Math.Round(sum * 1000000, MidpointRounding.ToEven), 2L * n * n * n, "flops", check ? new { n, values = c } : null);
    }
    WorkResult Compression(string parameter, bool check)
    {
        string name = parameter.EndsWith("incompressible") ? "random-1m" : "text-1m";
        byte[] original = name == "random-1m" ? RandomBytes : TextBytes;
        byte[] compressed;
        byte[] consumed = original;
        if (parameter.StartsWith("decompress")) compressed = compressedCorpora[name];
        else
        {
            using var output = new MemoryStream();
            using (var gzip = new GZipStream(output, CompressionLevel.Optimal, true)) gzip.Write(original);
            compressed = output.ToArray();
        }
        if (check || parameter.StartsWith("decompress"))
        {
            using var input = new GZipStream(new MemoryStream(compressed), CompressionMode.Decompress);
            using var restored = new MemoryStream(); input.CopyTo(restored);
            if (check && !restored.GetBuffer().AsSpan(0, (int)restored.Length).SequenceEqual(original)) throw new InvalidDataException("Gzip roundtrip");
            if (parameter.StartsWith("decompress")) consumed = restored.ToArray();
            GC.KeepAlive(restored);
        }
        return WorkResult.Data(consumed, original.Length, details: check ? new { compressed_bytes = compressed.Length, compression_level = "Optimal (zlib default)" } : null);
    }
    WorkResult Map(string parameter)
    {
        InitializeKeys();
        bool ascii = parameter.StartsWith("ascii");
        if (ascii)
        {
            bool supplied = prepared.TryGetValue("K03" + parameter, out object? maps);
            var map = parameter.EndsWith("lookup") && supplied ? (Dictionary<string, long>)maps! : parameter.EndsWith("remove") && supplied && ((Queue<Dictionary<string, long>>)maps!).Count > 0 ? ((Queue<Dictionary<string, long>>)maps!).Dequeue() : MakeAsciiMap();
            if (parameter.EndsWith("lookup")) { ulong total = 0; for (int i = 0; i < Count * 2; i++) total += (ulong)map.GetValueOrDefault(asciiKeys![i]); return WorkResult.Scalar(total, Count * 2L); }
            if (parameter.EndsWith("remove")) for (int i = 0; i < Count; i += 2) map.Remove(asciiKeys![i]);
            return WorkResult.Scalar((ulong)map.Values.Sum(), Count);
        }
        bool suppliedNumbers = prepared.TryGetValue("K03" + parameter, out object? numberMaps);
        var numbers = parameter.EndsWith("lookup") && suppliedNumbers ? (Dictionary<long, long>)numberMaps! : parameter.EndsWith("remove") && suppliedNumbers && ((Queue<Dictionary<long, long>>)numberMaps!).Count > 0 ? ((Queue<Dictionary<long, long>>)numberMaps!).Dequeue() : MakeIntegerMap();
        if (parameter.EndsWith("lookup")) { ulong total = 0; for (int i = 0; i < Count * 2; i++) total += (ulong)numbers.GetValueOrDefault(i); return WorkResult.Scalar(total, Count * 2L); }
        if (parameter.EndsWith("remove")) for (int i = 0; i < Count; i += 2) numbers.Remove(i);
        return WorkResult.Scalar((ulong)numbers.Values.Sum(), Count);
    }
    Dictionary<string, long> MakeAsciiMap()
    {
        InitializeKeys();
        var map = new Dictionary<string, long>(Count, StringComparer.Ordinal);
        for (int i = 0; i < Count; i++) map[asciiKeys![i]] = i * 3L;
        return map;
    }
    void InitializeKeys() => asciiKeys ??= Enumerable.Range(0, Count * 2).Select(i => "k" + i.ToString("D8", CultureInfo.InvariantCulture)).ToArray();
    Dictionary<long, long> MakeIntegerMap()
    {
        var map = new Dictionary<long, long>(Count);
        for (int i = 0; i < Count; i++) map[i] = i * 3L;
        return map;
    }
    WorkResult Churn(string parameter)
    {
        int survival = parameter.Contains("50pct") ? 50 : parameter.Contains("1pct") ? 1 : 10;
        int n = Count; ulong total = 0;
        var windows = new Queue<List<LogicalRecord>>();
        for (int window = 0; window < 10; window++)
        {
            var retained = new List<LogicalRecord>();
            for (int i = 0; i < n; i++) { var record = new LogicalRecord(i); total += (ulong)record.A; if (i % (100 / survival) == 0) retained.Add(record); }
            windows.Enqueue(retained); if (windows.Count > 3) windows.Dequeue();
        }
        GC.KeepAlive(windows); return WorkResult.Scalar(total, n * 10L);
    }
    WorkResult Json(string parameter, bool check)
    {
        int size = int.Parse(parameter.Split('-')[1], CultureInfo.InvariantCulture);
        byte[] source = documents[size];
        bool serialize = parameter.StartsWith("serialize");
        DocumentModel node = serialize ? documentObjects[size] : JsonSerializer.Deserialize<DocumentModel>(source, jsonOptions)!;
        byte[] encoded = serialize || check ? JsonSerializer.SerializeToUtf8Bytes(node, jsonOptions) : source;
        if (check && !JsonNode.DeepEquals(JsonNode.Parse(source), JsonNode.Parse(encoded))) throw new InvalidDataException("JSON roundtrip");
        GC.KeepAlive(node); GC.KeepAlive(encoded);
        return new(Fnv(source), source.Length, "bytes", check ? source : Array.Empty<byte>(), check ? new { emitted_bytes = serialize ? encoded.Length : (int?)null } : null);
    }
    WorkResult Representations(string parameter)
    {
        ulong sum = 0;
        if (parameter == "primitive-array") { var a = Enumerable.Range(0, Count).Select(i => (long)i).ToArray(); foreach (long value in a) sum += (ulong)value; }
        else if (parameter == "idiomatic-record") { var a = Enumerable.Range(0, Count).Select(i => new ValueRecord(i)).ToArray(); foreach (var value in a) sum += (ulong)value.Id; }
        else if (parameter == "boxed-reference") { var a = new List<object>(Count); for (int i = 0; i < Count; i++) a.Add((long)i); foreach (object value in a) sum += (ulong)(long)value; }
        else { var a = new List<object>(Count); for (int i = 0; i < Count; i++) a.Add((long)i); foreach (object value in a) sum += (ulong)(long)value; }
        return WorkResult.Scalar(sum, Count);
    }
    WorkResult Invocations(string parameter)
    {
        ulong total = 0;
        MethodInfo cached = typeof(CoreWorkloads).GetMethod(nameof(Target))!;
        long offset = 7; Func<long, long> capture = value => Target(value) + offset;
        Func<long, long> noncapture = static value => Target(value);
        for (int i = 0; i < Count; i++)
        {
            long result;
            if (parameter == "invoke-capturing") result = capture(i);
            else if (parameter == "invoke-noncapturing") result = noncapture(i);
            else if (parameter.StartsWith("reflection-lookup")) { _ = parameter.EndsWith("uncached") ? typeof(CoreWorkloads).GetMethod(nameof(Target))! : cached; result = Target(i); }
            else result = (long)(parameter.EndsWith("uncached") ? typeof(CoreWorkloads).GetMethod(nameof(Target))! : cached).Invoke(null, new object[] { (long)i })!;
            total += (ulong)result;
        }
        return WorkResult.Scalar(total, Count);
    }
    static long ExceptionFrame1(int i, bool fail) => ExceptionFrame2(i, fail);
    static long ExceptionFrame2(int i, bool fail) => ExceptionFrame3(i, fail);
    static long ExceptionFrame3(int i, bool fail) => fail ? throw new InvalidOperationException("benchmark failure") : i;
}

public static class Csv
{
    public static List<string[]> Parse(string text)
    {
        var rows = new List<string[]>(); var row = new List<string>(); var field = new StringBuilder(); bool quoted = false, closed = false;
        for (int i = 0; i < text.Length; i++)
        {
            char c = text[i];
            if (quoted)
            {
                if (c == '"') { if (i + 1 < text.Length && text[i + 1] == '"') { field.Append('"'); i++; } else { quoted = false; closed = true; } }
                else field.Append(c);
            }
            else if (c == '"' && field.Length == 0 && !closed) quoted = true;
            else if (c == ',') { row.Add(field.ToString()); field.Clear(); closed = false; }
            else if (c == '\r' || c == '\n') { if (c == '\r' && i + 1 < text.Length && text[i + 1] == '\n') i++; row.Add(field.ToString()); field.Clear(); rows.Add(row.ToArray()); row.Clear(); closed = false; }
            else { if (closed || c == '"') throw new InvalidDataException("Malformed CSV quoting"); field.Append(c); }
        }
        if (quoted) throw new InvalidDataException("Unterminated quoted CSV field");
        if (field.Length > 0 || row.Count > 0 || closed) { row.Add(field.ToString()); rows.Add(row.ToArray()); }
        if (rows.Any(row => row.Length != 5)) throw new InvalidDataException("CSV field count");
        return rows;
    }
    public static string Emit(IEnumerable<string[]> rows) => string.Concat(rows.Select(row => string.Join(",", row.Select(field => field.IndexOfAny(new[] { ',', '"', '\r', '\n' }) >= 0 ? "\"" + field.Replace("\"", "\"\"") + "\"" : field)) + "\r\n"));
}
