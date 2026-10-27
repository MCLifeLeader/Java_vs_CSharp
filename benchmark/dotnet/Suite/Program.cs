using System.Globalization;
using System.Security.Cryptography;
using System.Text.Json;
using BenchmarkDotNet.Attributes;
using BenchmarkDotNet.Running;
using BenchmarkDotNet.Configs;
using BenchmarkDotNet.Jobs;
using BenchmarkDotNet.Toolchains.CsProj;
using BenchmarkDotNet.Toolchains.DotNetCli;
using BenchmarkDotNet.Exporters.Json;
using BenchmarkSuite;
using BenchmarkDatabase;

if (args.Contains("--harness"))
{
    int cliIndex = Array.IndexOf(args, "--cli");
    string sdk = cliIndex >= 0 && cliIndex + 1 < args.Length ? args[cliIndex + 1] :
        (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator).Select(directory => Path.Combine(directory, OperatingSystem.IsWindows() ? "dotnet.exe" : "dotnet")).First(File.Exists);
    var sdkToolchain = CsProjCoreToolchain.From(new NetCoreAppSettings("net10.0", "10.0.14", "LTS", customDotNetCliPath: sdk));
    string runtimeHost = Environment.ProcessPath is { } processPath && Path.GetFileNameWithoutExtension(processPath).Equals("dotnet", StringComparison.OrdinalIgnoreCase) ? processPath : sdk;
    var toolchain = new BenchmarkDotNet.Toolchains.Toolchain("LTS", sdkToolchain.Generator, sdkToolchain.Builder, new DotNetCliExecutor(runtimeHost));
    var config = ManualConfig.Create(DefaultConfig.Instance).AddJob(Job.Default.WithLaunchCount(5).WithWarmupCount(3).WithIterationCount(5).WithToolchain(toolchain)).AddExporter(JsonExporter.Full);
    string[] harnessArguments = args.Where((argument, index) => argument != "--harness" && index != cliIndex && (cliIndex < 0 || index != cliIndex + 1)).ToArray();
    BenchmarkSwitcher.FromTypes([typeof(SuiteHarness)]).Run(harnessArguments, config);
    return 0;
}
string Option(string name, string fallback)
{
    int index = Array.IndexOf(args, name);
    return index >= 0 && index + 1 < args.Length ? args[index + 1] : fallback;
}
string data = Option("--data", Environment.GetEnvironmentVariable("BENCH_DATA") ?? ".");
bool smoke = Option("--scale", "smoke") == "smoke";
int workers = int.Parse(Option("--workers", "1"), CultureInfo.InvariantCulture);
if (workers < 1) throw new ArgumentException("workers must be positive");
string profile = Option("--profile", "controlled");
if (args.Contains("--server"))
{
    DatabaseSession? database = null;
    if (args.Contains("--engine"))
        database = new DatabaseSession(Option("--root", "benchmark"), Option("--engine", "sqlite"), Option("--access", "raw"),
            Environment.GetEnvironmentVariable("BENCH_DB_CONNECTION"), data,
            (Environment.GetEnvironmentVariable("BENCH_DB_ALLOWED_HOSTS") ?? "").Split(',', StringSplitOptions.RemoveEmptyEntries),
            Environment.GetEnvironmentVariable("BENCH_DB_OWNED_NAME"), Environment.GetEnvironmentVariable("BENCH_DB_INITIALIZE") == "1");
    var builder = WebApplication.CreateBuilder(args);
    var httpJson = new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        UnmappedMemberHandling = System.Text.Json.Serialization.JsonUnmappedMemberHandling.Disallow,
        RespectRequiredConstructorParameters = true, RespectNullableAnnotations = true };
    var httpDocuments = new[] { 1024, 65536, 1048576 }.ToDictionary(size => size,
        size => JsonSerializer.Deserialize<DocumentModel>(File.ReadAllBytes(Path.Combine(data, $"document-{size}.json")), httpJson)!);
    var computeInput = new Lazy<long[]>(() =>
    {
        byte[] bytes = File.ReadAllBytes(Path.Combine(data, "integers.i64le"));
        if (bytes.Length == 0 || bytes.Length % 8 != 0) throw new ArgumentException("Invalid integer input");
        long[] values = new long[bytes.Length / 8];
        for (int i = 0; i < values.Length; i++) values[i] = System.Buffers.Binary.BinaryPrimitives.ReadInt64LittleEndian(bytes.AsSpan(i * 8, 8));
        return values;
    });
    DocumentModel Document(int? size) => httpDocuments.TryGetValue(size ?? 1024, out var value) ? value : throw new ArgumentException("Invalid document size");
    builder.Logging.ClearProviders();
    builder.WebHost.UseUrls(Option("--url", "http://127.0.0.1:" + Option("--port", "0")));
    var app = builder.Build();
    int activeRequests = 0;
    int acceptingRequests = 1;
    app.Use(async (context, next) =>
    {
        bool applicationRequest = !context.Request.Path.StartsWithSegments("/__benchmark");
        if (applicationRequest) Interlocked.Increment(ref activeRequests);
        try
        {
            if (applicationRequest && Volatile.Read(ref acceptingRequests) == 0) { context.Response.StatusCode = 503; return; }
            await next();
        }
        catch (Exception error)
        {
            context.Response.StatusCode = error is ArgumentException or JsonException ? 400 : error is Microsoft.EntityFrameworkCore.DbUpdateConcurrencyException or System.Data.DBConcurrencyException ? 409 : 500;
            await context.Response.WriteAsJsonAsync(new { error = context.Response.StatusCode == 400 ? "invalid_request" : context.Response.StatusCode == 409 ? "conflict" : "operation_failed" });
        }
        finally { if (applicationRequest) Interlocked.Decrement(ref activeRequests); }
    });
    app.MapGet("/__benchmark/active", (HttpContext context) =>
        context.Connection.RemoteIpAddress is { } address && System.Net.IPAddress.IsLoopback(address)
            ? Results.Json(new { active = Volatile.Read(ref activeRequests) }) : Results.StatusCode(403));
    app.MapPost("/__benchmark/quiesce", async (HttpContext context) =>
    {
        if (context.Connection.RemoteIpAddress is not { } address || !System.Net.IPAddress.IsLoopback(address)) return Results.StatusCode(403);
        Interlocked.Exchange(ref acceptingRequests, 0);
        var deadline = DateTime.UtcNow.AddSeconds(30);
        while (Volatile.Read(ref activeRequests) != 0 && DateTime.UtcNow < deadline) await Task.Delay(10);
        return Volatile.Read(ref activeRequests) == 0 ? Results.Json(new { active = 0 }) : Results.StatusCode(503);
    });
    app.MapGet("/plaintext", () => Results.Text("0123456789abcdef0123456789abcdef", "text/plain"));
    app.MapGet("/json", (int? size) => Results.Json(Document(size), httpJson));
    app.MapPost("/json", async (HttpRequest request, int? size) =>
    {
        var expected = Document(size);
        var incoming = await JsonSerializer.DeserializeAsync<DocumentModel>(request.Body, httpJson);
        if (incoming is null || incoming.Id != expected.Id || incoming.Name != expected.Name || incoming.Group != expected.Group || incoming.AmountCents != expected.AmountCents || incoming.Active != expected.Active || incoming.Padding != expected.Padding || incoming.Children is null || !incoming.Children.SequenceEqual(expected.Children))
            throw new ArgumentException("Invalid document");
        return Results.Json(incoming, httpJson);
    });
    app.MapGet("/compute", () => Results.Text(CoreWorkloads.Transform(computeInput.Value).ToString("x16"), "text/plain"));
    app.MapGet("/__benchmark/state", (HttpContext context) =>
        context.Connection.RemoteIpAddress is { } address && System.Net.IPAddress.IsLoopback(address)
            ? database is null ? Results.StatusCode(503) : Results.Json(database.StateDigest())
            : Results.StatusCode(403));
    app.MapGet("/__benchmark/metadata", (HttpContext context) =>
        context.Connection.RemoteIpAddress is { } address && System.Net.IPAddress.IsLoopback(address)
            ? database is null ? Results.StatusCode(503) : Results.Json(database.Metadata())
            : Results.StatusCode(403));
    app.MapPost("/__benchmark/committed", async (HttpContext context) =>
    {
        if (context.Connection.RemoteIpAddress is not { } address || !System.Net.IPAddress.IsLoopback(address)) return Results.StatusCode(403);
        if (database is null) return Results.StatusCode(503);
        var ids = await JsonSerializer.DeserializeAsync<long[]>(context.Request.Body) ?? throw new ArgumentException("IDs required");
        if (ids.Length > 500 || ids.Any(id => id <= 0)) throw new ArgumentException("At most 500 positive IDs required");
        return Results.Json(database.CommittedOrders(ids));
    });
    app.MapPost("/__benchmark/reset", (HttpContext context) =>
    {
        if (context.Connection.RemoteIpAddress is not { } address || !System.Net.IPAddress.IsLoopback(address)) return Results.StatusCode(403);
        if (database is null) { Interlocked.Exchange(ref acceptingRequests, 1); return Results.Json(new { status = "ready" }); }
        var state = database.Reset();
        Interlocked.Exchange(ref acceptingRequests, 1);
        return Results.Json(state);
    });
    app.MapGet("/customer/{id:long}", (long id) => database is null ? Results.StatusCode(503) : Results.Json(database.Execute("D01", new() { ["customer_id"] = id })));
    app.MapGet("/order/{id:long}", (long id) => database is null ? Results.StatusCode(503) : Results.Json(database.Execute("D03", new() { ["order_id"] = id })));
    app.MapPost("/order", async (HttpRequest request) =>
    {
        if (database is null) return Results.StatusCode(503);
        using var json = await JsonDocument.ParseAsync(request.Body);
        return Results.Json(database.Execute("D06", DatabaseSession.ParseInput(json.RootElement)), statusCode: 201);
    });
    app.MapPost("/checkout", async (HttpRequest request) =>
    {
        if (database is null) return Results.StatusCode(503);
        using var json = await JsonDocument.ParseAsync(request.Body);
        return Results.Json(database.Checkout(DatabaseSession.ParseInput(json.RootElement)), statusCode: 201);
    });
    await app.StartAsync();
    var addresses = app.Services.GetRequiredService<Microsoft.AspNetCore.Hosting.Server.IServer>().Features.Get<Microsoft.AspNetCore.Hosting.Server.Features.IServerAddressesFeature>()!;
    string address = addresses.Addresses.Single();
    Console.WriteLine(JsonSerializer.Serialize(new { status = "ready", protocol = 2, language = "csharp", url = address, port = new Uri(address).Port })); Console.Out.Flush();
    await app.WaitForShutdownAsync();
    database?.Dispose();
    return 0;
}

