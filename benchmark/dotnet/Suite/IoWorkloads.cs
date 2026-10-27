using System.Net.Security;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Security.Authentication;
using System.Security.Cryptography.X509Certificates;
using System.Text.Json;
using System.Globalization;

namespace BenchmarkSuite;

public sealed partial class CoreWorkloads
{
    Stream? plainEcho, secureEcho;
    IntPtr nativeLibrary, nativeInts, nativeBytes;
    NativeSum? nativeSum, nativeBuffer;
    string? trustedThumbprint;
    void InitializeTlsTrust()
    {
        if (trustedThumbprint is not null) return;
        using var certificate = X509CertificateLoader.LoadCertificateFromFile(Services().GetProperty("tls_certificate").GetString()!);
        trustedThumbprint = certificate.Thumbprint;
    }
    string? scratchDirectory, scratchToken, ioFile;
    readonly HashSet<string> scratchFiles = new(StringComparer.Ordinal);
    bool ioPrepared, disposed;

    string WorkFile()
    {
        if (ioFile is not null) return ioFile;
        string? configured = Environment.GetEnvironmentVariable("BENCH_SCRATCH_ROOT");
        string parent = Path.GetFullPath(string.IsNullOrWhiteSpace(configured) ? Path.GetTempPath() : configured);
        Directory.CreateDirectory(parent);
        for (int attempt = 0; attempt < 10; attempt++)
        {
            string token = Guid.NewGuid().ToString("N");
            string candidate = Path.Combine(parent, $"worker-csharp-{Environment.ProcessId}-{token}");
            if (Directory.Exists(candidate) || File.Exists(candidate)) continue;
            Directory.CreateDirectory(candidate);
            DateTimeOffset? started = null;
            try { using var process = System.Diagnostics.Process.GetCurrentProcess(); started = new DateTimeOffset(process.StartTime.ToUniversalTime()); } catch { }
            using (var marker = new FileStream(Path.Combine(candidate, "ownership.json"), FileMode.CreateNew, FileAccess.Write, FileShare.Read))
                JsonSerializer.Serialize(marker, new { schema_version = 1, kind = "benchmark-worker-scratch", language = "csharp", pid = Environment.ProcessId,
                    process_start_utc = started?.ToString("O", CultureInfo.InvariantCulture), process_start_unix_ms = started?.ToUnixTimeMilliseconds(),
                    token, scratch_root_source = string.IsNullOrWhiteSpace(configured) ? "os_temp" : "environment" });
            scratchDirectory = candidate; scratchToken = token; ioFile = Path.Combine(candidate, "io.bin"); scratchFiles.Add(ioFile);
            return ioFile;
        }
        throw new IOException("Cannot allocate a unique worker scratch directory");
    }
    void PrepareIoFile()
    {
        ioPrepared = false;
        if (RandomBytes.Length == 0) throw new InvalidDataException("Empty canonical I/O corpus");
        string file = WorkFile(); long length = Smoke ? 65536 : 1L << 30;
        string staging = Path.Combine(scratchDirectory!, "io-prepare-" + Guid.NewGuid().ToString("N") + ".tmp");
        scratchFiles.Add(staging);
        using (var output = new FileStream(staging, FileMode.CreateNew, FileAccess.Write, FileShare.None, 65536))
        {
            for (long written = 0; written < length;)
            {
                int offset = (int)(written % RandomBytes.Length), count = (int)Math.Min(Math.Min(65536, RandomBytes.Length - offset), length - written);
                output.Write(RandomBytes, offset, count); written += count;
            }
            output.Flush(true);
        }
        using (var input = new FileStream(staging, FileMode.Open, FileAccess.Read, FileShare.Read, 65536))
        {
            byte[] buffer = new byte[65536];
            for (long read = 0; read < length;)
            {
                int offset = (int)(read % RandomBytes.Length), count = (int)Math.Min(Math.Min(buffer.Length, RandomBytes.Length - offset), length - read);
                input.ReadExactly(buffer.AsSpan(0, count));
                if (!buffer.AsSpan(0, count).SequenceEqual(RandomBytes.AsSpan(offset, count))) throw new InvalidDataException("I/O preparation differs from canonical bytes");
                read += count;
            }
            if (input.ReadByte() != -1) throw new InvalidDataException("I/O preparation length differs");
        }
        File.Move(staging, file, true); scratchFiles.Remove(staging); ioPrepared = true;
    }
    string PreparedWorkFile()
    {
        if (!ioPrepared || ioFile is null) throw new InvalidOperationException("PREPARE is required before I/O execution");
        return ioFile;
    }
    WorkResult SequentialFile(string parameter)
    {
        string file = PreparedWorkFile(); long length = Smoke ? 65536 : 1L << 30; bool write = parameter.StartsWith("write");
        byte[] buffer = new byte[65536]; ulong total = 0;
        using var stream = new FileStream(file, write ? FileMode.Create : FileMode.Open, write ? FileAccess.Write : FileAccess.Read, FileShare.None, 65536, FileOptions.SequentialScan);
        for (long offset = 0; offset < length; offset += buffer.Length)
        {
            int count = (int)Math.Min(buffer.Length, length - offset);
            if (write) { RandomBytes.AsSpan((int)(offset % RandomBytes.Length), count).CopyTo(buffer); stream.Write(buffer, 0, count); } else stream.ReadExactly(buffer.AsSpan(0, count));
            for (int i = 0; i < count; i++) total += buffer[i];
        }
        if (write) stream.Flush(parameter.EndsWith("durable-flush")); return WorkResult.Scalar(total, length, "bytes");
    }
    WorkResult RandomFile()
    {
        string file = PreparedWorkFile(); long length = Smoke ? 65536 : 1L << 30;
        byte[] buffer = new byte[4096]; int reads = Smoke ? 16 : 256; ulong sum = 0;
        using var stream = new FileStream(file, FileMode.Open, FileAccess.Read, FileShare.Read, 4096, FileOptions.RandomAccess);
        for (int i = 0; i < reads; i++) { stream.Position = (i * 7919L % (length / 4096)) * 4096; stream.ReadExactly(buffer); sum += buffer[0]; }
        return WorkResult.Scalar(sum, reads, "reads");
    }
    Stream OpenEcho(bool tls)
    {
        var services = Services(); var tcp = new TcpClient { NoDelay = true };
        tcp.Connect("127.0.0.1", services.GetProperty(tls ? "tls_port" : "echo_port").GetInt32()); Stream stream = tcp.GetStream();
        if (tls)
        {
            InitializeTlsTrust();
            var ssl = new SslStream(stream, false, (_, certificate, _, errors) => certificate is not null && certificate.GetCertHashString() == trustedThumbprint && (errors & SslPolicyErrors.RemoteCertificateNameMismatch) == 0);
            ssl.AuthenticateAsClient(new SslClientAuthenticationOptions { TargetHost = "localhost", EnabledSslProtocols = SslProtocols.Tls13, AllowTlsResume = false, CertificateRevocationCheckMode = X509RevocationMode.NoCheck }); stream = ssl;
        }
        return stream;
    }
    WorkResult Echo(bool tls, string parameter)
    {
        if (tls && parameter == "handshake")
        {
            using Stream handshake = OpenEcho(true);
            var ssl = (SslStream)handshake;
            return WorkResult.Scalar(1, 1, "handshakes", new { tls_protocol = ssl.SslProtocol.ToString(), cipher = ssl.NegotiatedCipherSuite.ToString() });
        }
        Stream stream = tls ? secureEcho ??= OpenEcho(true) : plainEcho ??= OpenEcho(false);
        byte[] received = new byte[RandomBytes.Length]; Task read = stream.ReadExactlyAsync(received).AsTask(); stream.Write(RandomBytes); stream.Flush(); read.GetAwaiter().GetResult();
        if (!received.AsSpan().SequenceEqual(RandomBytes)) throw new InvalidDataException("Echo mismatch"); return WorkResult.Data(received, received.Length);
    }
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate ulong NativeSum(IntPtr values, nuint count);
    void InitializeNative()
    {
        if (nativeLibrary != IntPtr.Zero) return;
        string path = Environment.GetEnvironmentVariable("BENCH_NATIVE_LIBRARY") ?? throw new ArgumentException("BENCH_NATIVE_LIBRARY required");
        nativeLibrary = NativeLibrary.Load(path);
        nativeSum = Marshal.GetDelegateForFunctionPointer<NativeSum>(NativeLibrary.GetExport(nativeLibrary, "bench_sum"));
        nativeBuffer = Marshal.GetDelegateForFunctionPointer<NativeSum>(NativeLibrary.GetExport(nativeLibrary, "bench_buffer"));
        byte[] integers = LongBytes(Enumerable.Range(0, 1024).Select(i => (long)i - 512));
        nativeInts = Marshal.AllocHGlobal(integers.Length); Marshal.Copy(integers, 0, nativeInts, integers.Length);
        nativeBytes = Marshal.AllocHGlobal(RandomBytes.Length); Marshal.Copy(RandomBytes, 0, nativeBytes, RandomBytes.Length);
    }
    WorkResult Native(bool buffer, string parameter)
    {
        InitializeNative(); int count = parameter == "count-1" ? 1 : 1024;
        return WorkResult.Scalar(buffer ? nativeBuffer!(nativeBytes, (nuint)RandomBytes.Length) : nativeSum!(nativeInts, (nuint)count), buffer ? RandomBytes.Length : count, buffer ? "bytes" : "integers");
    }
    static void CleanupAttempt(List<string> errors, string name, Action action)
    {
        try { action(); } catch (Exception error) { errors.Add(name + ":" + error.GetType().Name); }
    }
    void CleanupScratch(List<string> errors)
    {
        if (scratchDirectory is null || scratchToken is null) return;
        string marker = Path.Combine(scratchDirectory, "ownership.json");
        using (var ownership = JsonDocument.Parse(File.ReadAllBytes(marker)))
            if (ownership.RootElement.GetProperty("token").GetString() != scratchToken || ownership.RootElement.GetProperty("kind").GetString() != "benchmark-worker-scratch")
                throw new IOException("Worker scratch ownership marker differs");
        foreach (string path in scratchFiles) CleanupAttempt(errors, "scratch-file", () => File.Delete(path));
        if (Directory.EnumerateFileSystemEntries(scratchDirectory).Any(path => !string.Equals(path, marker, StringComparison.Ordinal)))
        {
            errors.Add("scratch-directory:not-empty"); return;
        }
        CleanupAttempt(errors, "scratch-marker", () => File.Delete(marker));
        CleanupAttempt(errors, "scratch-directory", () => Directory.Delete(scratchDirectory, false));
    }
    public void Dispose()
    {
        if (disposed) return; disposed = true;
        var errors = new List<string>();
        CleanupAttempt(errors, "plain-echo", () => plainEcho?.Dispose());
        CleanupAttempt(errors, "secure-echo", () => secureEcho?.Dispose());
        CleanupAttempt(errors, "http-client", () => delayClient?.Dispose());
        CleanupAttempt(errors, "native-integers", () => { if (nativeInts != IntPtr.Zero) { Marshal.FreeHGlobal(nativeInts); nativeInts = IntPtr.Zero; } });
        CleanupAttempt(errors, "native-bytes", () => { if (nativeBytes != IntPtr.Zero) { Marshal.FreeHGlobal(nativeBytes); nativeBytes = IntPtr.Zero; } });
        CleanupAttempt(errors, "native-library", () => { if (nativeLibrary != IntPtr.Zero) { NativeLibrary.Free(nativeLibrary); nativeLibrary = IntPtr.Zero; } });
        CleanupAttempt(errors, "scratch", () => CleanupScratch(errors));
        CleanupAttempt(errors, "platform-pool", () => platformPool?.Dispose());
        if (errors.Count != 0) try { Console.Error.WriteLine(JsonSerializer.Serialize(new { status = "cleanup_incomplete", language = "csharp", errors })); } catch { }
    }
}
