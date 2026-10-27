using System.Data;
using System.Data.Common;
using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.Data.SqlClient;
using Microsoft.Data.Sqlite;
using Microsoft.EntityFrameworkCore;
using Microsoft.VisualBasic.FileIO;
using Npgsql;

namespace BenchmarkDatabase;

/// <summary>Stateful process owner. Each operation leases a fresh connection/context.</summary>
public sealed class DatabaseSession : IDisposable
{
    private readonly string root, engine, access, connectionString;
    private readonly DbConnection? anchor;
    private readonly Dictionary<string, string> sql = [];
    private readonly string? dataDirectory;
    private readonly bool ownsInitializedNetwork;
    private readonly Dictionary<string, string> fixtureHashes;
    private readonly StateSnapshot canonicalState;
    private volatile bool requiresReset;
    private sealed record StateSnapshot(string sha256, Dictionary<string, int> counts);
    private static readonly (string File, string Table, string[] Columns)[] Tables = [
        ("customers.csv", "customer", ["id", "email", "name", "balance_cents", "version"]),
        ("products.csv", "product", ["id", "sku", "name", "price_cents"]),
        ("orders.csv", "orders", ["id", "customer_id", "status", "created_epoch_ms", "total_cents", "version"]),
        ("order_lines.csv", "order_line", ["id", "order_id", "line_no", "product_id", "quantity", "unit_price_cents"])];
    private static object? blackhole;
    public DatabaseSession(string root, string engine, string access, string? connectionString = null,
        string? dataDirectory = null, string[]? allowedHosts = null, string? ownedDatabaseName = null,
        bool initializeNetwork = false)
    {
        this.root = Path.GetFullPath(root); this.engine = engine; this.access = access; this.dataDirectory = dataDirectory; this.ownsInitializedNetwork = engine != "sqlite" && initializeNetwork;
        if (access is not ("raw" or "orm")) throw new ArgumentException("Invalid access mode");
        if (string.IsNullOrEmpty(dataDirectory)) throw new ArgumentException("Canonical CSV directory required for verified startup");
        fixtureHashes = Tables.ToDictionary(t => t.File, t => Convert.ToHexStringLower(SHA256.HashData(File.ReadAllBytes(Path.Combine(dataDirectory, t.File)))));
        canonicalState = FixtureState(dataDirectory); VerifyFixtures();
        if (engine == "sqlite")
        {
            this.connectionString = new SqliteConnectionStringBuilder { DataSource = "bench_" + Guid.NewGuid().ToString("N"),
                Mode = SqliteOpenMode.Memory, Cache = SqliteCacheMode.Shared, Pooling = false, DefaultTimeout = 5 }.ToString();
            anchor = NewConnection(); try { anchor.Open(); Configure(anchor); } catch { anchor.Dispose(); throw; }
        }
        else
        {
            if (string.IsNullOrEmpty(connectionString)) throw new ArgumentException("Network connection environment is required");
            this.connectionString = GuardConnection(engine, connectionString, allowedHosts ?? [], ownedDatabaseName);
        }
        try
        {
        foreach (string path in Directory.GetFiles(Path.Combine(this.root, "queries", engine), "*.sql"))
            sql[Path.GetFileName(path)] = File.ReadAllText(path);
        if (engine == "sqlite" || initializeNetwork)
        {
            if (string.IsNullOrEmpty(dataDirectory)) throw new ArgumentException("Canonical CSV directory required to initialize");
            using var c = Open();
            if (engine != "sqlite")
            {
                using var exists = c.CreateCommand();
                exists.CommandText = "SELECT COUNT(*) FROM information_schema.tables WHERE table_type = 'BASE TABLE' AND table_schema = '" + (engine == "postgresql" ? "public" : "dbo") + "'";
                if (Convert.ToInt64(exists.ExecuteScalar()) != 0) throw new InvalidOperationException("Initialization requires an empty owned database");
            }
            using var tx = Begin(c);
            using var schema = c.CreateCommand(); schema.Transaction = tx; schema.CommandText = File.ReadAllText(Path.Combine(this.root, "schema", engine + ".sql")); schema.ExecuteNonQuery();
            Import(c, dataDirectory, tx); VerifyFixtures(); AssertCanonical(StateDigest(c, tx)); tx.Commit();
        }
        using (var c = Open()) { AssertCanonical(StateDigest(c)); }
        if (access == "orm") { using var c = Open(); using var context = new BenchContext(engine, c); _ = context.Model; }
        var prewarmed = new List<DbConnection>();
        try { for (int i = 0; i < (engine == "sqlite" ? 1 : 16); i++) prewarmed.Add(Open()); }
        finally { foreach (var c in prewarmed) c.Dispose(); }
        }
        catch { anchor?.Dispose(); throw; }
    }
    private static string GuardConnection(string engine, string value, string[] hosts, string? ownedName)
    {
        if (ownedName is null || !Regex.IsMatch(ownedName, "^bench_[a-f0-9]{32}$"))
            throw new ArgumentException("Exact controller-owned bench_<32-hex-token> database name required");
        if (engine == "postgresql")
        {
            var b = new NpgsqlConnectionStringBuilder(value);
            if (string.IsNullOrEmpty(b.Host) || !hosts.Contains(b.Host, StringComparer.OrdinalIgnoreCase) || b.Database != ownedName)
                throw new ArgumentException("Network target is not explicitly allowlisted and owned");
            VerifyLease(engine, b.Host, ownedName);
            b.MaxPoolSize = 16; b.MinPoolSize = 0; b.Timeout = 15; b.CommandTimeout = 15; b.MaxAutoPrepare = 0;
            return b.ToString();
        }
        if (engine == "sqlserver")
        {
            var b = new SqlConnectionStringBuilder(value);
            string host = b.DataSource.Replace("tcp:", "", StringComparison.OrdinalIgnoreCase).Split(',')[0];
            if (!hosts.Contains(host, StringComparer.OrdinalIgnoreCase) || b.InitialCatalog != ownedName)
                throw new ArgumentException("Network target is not explicitly allowlisted and owned");
            VerifyLease(engine, host, ownedName);
            b.MaxPoolSize = 16; b.MinPoolSize = 0; b.ConnectTimeout = 15; b.ConnectRetryCount = 0;
            return b.ToString();
        }
        throw new ArgumentException("Unknown engine");
    }
    private static void VerifyLease(string engine, string host, string database)
    {
        string token = Environment.GetEnvironmentVariable("BENCH_DB_OWNER_TOKEN") ?? "";
        string path = Environment.GetEnvironmentVariable("BENCH_DB_OWNERSHIP_FILE") ?? "";
        if (!Regex.IsMatch(token, "^[a-f0-9]{32}$") || !Path.IsPathFullyQualified(path)) throw new ArgumentException("Controller ownership lease required");
        using var lease = JsonDocument.Parse(File.ReadAllText(path)); var r = lease.RootElement;
        if (r.GetProperty("status").GetString() != "active" || r.GetProperty("owner_token").GetString() != token || r.GetProperty("engine").GetString() != engine ||
            !r.GetProperty("allowed_hosts").EnumerateArray().Any(h => string.Equals(h.GetString(), host, StringComparison.OrdinalIgnoreCase)) ||
            !r.GetProperty("databases").EnumerateArray().Any(d => d.GetString() == database)) throw new ArgumentException("Controller ownership lease does not match target");
    }
    private DbConnection NewConnection() => engine switch
    {
        "sqlite" => new SqliteConnection(connectionString), "postgresql" => new NpgsqlConnection(connectionString),
        "sqlserver" => new SqlConnection(connectionString), _ => throw new ArgumentException("Unknown engine")
    };
    private DbConnection Open() { var c = NewConnection(); c.Open(); Configure(c); return c; }
    private void Configure(DbConnection c)
    {
        if (engine != "sqlite") return;
        using var cmd = c.CreateCommand(); cmd.CommandText = "PRAGMA foreign_keys=ON; PRAGMA read_uncommitted=0; PRAGMA busy_timeout=5000;"; cmd.ExecuteNonQuery();
    }
    private DbTransaction Begin(DbConnection c) => c.BeginTransaction(engine == "sqlite" ? IsolationLevel.Serializable : IsolationLevel.ReadCommitted);
    private DbCommand Command(DbConnection c, string name, Dictionary<string, object?> parameters, DbTransaction? tx = null)
    {
        var cmd = c.CreateCommand(); cmd.Transaction = tx; cmd.CommandTimeout = 15;
        cmd.CommandText = Regex.Replace(sql.TryGetValue(name, out var source) ? source : name, @":([a-z_]+)", "@$1");
        foreach (Match match in Regex.Matches(cmd.CommandText, @"@([a-z_]+)"))
        {
            string key = match.Groups[1].Value;
            if (cmd.Parameters.Contains("@" + key)) continue;
            var p = cmd.CreateParameter(); p.ParameterName = "@" + key;
            p.Value = parameters.TryGetValue(key, out var value) ? value ?? DBNull.Value : throw new ArgumentException("Missing SQL parameter " + key);
            if (value is string) { p.DbType = DbType.AnsiString; p.Size = key == "email" ? 254 : key == "sku" ? 64 : 128; }
            else p.DbType = DbType.Int64;
            cmd.Parameters.Add(p);
        }
        return cmd;
    }
    private List<Dictionary<string, object?>> Rows(DbConnection c, string name, Dictionary<string, object?> p, DbTransaction? tx = null)
    {
        using var cmd = Command(c, name, p, tx); using var r = cmd.ExecuteReader(); var rows = new List<Dictionary<string, object?>>();
        while (r.Read()) { var row = new Dictionary<string, object?>(); for (int i = 0; i < r.FieldCount; i++) row[r.GetName(i)] = r.IsDBNull(i) ? null : r.GetValue(i); rows.Add(row); }
        return rows;
    }
    private int Change(DbConnection c, string name, Dictionary<string, object?> p, DbTransaction? tx)
    { using var cmd = Command(c, name, p, tx); return cmd.ExecuteNonQuery(); }
    private static Dictionary<string, object?> Map(params (string, object?)[] pairs) => pairs.ToDictionary(x => x.Item1, x => x.Item2);
    private static long Num(Dictionary<string, object?> p, string key) => p.TryGetValue(key, out var v) ? Convert.ToInt64(v) : throw new ArgumentException("Missing input " + key);
    private static Dictionary<string, object?> CustomerMap(Customer c) => Map(("id", c.Id), ("email", c.Email), ("name", c.Name), ("balance_cents", c.BalanceCents), ("version", c.Version));
    private static Dictionary<string, object?> LineMap(OrderLine l) => Map(("id", l.Id), ("order_id", l.OrderId), ("line_no", l.LineNo), ("product_id", l.ProductId), ("quantity", l.Quantity), ("unit_price_cents", l.UnitPriceCents));
    private static Dictionary<string, object?> OrderMap(Order o) => Map(("id", o.Id), ("customer_id", o.CustomerId), ("status", o.Status), ("created_epoch_ms", o.CreatedEpochMs), ("total_cents", o.TotalCents), ("version", o.Version));
    private static Dictionary<string, object?> GraphMap(Order o) { var d = OrderMap(o); d["customer"] = CustomerMap(o.Customer); d["lines"] = o.Lines.OrderBy(x => x.LineNo).Select(LineMap).ToArray(); return d; }
    private static Dictionary<string, object?> RawGraph(List<Dictionary<string, object?>> rows)
    {
        if (rows.Count != 5) throw new InvalidOperationException("Order must contain exactly five lines"); var x = rows[0];
        return Map(("id", x["order_id"]), ("customer_id", x["customer_id"]), ("status", x["status"]), ("created_epoch_ms", x["created_epoch_ms"]),
            ("total_cents", x["total_cents"]), ("version", x["order_version"]),
            ("customer", Map(("id", x["customer_record_id"]), ("email", x["email"]), ("name", x["customer_name"]), ("balance_cents", x["balance_cents"]), ("version", x["customer_version"]))),
            ("lines", rows.Select(l => Map(("id", l["line_id"]), ("order_id", l["line_order_id"]), ("line_no", l["line_no"]), ("product_id", l["product_id"]), ("quantity", l["quantity"]), ("unit_price_cents", l["unit_price_cents"]))).ToArray()));
    }
    public object Execute(string caseId, Dictionary<string, object?> p)
    {
        if (requiresReset) throw new InvalidOperationException("A failed reset requires canonical recovery before operations resume");
        if (caseId == "D08")
        {
            if (p["trace"] is not JsonElement trace || trace.ValueKind != JsonValueKind.Array) throw new ArgumentException("D08 requires canonical trace array");
            var ops = trace.EnumerateArray().ToArray(); var expected = new Dictionary<string, int> { ["D01"] = 60, ["D02"] = 15, ["D05"] = 10, ["D06"] = 10, ["D07"] = 5 };
            if (ops.Length == 0 || ops.Length % 20 != 0 || expected.Any(e => ops.Count(o => o.GetProperty("case_id").GetString() == e.Key) * 100 != ops.Length * e.Value)) throw new ArgumentException("D08 proportions differ from fixed contract");
            var results = new List<object>();
            foreach (var op in ops) { try { results.Add(Execute(op.GetProperty("case_id").GetString()!, ParseInput(op.GetProperty("input")))); } catch (Exception e) { results.Add(new { status = "error", category = FailureCategory(e) }); } }
            return results;
        }
        using var c = Open();
        return access == "orm" ? Orm(c, caseId, p) : Raw(c, caseId, p);
    }
    public object Audit(string workload, JsonElement trace)
    {
        if (trace.ValueKind != JsonValueKind.Array || trace.GetArrayLength() is < 1 or > 1000) throw new ArgumentException("AUDIT requires1..1000 serial operations");
        var operations = trace.EnumerateArray().Select(op => (Case: op.TryGetProperty("case_id", out var id) ? id.GetString()! : workload,
            Input: ParseInput(op.TryGetProperty("input", out var input) ? input : op))).ToArray();
        if (operations.Any(op => op.Case is not ("D01" or "D02" or "D03" or "D04" or "D05" or "D06" or "D07" or "H06"))) throw new ArgumentException("AUDIT requires individual database operations");
        var results = new List<object>();
        foreach (var op in operations) results.Add(new { case_id = op.Case, result = op.Case == "H06" ? Checkout(op.Input) : Execute(op.Case, op.Input) });
        return new { operations = operations.Length, results };
    }
    public long[] CommittedOrders(IEnumerable<long> ids)
    {
        long[] keys = ids.Distinct().Order().ToArray();
        if (keys.Any(id => id <= 0)) throw new ArgumentException("Positive order IDs required");
        var found = new SortedSet<long>();
        using var connection = Open();
        foreach (long[] chunk in keys.Chunk(500))
        {
            using var command = connection.CreateCommand(); command.CommandTimeout = 15;
            command.CommandText = "SELECT id FROM orders WHERE id IN (" + string.Join(',', Enumerable.Range(0, chunk.Length).Select(i => "@p" + i)) + ")";
            for (int i = 0; i < chunk.Length; i++) { var parameter = command.CreateParameter(); parameter.ParameterName = "@p" + i; parameter.DbType = DbType.Int64; parameter.Value = chunk[i]; command.Parameters.Add(parameter); }
            using var reader = command.ExecuteReader(); while (reader.Read()) found.Add(reader.GetInt64(0));
        }
        return found.ToArray();
    }
    public object Batch(string workload, JsonElement trace, int iterations, int workers = 1)
    {
        if (trace.ValueKind != JsonValueKind.Array || trace.GetArrayLength() == 0 || iterations <= 0) throw new ArgumentException("Nonempty canonical trace and positive iterations required");
        if (workers is not (1 or 8 or 32) || (engine == "sqlite" && workers != 1)) throw new ArgumentException("Network workers must be 1/8/32; SQLite requires one worker");
        var operations = trace.EnumerateArray().Select((e, index) => (Case: e.TryGetProperty("case_id", out var id) ? id.GetString()! : workload,
            Input: ParseInput(e.TryGetProperty("input", out var input) ? input : e), Worker: (e.TryGetProperty("worker_partition", out var partition) ? partition.GetInt32() : index) % workers)).ToArray();
        if (iterations != 1 && operations.Any(o => o.Case is "D05" or "D06" or "D07" or "D08" or "H06")) throw new ArgumentException("Finite mutation traces cannot wrap");
        if (workload == "D08") { var mix = new Dictionary<string, int> { ["D01"] = 60, ["D02"] = 15, ["D05"] = 10, ["D06"] = 10, ["D07"] = 5 }; if (operations.Length % 20 != 0 || mix.Any(e => operations.Count(o => o.Case == e.Key) * 100 != operations.Length * e.Value)) throw new ArgumentException("D08 batch proportions differ"); }
        var owners = new Dictionary<long, int>(); var inserts = new HashSet<long>(); var deletes = new HashSet<long>();
        foreach (var op in operations)
        {
            if (op.Worker < 0) throw new ArgumentException("Negative worker partition");
            if (op.Case is "D05" or "H06") { long customer = Num(op.Input, "customer_id"); if (owners.TryGetValue(customer, out int owner) && owner != op.Worker) throw new ArgumentException("Write trace crosses customer worker ownership"); owners[customer] = op.Worker; }
            if ((op.Case is "D06" or "H06") && !inserts.Add(Num(op.Input, "order_id"))) throw new ArgumentException("Duplicate finite insert trace");
            if (op.Case == "D07" && !deletes.Add(Num(op.Input, "order_id"))) throw new ArgumentException("Duplicate finite delete trace");
        }
        var latencies = new long[checked(operations.Length * iterations)]; var failures = new System.Collections.Concurrent.ConcurrentDictionary<string, long>(); long successes = 0;
        var jobs = Enumerable.Range(0, workers).Select(worker => Enumerable.Range(0, operations.Length).Where(index => operations[index].Worker == worker).ToArray()).ToArray();
        using var ready = new CountdownEvent(workers); using var startGate = new ManualResetEventSlim(false);
        var threads = Enumerable.Range(0, workers).Select(worker => new Thread(() =>
        {
            ready.Signal(); startGate.Wait();
            for (int repeat = 0; repeat < iterations; repeat++) foreach (int index in jobs[worker])
            {
                var op = operations[index]; long start = Stopwatch.GetTimestamp();
                try { object result = op.Case == "H06" ? Checkout(op.Input) : Execute(op.Case, op.Input); Volatile.Write(ref blackhole, result); Interlocked.Increment(ref successes); }
                catch (Exception e) { failures.AddOrUpdate(FailureCategory(e), 1, (_, count) => count + 1); }
                finally { latencies[repeat * operations.Length + index] = (long)(Stopwatch.GetElapsedTime(start).TotalSeconds * 1_000_000_000); }
            }
        })).ToArray();
        foreach (var thread in threads) thread.Start(); ready.Wait(); var wall = Stopwatch.StartNew(); startGate.Set(); foreach (var thread in threads) thread.Join(); wall.Stop();
        return new { attempts = latencies.Length, successes, categorized_failures = failures, latencies_ns = latencies, wall_seconds = wall.Elapsed.TotalSeconds, workers,
            last_result = Volatile.Read(ref blackhole), timing_boundary = "fixed platform workers; connection lease through context/transaction/result consumption/disposal; failures included", admitted = false };
    }
    private static string FailureCategory(Exception e) => e is DbUpdateException { InnerException: { } inner } ? FailureCategory(inner) : e switch
    {
        DBConcurrencyException or DbUpdateConcurrencyException => "conflict",
        SqliteException { SqliteErrorCode: 5 } => "busy", SqliteException { SqliteErrorCode: 6 } => "locked",
        SqliteException { SqliteErrorCode: 19 } => "constraint", SqlException { Number: 2627 or 2601 or 547 } => "constraint",
        PostgresException { SqlState: "23505" or "23503" or "23514" } => "constraint",
        SqlException { Number: 1205 } => "deadlock", SqlException { Number: -2 } or TimeoutException => "timeout",
        PostgresException { SqlState: "40P01" } => "deadlock", PostgresException { SqlState: "40001" } => "conflict",
        _ => e.GetType().Name
    };
    public object Reset() => Reset(false);
    public object Reset(bool injectFailure)
    {
        if (engine != "sqlite" && !ownsInitializedNetwork) throw new InvalidOperationException("Reset requires this session to have initialized the empty allowlisted controller-owned database");
        if (engine != "sqlite") _ = GuardConnection(engine, connectionString, (Environment.GetEnvironmentVariable("BENCH_DB_ALLOWED_HOSTS") ?? "").Split(','), Environment.GetEnvironmentVariable("BENCH_DB_OWNED_NAME"));
        requiresReset = true; VerifyFixtures();
        using var c = Open(); using var tx = Begin(c);
        foreach (string table in new[] { "order_line", "orders", "product", "customer" }) { using var cmd = c.CreateCommand(); cmd.Transaction = tx; cmd.CommandText = "DELETE FROM " + table; cmd.ExecuteNonQuery(); }
        if (injectFailure) throw new InvalidOperationException("Injected reset rollback after delete");
        Import(c, dataDirectory!, tx); VerifyFixtures(); var restored = StateDigest(c, tx); AssertCanonical(restored); tx.Commit(); requiresReset = false; return restored;
    }
    private object Raw(DbConnection c, string id, Dictionary<string, object?> p)
    {
        if (id == "D01") return Rows(c, "D01.sql", p).Single();
        if (id == "D02") return Rows(c, "D02.sql", p);
        if (id == "D03") return RawGraph(Rows(c, "D03.sql", p));
        if (id == "D04") return Rows(c, "D04.sql", p);
        using var tx = Begin(c);
        try
        {
            object result;
            if (id == "D05")
            {
                var row = Rows(c, "D05-read.sql", p, tx).Single(); long old = Convert.ToInt64(row["version"]);
                var update = new Dictionary<string, object?>(p) { ["new_balance_cents"] = checked(Convert.ToInt64(row["balance_cents"]) + Num(p, "delta_cents")), ["old_version"] = p.GetValueOrDefault("expected_version", old), ["new_version"] = checked(old + 1) };
                if (Change(c, "D05-update.sql", update, tx) != 1) throw new DBConcurrencyException();
                row["balance_cents"] = update["new_balance_cents"]; row["version"] = update["new_version"]; result = row;
            }
            else if (id == "D06") result = InsertRaw(c, tx, p);
            else if (id == "D07")
            {
                var rows = Rows(c, "D07-load.sql", p, tx); if (rows.Count != 5) throw new InvalidOperationException("Expected five-line graph");
                foreach (var row in rows) if (Change(c, "D07-child.sql", Map(("line_id", row["line_id"]), ("order_id", row["order_id"])), tx) != 1) throw new DBConcurrencyException();
                var deletion = new Dictionary<string, object?>(p) { ["old_version"] = p.GetValueOrDefault("expected_version", rows[0]["order_version"]) };
                if (Change(c, "D07-parent.sql", deletion, tx) != 1) throw new DBConcurrencyException();
                result = Map(("order_id", Num(p, "order_id")), ("deleted_lines", 5L), ("deleted", true));
            }
            else throw new ArgumentException("Unknown database case");
            if (p.GetValueOrDefault("inject_failure") is true) throw new InvalidOperationException("Injected rollback");
            tx.Commit(); return result;
        }
        catch { tx.Rollback(); throw; }
    }
    private Order NewOrder(Dictionary<string, object?> p)
    {
        if (p["lines"] is not JsonElement lines || lines.ValueKind != JsonValueKind.Array || lines.GetArrayLength() != 5) throw new ArgumentException("Exactly five canonical lines required");
        var order = new Order { Id = Num(p, "order_id"), CustomerId = Num(p, "customer_id"), Status = Num(p, "status"), CreatedEpochMs = Num(p, "created_epoch_ms"), TotalCents = Num(p, "total_cents"), Version = 0 };
        foreach (var l in lines.EnumerateArray()) order.Lines.Add(new OrderLine { Id = l.GetProperty("line_id").GetInt64(), OrderId = order.Id, LineNo = l.GetProperty("line_no").GetInt64(), ProductId = l.GetProperty("product_id").GetInt64(), Quantity = l.GetProperty("quantity").GetInt64(), UnitPriceCents = l.GetProperty("unit_price_cents").GetInt64() });
        if (order.Id <= 0 || order.CustomerId <= 0 || order.Status < 0 || order.Status > 3 || order.CreatedEpochMs < 0 || order.Lines.Any(l => l.Id <= 0 || l.ProductId <= 0 || l.Quantity <= 0 || l.UnitPriceCents < 0) || !order.Lines.Select(l => l.LineNo).Order().SequenceEqual(new long[] { 1, 2, 3, 4, 5 })) throw new ArgumentException("Invalid order values");
        long total = 0; foreach (var l in order.Lines) total = checked(total + checked(l.Quantity * l.UnitPriceCents));
        if (total != order.TotalCents) throw new ArgumentException("Order total differs from lines");
        return order;
    }
    private object InsertRaw(DbConnection c, DbTransaction tx, Dictionary<string, object?> p)
    {
        var o = NewOrder(p); var fields = OrderMap(o); fields["order_id"] = o.Id;
        if (Change(c, "D06-order.sql", fields, tx) != 1) throw new InvalidOperationException();
        var first = LineMap(o.Lines[0]); first["line_id"] = o.Lines[0].Id;
        using var lineCommand = Command(c, "D06-line.sql", first, tx);
        foreach (var line in o.Lines)
        {
            var f = LineMap(line); f["line_id"] = line.Id;
            foreach (DbParameter parameter in lineCommand.Parameters) parameter.Value = f[parameter.ParameterName.TrimStart('@')]!;
            if (lineCommand.ExecuteNonQuery() != 1) throw new InvalidOperationException();
        }
        var result = OrderMap(o); result["lines"] = o.Lines.OrderBy(l => l.LineNo).Select(LineMap).ToArray(); return result;
    }
    private object Orm(DbConnection c, string id, Dictionary<string, object?> p)
    {
        using var db = new BenchContext(engine, c);
        if (id == "D01") { long key = Num(p, "customer_id"); return CustomerMap(db.Customers.Single(x => x.Id == key)); }
        if (id == "D02") { long status = Num(p, "status"), epoch = Num(p, "cursor_created_epoch_ms"), key = Num(p, "cursor_id"); return db.Orders.Where(x => x.Status == status && (x.CreatedEpochMs > epoch || (x.CreatedEpochMs == epoch && x.Id > key))).OrderBy(x => x.CreatedEpochMs).ThenBy(x => x.Id).Take(50).ToList().Select(OrderMap).ToArray(); }
        if (id == "D03") { long key = Num(p, "order_id"); var order = db.Orders.Include(x => x.Customer).Include(x => x.Lines).AsSingleQuery().Single(x => x.Id == key); if (order.Lines.Count != 5) throw new InvalidOperationException(); return GraphMap(order); }
        if (id == "D04") { long start = Num(p, "start_epoch_ms"), end = Num(p, "end_epoch_ms"); return db.Orders.Where(x => x.CreatedEpochMs >= start && x.CreatedEpochMs < end).GroupBy(x => x.Status).OrderBy(x => x.Key).Select(g => new { status = g.Key, order_count = g.LongCount(), total_cents = g.Sum(x => x.TotalCents) }).ToArray(); }
        using var tx = db.Database.BeginTransaction(engine == "sqlite" ? IsolationLevel.Serializable : IsolationLevel.ReadCommitted);
        try
        {
            object result;
            if (id == "D05")
            {
                long key = Num(p, "customer_id"); var customer = db.Customers.Single(x => x.Id == key);
                if (p.ContainsKey("expected_version")) db.Entry(customer).Property(x => x.Version).OriginalValue = Num(p, "expected_version");
                customer.BalanceCents = checked(customer.BalanceCents + Num(p, "delta_cents")); customer.Version = checked(customer.Version + 1);
                db.SaveChanges(); result = CustomerMap(customer);
            }
            else if (id == "D06") { var order = NewOrder(p); db.Orders.Add(order); db.SaveChanges(); var r = OrderMap(order); r["lines"] = order.Lines.OrderBy(l => l.LineNo).Select(LineMap).ToArray(); result = r; }
            else if (id == "D07")
            {
                long key = Num(p, "order_id"); var order = db.Orders.Include(x => x.Customer).Include(x => x.Lines).AsSingleQuery().Single(x => x.Id == key);
                if (order.Lines.Count != 5) throw new InvalidOperationException();
                if (p.ContainsKey("expected_version")) db.Entry(order).Property(x => x.Version).OriginalValue = Num(p, "expected_version");
                db.Lines.RemoveRange(order.Lines); db.Orders.Remove(order); db.SaveChanges(); result = Map(("order_id", key), ("deleted_lines", 5L), ("deleted", true));
            }
            else throw new ArgumentException("Unknown database case");
            if (p.GetValueOrDefault("inject_failure") is true) throw new InvalidOperationException("Injected rollback");
            tx.Commit(); return result;
        }
        catch { tx.Rollback(); throw; }
    }
    public object Checkout(Dictionary<string, object?> p)
    {
        if (requiresReset) throw new InvalidOperationException("A failed reset requires canonical recovery before operations resume");
        using var c = Open();
        if (access == "orm")
        {
            using var db = new BenchContext(engine, c); using var tx = db.Database.BeginTransaction(engine == "sqlite" ? IsolationLevel.Serializable : IsolationLevel.ReadCommitted);
            var order = NewOrder(p); var customer = db.Customers.Single(x => x.Id == order.CustomerId);
            foreach (var line in order.Lines) { var product = db.Products.Single(x => x.Id == line.ProductId); if (line.UnitPriceCents != product.PriceCents) throw new ArgumentException("Price mismatch"); }
            customer.BalanceCents = checked(customer.BalanceCents - order.TotalCents); customer.Version = checked(customer.Version + 1);
            db.Orders.Add(order); db.SaveChanges(); if (p.GetValueOrDefault("inject_failure") is true) throw new InvalidOperationException("Injected rollback"); tx.Commit();
            return Map(("id", order.Id), ("total_cents", order.TotalCents), ("version", customer.Version));
        }
        using var rawTx = Begin(c); var newOrder = NewOrder(p); var row = Rows(c, "D01.sql", Map(("customer_id", newOrder.CustomerId)), rawTx).Single();
        foreach (var line in newOrder.Lines) { var product = Rows(c, "H06-product.sql", Map(("product_id", line.ProductId)), rawTx).Single(); if (line.UnitPriceCents != Convert.ToInt64(product["price_cents"])) throw new ArgumentException("Price mismatch"); }
        InsertRaw(c, rawTx, p); long version = checked(Convert.ToInt64(row["version"]) + 1);
        var update = Map(("customer_id", newOrder.CustomerId), ("old_version", row["version"]), ("new_version", version), ("new_balance_cents", checked(Convert.ToInt64(row["balance_cents"]) - newOrder.TotalCents)));
        if (Change(c, "D05-update.sql", update, rawTx) != 1) throw new DBConcurrencyException();
        if (p.GetValueOrDefault("inject_failure") is true) throw new InvalidOperationException("Injected rollback"); rawTx.Commit(); return Map(("id", newOrder.Id), ("total_cents", newOrder.TotalCents), ("version", version));
    }
    private static IEnumerable<Dictionary<string, object?>> FixtureRows(string directory, string file, string[] columns)
    {
            using var parser = new TextFieldParser(Path.Combine(directory, file), Encoding.UTF8) { HasFieldsEnclosedInQuotes = true, TrimWhiteSpace = false };
            parser.SetDelimiters(","); if (!(parser.ReadFields() ?? []).SequenceEqual(columns)) throw new InvalidDataException("CSV header differs from contract");
            long previous = 0;
            while (!parser.EndOfData)
            {
                var fields = parser.ReadFields()!; if (fields.Length != columns.Length) throw new InvalidDataException("CSV field count differs");
                var values = new Dictionary<string, object?>();
                for (int i = 0; i < columns.Length; i++)
                {
                    if (columns[i] is "email" or "name" or "sku")
                    {
                        int max = columns[i] == "email" ? 254 : columns[i] == "sku" ? 64 : 128;
                        if (fields[i].Length is 0 || fields[i].Length > max || fields[i].EndsWith(' ') || fields[i].Any(ch => ch < 32 || ch > 126)) throw new InvalidDataException("Noncanonical ASCII field");
                        values[columns[i]] = fields[i];
                    }
                    else values[columns[i]] = long.Parse(fields[i], System.Globalization.CultureInfo.InvariantCulture);
                }
                long id = Convert.ToInt64(values["id"]); if (id <= previous) throw new InvalidDataException("Canonical CSV IDs must be positive and strictly ascending"); previous = id;
                yield return values;
            }
    }
    private static StateSnapshot FixtureState(string directory)
    {
        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256); var counts = new Dictionary<string, int>();
        foreach (var (file, table, columns) in Tables) { int count = 0; foreach (var values in FixtureRows(directory, file, columns)) { AppendRow(hash, table, columns.Select(column => values[column])); count++; } counts[table] = count; }
        return new(Convert.ToHexStringLower(hash.GetHashAndReset()), counts);
    }
    private void VerifyFixtures()
    {
        foreach (var (file, expected) in fixtureHashes) if (Convert.ToHexStringLower(SHA256.HashData(File.ReadAllBytes(Path.Combine(dataDirectory!, file)))) != expected) throw new InvalidDataException("Canonical CSV changed during database session");
    }
    private void AssertCanonical(StateSnapshot actual)
    {
        if (actual.sha256 != canonicalState.sha256 || actual.counts.Any(pair => canonicalState.counts[pair.Key] != pair.Value)) throw new InvalidDataException("Database state differs from canonical CSV snapshot");
    }
    private static void AppendRow(IncrementalHash hash, string table, IEnumerable<object?> values) => hash.AppendData(Encoding.UTF8.GetBytes(table + "\t" + string.Join('\t', values.Select(value => Convert.ToString(value, System.Globalization.CultureInfo.InvariantCulture))) + "\n"));
    private void Import(DbConnection c, string directory, DbTransaction tx)
    {
        foreach (var (file, table, columns) in Tables) { string statement = $"INSERT INTO {table} ({string.Join(',', columns)}) VALUES ({string.Join(',', columns.Select(x => ':' + x))})"; foreach (var values in FixtureRows(directory, file, columns)) Change(c, statement, values, tx); }
    }
    public object StateDigest()
    {
        using var c = Open(); return StateDigest(c);
    }
    private static StateSnapshot StateDigest(DbConnection c, DbTransaction? tx = null)
    {
        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256); var counts = new Dictionary<string, int>();
        foreach (var (_, table, columns) in Tables)
        {
            int count = 0; using var cmd = c.CreateCommand(); cmd.Transaction = tx; cmd.CommandText = $"SELECT {string.Join(',', columns)} FROM {table} ORDER BY id"; using var rows = cmd.ExecuteReader();
            while (rows.Read()) { AppendRow(hash, table, Enumerable.Range(0, rows.FieldCount).Select(i => rows.GetValue(i))); count++; }
            counts[table] = count;
        }
        return new(Convert.ToHexStringLower(hash.GetHashAndReset()), counts);
    }
    public object Metadata()
    {
        using var c = Open(); var m = Map(("engine", engine), ("access", access), ("orm", typeof(DbContext).Assembly.GetName().Version?.ToString()), ("driver", c.GetType().Assembly.GetName().Version?.ToString()), ("network_verification", engine == "sqlite" ? "not applicable" : "connected"));
        m["server_version"] = c.ServerVersion; m["schema_sha256"] = Convert.ToHexStringLower(SHA256.HashData(File.ReadAllBytes(Path.Combine(root, "schema", engine + ".sql"))));
        m["query_sha256"] = sql.OrderBy(x => x.Key).ToDictionary(x => x.Key, x => Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(x.Value))));
        m["pool_limit"] = engine == "sqlite" ? 1 : 16; m["connection_acquisition_timeout_seconds"] = 15; m["command_timeout_seconds"] = 15;
        m["orm_policy"] = "fresh tracked DbContext per operation, no context/result caches, no lazy loading, network MaxBatchSize=1";
        m["native_parity_admission"] = engine == "sqlite" ? "blocked until cross-language native version/build comparison" : "requires same server/image/settings evidence";
        if (engine == "sqlite")
        {
            using var cmd = c.CreateCommand(); cmd.CommandText = "SELECT sqlite_version(), sqlite_source_id()"; using (var r = cmd.ExecuteReader()) { r.Read(); m["sqlite_version"] = r.GetString(0); m["sqlite_source_id"] = r.GetString(1); }
            cmd.CommandText = "PRAGMA compile_options"; using (var r = cmd.ExecuteReader()) { var opts = new List<string>(); while (r.Read()) opts.Add(r.GetString(0)); m["compile_options"] = opts; }
            m["native_libraries"] = Process.GetCurrentProcess().Modules.Cast<ProcessModule>().Where(x => Path.GetFileNameWithoutExtension(x.ModuleName).Equals("e_sqlite3", StringComparison.OrdinalIgnoreCase)).Select(x => new { name = x.ModuleName, sha256 = Convert.ToHexStringLower(SHA256.HashData(File.ReadAllBytes(x.FileName))) }).ToArray();
            m["build_parity"] = "compare with Java metadata before admission";
        }
        return m;
    }
    public static Dictionary<string, object?> ParseInput(JsonElement e) => e.EnumerateObject().ToDictionary(p => p.Name, p => p.Value.ValueKind switch { JsonValueKind.Number => (object?)p.Value.GetInt64(), JsonValueKind.String => p.Value.GetString(), JsonValueKind.True => true, JsonValueKind.False => false, _ => p.Value.Clone() });
    public void Dispose() { anchor?.Dispose(); }
}
