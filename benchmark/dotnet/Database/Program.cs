using BenchmarkDatabase;
using System.Text.Json;

// Separate JSONL process: root engine access canonical-csv-directory.
try
{
    if (args.Length != 4) throw new ArgumentException("Expected benchmark root, engine, access, CSV directory");
    using var session = new DatabaseSession(args[0], args[1], args[2], Environment.GetEnvironmentVariable("BENCH_DB_CONNECTION"), args[3],
        (Environment.GetEnvironmentVariable("BENCH_DB_ALLOWED_HOSTS") ?? "").Split(',', StringSplitOptions.RemoveEmptyEntries),
        Environment.GetEnvironmentVariable("BENCH_DB_OWNED_NAME"), Environment.GetEnvironmentVariable("BENCH_DB_INITIALIZE") == "1");
    Console.WriteLine(JsonSerializer.Serialize(new { status = "ready", metadata = session.Metadata(), state = session.StateDigest() }));
    while (Console.ReadLine() is { } text)
    {
        try
        {
            using var json = JsonDocument.Parse(text); var command = json.RootElement.GetProperty("case_id").GetString();
            if (command == "QUIT") break;
            object result = command switch { "STATE" => session.StateDigest(), "METADATA" => session.Metadata(),
                "RESET" => session.Reset(json.RootElement.TryGetProperty("inject_failure", out var injected) && injected.GetBoolean()),
                "AUDIT" => session.Audit(json.RootElement.GetProperty("workload").GetString()!, json.RootElement.GetProperty("trace")),
                "COMMITTED" => session.CommittedOrders(json.RootElement.GetProperty("ids").EnumerateArray().Select(id => id.GetInt64())),
                "BATCH" => session.Batch(json.RootElement.GetProperty("workload").GetString()!, json.RootElement.GetProperty("trace"), json.RootElement.GetProperty("iterations").GetInt32(), json.RootElement.TryGetProperty("workers", out var workers) ? workers.GetInt32() : 1),
                "H06" => session.Checkout(DatabaseSession.ParseInput(json.RootElement.GetProperty("input"))),
                "H04" => session.Execute("D01", DatabaseSession.ParseInput(json.RootElement.GetProperty("input"))),
                "H05-read" => session.Execute("D03", DatabaseSession.ParseInput(json.RootElement.GetProperty("input"))),
                "H05-write" => session.Execute("D06", DatabaseSession.ParseInput(json.RootElement.GetProperty("input"))),
                _ => session.Execute(command!, DatabaseSession.ParseInput(json.RootElement.GetProperty("input"))) };
            Console.WriteLine(JsonSerializer.Serialize(new { status = "ok", case_id = command, result }));
        }
        catch (Exception e) { Console.WriteLine(JsonSerializer.Serialize(new { status = "error", reason = e.GetType().Name })); }
    }
    return 0;
}
catch (Exception e) { Console.WriteLine(JsonSerializer.Serialize(new { status = "blocked", reason = e.GetType().Name })); return 1; }