using var suite = new CoreWorkloads(data, smoke, workers, profile);
using var caseDefinitions = JsonDocument.Parse(File.ReadAllBytes(Path.Combine(AppContext.BaseDirectory, "cases.json")));
var allowedParameters = caseDefinitions.RootElement.GetProperty("cases").EnumerateArray().ToDictionary(
    definition => definition.GetProperty("case_id").GetString()!,
    definition => definition.GetProperty("parameters").EnumerateArray().Select(parameter => parameter.GetProperty("parameter_id").GetString()!).ToHashSet());
void Emit(object value) { Console.WriteLine(JsonSerializer.Serialize(value)); Console.Out.Flush(); }
string[] supported = { "C01", "C02", "C03", "C04", "C05", "C06", "K01", "K02", "K03", "K04", "K05", "K06", "M01", "M02", "M03", "M04", "M05", "A01", "A02", "A03", "A04", "A05", "S01", "S02", "S03", "S04", "I01", "I02", "I03", "I04", "N01", "N02", "L01", "L02", "L03", "L04" };
Emit(new { status = "ready", protocol = 2, language = "csharp", runtime = System.Runtime.InteropServices.RuntimeInformation.FrameworkDescription, supported, scale = smoke ? "smoke" : "S", workers, profile });
while (Console.ReadLine() is { } line)
{
    if (line == "QUIT") break;
    try
    {
        string[] fields = line.Split('\t');
        if (fields.Length < 3 || !supported.Contains(fields[1])) throw new ArgumentException("Unsupported command/case");
        string id = fields[1], parameter = fields[2];
        if (!allowedParameters.TryGetValue(id, out var names) || !names.Contains(parameter)) throw new ArgumentException("Unknown case parameter");
        if (fields[0] == "PREPARE")
        {
            int count = int.Parse(fields[3], CultureInfo.InvariantCulture);
            if (count < 1) throw new ArgumentException("Invalid iterations");
            suite.Prepare(id, parameter, count);
            Emit(new { status = "prepared", case_id = id, parameter_id = parameter, iterations = count });
            continue;
        }
        bool check = fields[0] == "CHECK";
        if (!check && fields[0] != "BATCH") throw new ArgumentException("Unknown protocol command");
        int iterations = check ? 1 : int.Parse(fields[3], CultureInfo.InvariantCulture);
        if (iterations < 1) throw new ArgumentException("Invalid iterations");
        ulong checksum = 0; long units = 0; WorkResult? last = null;
        for (int i = 0; i < iterations; i++) { last = suite.Execute(id, parameter, check); checksum = unchecked(checksum + last.Checksum); units += last.WorkUnits; }
        Emit(new { status = "ok", case_id = id, parameter_id = parameter, iterations, work_units = units,
            unit = last!.Unit, checksum = checksum.ToString("x16"), correctness = true,
            semantic_hash = check ? Convert.ToHexStringLower(SHA256.HashData(last.Bytes)) : null,
            details = check ? last.Details : null });
    }
    catch (Exception error)
    {
        Emit(new { status = "error", reason = error.GetType().Name, message = error is ArgumentException ? error.Message : "Operation failed; inspect private diagnostics", correctness = false });
    }
}
return 0;

