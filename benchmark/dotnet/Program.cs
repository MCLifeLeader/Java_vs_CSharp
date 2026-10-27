using System.Buffers.Binary;
using System.Numerics;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text.Json;

// Correctness worker only. No timing from this protocol is admitted for scoring.
try
{
    if (args.Length != 1) throw new ArgumentException("Expected one input file.");
    byte[] bytes = File.ReadAllBytes(args[0]);
    if (bytes.Length == 0 || bytes.Length % sizeof(long) != 0)
        throw new InvalidDataException("Input must contain complete nonempty little-endian int64 values.");
    long[] values = new long[bytes.Length / sizeof(long)];
    for (int i = 0; i < values.Length; i++)
        values[i] = BinaryPrimitives.ReadInt64LittleEndian(bytes.AsSpan(i * sizeof(long), sizeof(long)));

    Emit(new { status = "ready", protocol = 1, language = "csharp", runtime = RuntimeInformation.FrameworkDescription,
        items = values.Length, track = "lts", measured = false });
    while (Console.ReadLine() is { } command)
    {
        if (command == "QUIT") break;
        string[] parts = command.Split('\t');
        if (parts.Length != 3 || parts[0] != "CHECK")
            throw new ArgumentException("Expected CHECK, case and parameter separated by tabs.");
        ulong checksum;
        string? outputSha256 = null;
        switch ((parts[1], parts[2]))
        {
            case ("C01", "transform"):
                checksum = 0;
                unchecked
                {
                    foreach (long value in values)
                    {
                        ulong x = (ulong)value;
                        x = (x + 0x9e3779b97f4a7c15UL) * 0xbf58476d1ce4e5b9UL;
                        x ^= x >> 29;
                        checksum += BitOperations.RotateLeft(x, 17);
                    }
                }
                break;
            case ("K01", "scan"):
                checksum = 0;
                unchecked { foreach (long value in values) checksum += (ulong)value; }
                break;
            case ("K01", "sort"):
                long[] sorted = (long[])values.Clone();
                Array.Sort(sorted); // Signed ascending order on both sides.
                outputSha256 = HashValues(sorted);
                checksum = 0;
                unchecked
                {
                    for (int i = 0; i < sorted.Length; i++) checksum += (ulong)sorted[i] * (ulong)(i + 1);
                }
                break;
            default:
                throw new ArgumentException("Case/parameter is not implemented by this worker.");
        }
        Emit(new { status = "ok", case_id = parts[1], parameter_id = parts[2], items = values.Length,
            checksum = checksum.ToString("x16"), input_sha256 = HashValues(values),
            output_sha256 = outputSha256, measured = false });
    }
}
catch (Exception exception)
{
    // Do not include input paths or uncontrolled exception messages in stdout evidence.
    Emit(new { status = "error", reason = exception.GetType().Name, measured = false });
    return 1;
}
return 0;

static void Emit<T>(T value)
{
    Console.WriteLine(JsonSerializer.Serialize(value));
    Console.Out.Flush();
}

// Untimed semantic diagnostics. The future measured protocol must keep hashing outside timing.
static string HashValues(long[] values)
{
    byte[] bytes = new byte[values.Length * sizeof(long)];
    for (int i = 0; i < values.Length; i++)
        BinaryPrimitives.WriteInt64LittleEndian(bytes.AsSpan(i * sizeof(long), sizeof(long)), values[i]);
    return Convert.ToHexStringLower(SHA256.HashData(bytes));
}
