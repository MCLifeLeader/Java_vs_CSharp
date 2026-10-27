using System.Collections.Concurrent;
using System.Net.Http;
using System.Text.Json;

namespace BenchmarkSuite;

public sealed partial class CoreWorkloads
{
    readonly Dictionary<string, object> prepared = new();
    HttpClient? delayClient;
    PlatformPool? platformPool;
    string? delayUrl, delayExpectedBody;
    public void Prepare(string id, string parameter, int iterations)
    {
        if (id == "M03") InitializeLinked();
        if (id.StartsWith("N")) InitializeNative();
        if (id is "I01" or "I02") PrepareIoFile();
        if (id == "I03") plainEcho ??= OpenEcho(false);
        if (id == "I04") InitializeTlsTrust();
        if (id == "I04" && parameter == "persistent-transfer") secureEcho ??= OpenEcho(true);
        if (id == "A04") InitializeDelay();
        if (id is "A01" or "A03") platformPool ??= new PlatformPool(Workers);
        if (id == "K03") InitializeKeys();
        if (id == "K02" && parameter == "remove")
        {
            if ((long)Count * 8 * iterations > preparationBudget) throw new ArgumentException("Prepared lists exceed the declared memory budget");
            prepared[id + parameter] = new Queue<List<BusinessRecord>>(Enumerable.Range(0, iterations).Select(_ => new List<BusinessRecord>(Records)));
        }
        if (id == "K03" && parameter.EndsWith("lookup"))
            prepared[id + parameter] = parameter.StartsWith("ascii") ? MakeAsciiMap() : MakeIntegerMap();
        if (id == "K03" && parameter.EndsWith("remove"))
        {
            if ((long)Count * 128 * iterations > preparationBudget) throw new ArgumentException("Prepared maps exceed the declared memory budget");
            prepared[id + parameter] = parameter.StartsWith("ascii")
                ? new Queue<Dictionary<string, long>>(Enumerable.Range(0, iterations).Select(_ => MakeAsciiMap()))
                : new Queue<Dictionary<long, long>>(Enumerable.Range(0, iterations).Select(_ => MakeIntegerMap()));
        }
        if (id == "K01" && parameter == "sort")
        {
            if ((long)Integers.Length * 8 * iterations > preparationBudget) throw new ArgumentException("Prepared sort arrays exceed the declared memory budget");
            prepared[id + parameter] = new Queue<long[]>(Enumerable.Range(0, iterations).Select(_ => (long[])Integers.Clone()));
        }
    }
    void InitializeLinked()
    {
        if (linkedHead is not null) return;
        for (int i = (Smoke ? 10000 : 1000000) - 1; i >= 0; i--) linkedHead = new Link(i, linkedHead);
    }
    JsonElement Services()
    {
        var services = JsonDocument.Parse(File.ReadAllBytes(Path.Combine(DataDirectory, "services.json"))).RootElement.Clone();
        if (services.GetProperty("echo_host").GetString() != "127.0.0.1") throw new ArgumentException("Loopback services required");
        return services;
    }
    WorkResult ExecuteExternal(string id, string parameter, bool check)
    {
        switch (id)
        {
            case "A01":
            {
                int n = Smoke ? 1000 : 100000; long total = 0;
                (platformPool ??= new PlatformPool(Workers)).Run(n, i => Interlocked.Add(ref total, (i + 1L) * (i + 3L)));
                return WorkResult.Scalar((ulong)total, n, "tasks");
            }
            case "A02":
            {
                int n = Smoke ? 1000 : 100000; using var queue = new BlockingCollection<(int Id, byte[] Data)>(1024);
                byte[] payload = RandomBytes.AsSpan(0, 1024).ToArray(); int producers = Math.Max(1, Workers / 2), consumers = Math.Max(1, Workers - producers); long total = 0; int completed = 0;
                var readers = Enumerable.Range(0, consumers).Select(_ => new Thread(() => { long local = 0; foreach (var item in queue.GetConsumingEnumerable()) { if (item.Data.Length != 1024) throw new InvalidDataException(); local += item.Id; Interlocked.Increment(ref completed); } Interlocked.Add(ref total, local); })).ToArray();
                var writers = Enumerable.Range(0, producers).Select(worker => new Thread(() => { for (int i = worker; i < n; i += producers) queue.Add((i, payload)); })).ToArray();
                foreach (Thread t in readers.Concat(writers)) t.Start(); foreach (Thread t in writers) t.Join(); queue.CompleteAdding(); foreach (Thread t in readers) t.Join();
                if (completed != n) throw new InvalidDataException("Pipeline count"); return WorkResult.Scalar((ulong)total, n, "messages");
            }
            case "A03":
            {
                long total = 0; object gate = new(); int n = Smoke ? 100 : 10000;
                (platformPool ??= new PlatformPool(Workers)).Run(Workers, _ => { for (int i = 0; i < n; i++) { if (parameter == "atomic") Interlocked.Increment(ref total); else lock (gate) total++; } });
                if (total != n * Workers) throw new InvalidDataException("Counter mismatch");
                return WorkResult.Scalar((ulong)total, total, "increments");
            }
            case "A04": return DelayRequests(parameter);
            case "A05": return Cancellation(parameter);
            case "I01": return SequentialFile(parameter);
            case "I02": return RandomFile();
            case "I03": return Echo(false, parameter);
            case "I04": return Echo(true, parameter);
            case "N01": return Native(false, parameter);
            case "N02": return Native(true, parameter);
            default: throw new ArgumentException("Unsupported case");
        }
    }
    void InitializeDelay()
    {
        if (delayClient is not null) return;
        var services = Services(); delayUrl = services.GetProperty("delay_url").GetString()!;
        if (new Uri(delayUrl).Host != "127.0.0.1") throw new ArgumentException("Loopback required");
        delayExpectedBody = services.GetProperty("delay_expected_body").GetString()!;
        delayClient = new HttpClient(new SocketsHttpHandler { MaxConnectionsPerServer = 256 }) { Timeout = TimeSpan.FromSeconds(15) };
    }
    WorkResult DelayRequests(string parameter)
    {
        InitializeDelay(); var client = delayClient!; string url = delayUrl!;
        int n = Smoke ? parameter.EndsWith("10000") ? 32 : 16 : parameter.EndsWith("10000") ? 10000 : 1000;
        string expected = delayExpectedBody!; int completed = 0;
        if (Profile == "idiomatic") Task.WhenAll(Enumerable.Range(0, n).Select(async _ => { if (await client.GetStringAsync(url) != expected) throw new InvalidDataException(); Interlocked.Increment(ref completed); })).GetAwaiter().GetResult();
        else
        {
            using var queue = new BlockingCollection<int>(Math.Max(1024, n)); Exception? failure = null;
            var threads = Enumerable.Range(0, Math.Min(n, Math.Max(1, Workers))).Select(_ => new Thread(() => { try { foreach (int item in queue.GetConsumingEnumerable()) { using var response = client.Send(new HttpRequestMessage(HttpMethod.Get, url)); if (response.Content.ReadAsStringAsync().GetAwaiter().GetResult() != expected) throw new InvalidDataException(); Interlocked.Increment(ref completed); } } catch (Exception error) { Interlocked.CompareExchange(ref failure, error, null); } })).ToArray();
            foreach (Thread t in threads) t.Start(); for (int i = 0; i < n; i++) queue.Add(i); queue.CompleteAdding(); foreach (Thread t in threads) t.Join(); if (failure is not null) throw failure;
        }
        if (completed != n) throw new InvalidDataException("Incomplete delay stream"); return WorkResult.Scalar((ulong)completed, n, "requests");
    }
    WorkResult Cancellation(string parameter)
    {
        using var cancellation = new CancellationTokenSource(); int active = 0; bool observed = false;
        var tasks = Enumerable.Range(0, 8).Select(index => Task.Run(async () => { Interlocked.Increment(ref active); try { if (parameter == "child-failure" && index == 0) throw new InvalidOperationException("injected"); await Task.Delay(1000, cancellation.Token); } finally { Interlocked.Decrement(ref active); } })).ToArray();
        if (parameter == "deadline") cancellation.CancelAfter(10); else Task.WhenAny(tasks).GetAwaiter().GetResult(); cancellation.Cancel();
        try { Task.WhenAll(tasks).GetAwaiter().GetResult(); } catch (Exception) { observed = true; }
        if (active != 0 || !observed) throw new InvalidDataException("Cancellation failed"); return WorkResult.Scalar(0, 8, "children", new { leaked_children = active, observed });
    }
    sealed class PlatformPool : IDisposable
    {
        readonly BlockingCollection<Action> queue = new(1024);
        readonly Thread[] threads;
        public PlatformPool(int workers)
        {
            threads = Enumerable.Range(0, workers).Select(_ => new Thread(() => { foreach (Action work in queue.GetConsumingEnumerable()) work(); }) { IsBackground = true }).ToArray();
            foreach (Thread thread in threads) thread.Start();
        }
        public void Run(int count, Action<int> operation)
        {
            using var completed = new CountdownEvent(count); Exception? failure = null;
            for (int i = 0; i < count; i++)
            {
                int index = i;
                queue.Add(() => { try { operation(index); } catch (Exception error) { Interlocked.CompareExchange(ref failure, error, null); } finally { completed.Signal(); } });
            }
            completed.Wait(); if (failure is not null) throw failure;
        }
        public void Dispose()
        {
            queue.CompleteAdding(); foreach (Thread thread in threads) thread.Join(); queue.Dispose();
        }
    }
}
