using System.Net.Security;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Security.Authentication;
using System.Security.Cryptography.X509Certificates;

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
    string WorkFile()
    {
        string directory = Path.Combine(DataDirectory, "worker-csharp-" + Environment.ProcessId);
        Directory.CreateDirectory(directory); return Path.Combine(directory, "io.bin");
    }
    void EnsureFile(string file, long length)
    {
        if (File.Exists(file) && new FileInfo(file).Length == length) return;
        using var output = new FileStream(file, FileMode.Create, FileAccess.Write, FileShare.None, 65536);
        for (long written = 0; written < length;) { int count = (int)Math.Min(RandomBytes.Length, length - written); output.Write(RandomBytes, 0, count); written += count; } output.Flush(true);
    }
    WorkResult SequentialFile(string parameter)
    {
        string file = WorkFile(); long length = Smoke ? 65536 : 1L << 30; bool write = parameter.StartsWith("write");
        if (!write) EnsureFile(file, length);
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
        string file = WorkFile(); long length = Smoke ? 65536 : 1L << 30; EnsureFile(file, length);
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
    public void Dispose()
    {
        delayClient?.Dispose();
        platformPool?.Dispose();
        plainEcho?.Dispose(); secureEcho?.Dispose();
        if (nativeInts != IntPtr.Zero) Marshal.FreeHGlobal(nativeInts);
        if (nativeBytes != IntPtr.Zero) Marshal.FreeHGlobal(nativeBytes);
        if (nativeLibrary != IntPtr.Zero) NativeLibrary.Free(nativeLibrary);
        string directory = Path.GetDirectoryName(WorkFile())!;
        if (File.Exists(Path.Combine(directory, "io.bin"))) File.Delete(Path.Combine(directory, "io.bin"));
        if (Directory.Exists(directory) && !Directory.EnumerateFileSystemEntries(directory).Any()) Directory.Delete(directory);
    }
}