[MemoryDiagnoser]
public class SuiteHarness
{
    CoreWorkloads suite = null!;
    string caseId = "", parameterId = "";
    public IEnumerable<string> Scenarios
    {
        get
        {
            using var catalog = JsonDocument.Parse(File.ReadAllBytes(Path.Combine(AppContext.BaseDirectory, "cases.json")));
            string? selectedCase = Environment.GetEnvironmentVariable("BENCH_CASE"), selectedParameter = Environment.GetEnvironmentVariable("BENCH_PARAMETER");
            foreach (var definition in catalog.RootElement.GetProperty("cases").EnumerateArray())
            {
                string id = definition.GetProperty("case_id").GetString()!;
                if (!"CKS".Contains(id[0]) || selectedCase is not null && selectedCase != id) continue;
                foreach (var parameter in definition.GetProperty("parameters").EnumerateArray())
                {
                    string name = parameter.GetProperty("parameter_id").GetString()!;
                    if (selectedParameter is null || selectedParameter == name) yield return id + "/" + name;
                }
            }
        }
    }
    [ParamsSource(nameof(Scenarios))]
    public string Scenario { get; set; } = "C01/transform";
    [GlobalSetup] public void Setup()
    {
        suite = new CoreWorkloads(Environment.GetEnvironmentVariable("BENCH_DATA") ?? throw new ArgumentException("BENCH_DATA required"), false, 1, "controlled");
        var parts = Scenario.Split('/'); caseId = parts[0]; parameterId = parts[1];
    }
    [IterationSetup] public void PrepareIteration() => suite.Prepare(caseId, parameterId, 1);
    [GlobalCleanup] public void Cleanup() => suite.Dispose();
    [Benchmark] public ulong Execute() => suite.Execute(caseId, parameterId, false).Checksum;
}
