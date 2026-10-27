package benchmark.suite;

import java.nio.charset.StandardCharsets;
import java.util.List;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class CsvContractTest {
    @Test void quotedCommasQuotesUnicodeAndNewlinesRoundTrip(){
        var rows=List.of(List.of("id","name","group","amount_cents","active"),
            List.of("9223372036854775807","東京, \"雪\"\nline","3","-12345","true"));
        String encoded=Workloads.emitCsv(rows);
        assertEquals(rows,Workloads.parseCsv(encoded));
        assertTrue(encoded.endsWith("\r\n"));
        assertTrue(encoded.contains("\"東京, \"\"雪\"\"\nline\""));
    }
    @Test void emptyFieldsAndFinalRecordWithoutNewlineArePreserved(){
        assertEquals(List.of(List.of("","","x","","")),Workloads.parseCsv(",,x,,"));
        assertEquals(List.of(List.of("","","","","")),Workloads.parseCsv(",,,,\r\n"));
    }
    @Test void unterminatedQuotedFieldIsRejected(){assertThrows(IllegalArgumentException.class,()->Workloads.parseCsv("1,\"bad\r\n"));}
    @Test void charactersFollowingClosingQuoteAreRejected(){assertThrows(IllegalArgumentException.class,()->Workloads.parseCsv("1,\"value\"garbage,3\r\n"));}
    @Test void quoteInsideUnquotedFieldIsRejected(){assertThrows(IllegalArgumentException.class,()->Workloads.parseCsv("1,unquoted\"value,3\r\n"));}
    @Test void fnvMatchesIndependentKnownVectorsAndTreatsBytesUnsigned(){
        assertEquals(0xcbf29ce484222325L,Workloads.fnv(new byte[0]));
        assertEquals(0xa430d84680aabd0bL,Workloads.fnv("hello".getBytes(StandardCharsets.US_ASCII)));
        assertEquals((0xcbf29ce484222325L^255L)*0x100000001b3L,Workloads.fnv(new byte[]{(byte)255}));
    }
}
