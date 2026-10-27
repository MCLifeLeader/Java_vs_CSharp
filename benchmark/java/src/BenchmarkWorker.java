import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.Arrays;

/** Correctness worker only; no timing from this protocol is score-bearing. */
public final class BenchmarkWorker {
    public static void main(String[] args) {
        try {
            if (args.length != 1) throw new IllegalArgumentException();
            byte[] bytes = Files.readAllBytes(Path.of(args[0]));
            if (bytes.length == 0 || bytes.length % Long.BYTES != 0) throw new IllegalArgumentException();
            var buffer = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN);
            long[] values = new long[bytes.length / Long.BYTES];
            for (int i = 0; i < values.length; i++) values[i] = buffer.getLong();
            emit("{\"status\":\"ready\",\"protocol\":1,\"language\":\"java\",\"runtime\":\""
                + Runtime.version() + "\",\"items\":" + values.length + ",\"track\":\"lts\",\"measured\":false}");
            var reader = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
            String command;
            while ((command = reader.readLine()) != null) {
                if (command.equals("QUIT")) break;
                String[] parts = command.split("\t", -1);
                if (parts.length != 3 || !parts[0].equals("CHECK")) throw new IllegalArgumentException();
                long checksum = 0;
                String outputSha256 = "null";
                switch (parts[1] + "/" + parts[2]) {
                    case "C01/transform" -> {
                        for (long value : values) {
                            long x = (value + 0x9e3779b97f4a7c15L) * 0xbf58476d1ce4e5b9L;
                            x ^= x >>> 29;
                            checksum += Long.rotateLeft(x, 17);
                        }
                    }
                    case "K01/scan" -> {
                        for (long value : values) checksum += value;
                    }
                    case "K01/sort" -> {
                        long[] sorted = values.clone();
                        Arrays.sort(sorted);
                        outputSha256 = "\"" + hashValues(sorted) + "\"";
                        for (int i = 0; i < sorted.length; i++) checksum += sorted[i] * (i + 1L);
                    }
                    default -> throw new IllegalArgumentException();
                }
                emit("{\"status\":\"ok\",\"case_id\":\"" + parts[1] + "\",\"parameter_id\":\"" + parts[2]
                    + "\",\"items\":" + values.length + ",\"checksum\":\"" + String.format("%016x", checksum)
                    + "\",\"input_sha256\":\"" + hashValues(values) + "\",\"output_sha256\":" + outputSha256
                    + ",\"measured\":false}");
            }
        } catch (Exception exception) {
            emit("{\"status\":\"error\",\"reason\":\"" + exception.getClass().getSimpleName() + "\",\"measured\":false}");
            System.exit(1);
        }
    }

    private static void emit(String json) {
        System.out.println(json);
        System.out.flush();
    }

    // Untimed semantic diagnostics; keep outside any future performance timing.
    private static String hashValues(long[] values) throws Exception {
        var buffer = ByteBuffer.allocate(values.length * Long.BYTES).order(ByteOrder.LITTLE_ENDIAN);
        for (long value : values) buffer.putLong(value);
        return java.util.HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(buffer.array()));
    }
}
