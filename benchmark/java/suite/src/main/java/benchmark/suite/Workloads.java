package benchmark.suite;

import benchmark.proto.CanonicalRecord;
import com.fasterxml.jackson.databind.JsonNode;
import java.io.*;
import java.lang.foreign.*;
import java.lang.invoke.MethodHandle;
import java.lang.reflect.Method;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.math.RoundingMode;
import java.net.*;
import java.net.http.*;
import java.nio.*;
import java.nio.channels.FileChannel;
import java.nio.charset.*;
import java.nio.file.*;
import java.security.*;
import java.security.cert.CertificateFactory;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;
import java.util.function.LongUnaryOperator;
import java.util.regex.Pattern;
import java.util.stream.Collectors;
import java.util.zip.*;
import javax.net.ssl.*;

public final class Workloads implements AutoCloseable {
    static final List<String> SUPPORTED=List.of("C01","C02","C03","C04","C05","C06","K01","K02","K03","K04","K05","K06",
        "M01","M02","M03","M04","M05","A01","A02","A03","A04","A05","S01","S02","S03","S04","I01","I02","I03","I04","H01","H02","H03","N01","N02","L01","L02","L03","L04");
    public record Result(long checksum,long units,String unit,byte[] semantic,Object details) {}
    record RefRecord(long id,String name,long group,long amount,boolean active) {}
    record LogicalRecord(long a,long b,long c,long d,long e,long f,long g,long h) {}
    static final class Link { long id,a,b,c,d,e,f; Link next; Link(long id,Link next){this.id=id;this.next=next;} }
    record Message(int id,byte[] payload) {}
    record Tagged(long id,int tag) {}
    public record Child(long id,String label) {}
    public record Document(long id,String name,long group,long amount_cents,boolean active,List<Child> children,String padding) {}

    final Path data;
    final boolean smoke;
    final int workers,n,allocations;
    final String profile;
    final long[] integers;
    final byte[] ascii,unicode,text,random;
    final String asciiText,unicodeText;
    final Map<Integer,byte[]> documentBytes=new HashMap<>();
    final Map<Integer,Document> documents=new HashMap<>();
    final CanonicalRecord.Record proto;
    final byte[] protoBytes,csvBytes;
    final String csvText;
    final byte[] textGzip,randomGzip;
    final List<String> integerLines;
    final List<List<String>> csvRows;
    final List<RefRecord> records;
    final Set<Long> leftSet,rightSet;
    final Pattern regex=Pattern.compile("[A-Za-z]+|[0-9]+");
    final ExecutorService pool;
    final HttpClient http;
    Path temporary,file;
    String scratchToken;
    final Set<Path> scratchFiles=new LinkedHashSet<>();
    boolean closed;
    final Map<String,Object> services;
    Link linked;
    Socket echo;
    SSLSocket tls;
    SSLContext tlsContext;
    Arena nativeArena;
    MemorySegment nativeInts,nativeBytes;
    MethodHandle nativeSum,nativeBuffer;
    boolean ioPrepared;
    final ArrayDeque<long[]> sortInputs=new ArrayDeque<>();
    final ArrayDeque<List<RefRecord>> listInputs=new ArrayDeque<>();
    final ArrayDeque<Map<Object,Long>> mapInputs=new ArrayDeque<>();
    String[] asciiKeys;Long[] integerKeys;
    Map<Object,Long> asciiLookupMap,integerLookupMap;
    final Tagged[] taggedInputs;

    Workloads(Path data,boolean smoke,int workers,String profile)throws Exception {
        this.data=data.toAbsolutePath();this.smoke=smoke;this.workers=workers;this.profile=profile;
        if(!profile.equals("controlled")&&!profile.equals("idiomatic"))throw new IllegalArgumentException("Unknown profile");
        n=smoke?1000:100000;allocations=smoke?1000:100000;
        byte[] binary=Files.readAllBytes(data.resolve("integers.i64le"));
        if(binary.length==0 || binary.length%8!=0)throw new IllegalArgumentException("Malformed integer fixture");
        integers=new long[binary.length/8];var input=ByteBuffer.wrap(binary).order(ByteOrder.LITTLE_ENDIAN);
        for(int i=0;i<integers.length;i++)integers[i]=input.getLong();
        ascii=Files.readAllBytes(data.resolve("ascii.txt"));unicode=Files.readAllBytes(data.resolve("unicode.txt"));
        asciiText=strictDecode(ascii);unicodeText=strictDecode(unicode);
        text=Files.readAllBytes(data.resolve("text-1m.bin"));random=Files.readAllBytes(data.resolve("random-1m.bin"));
        textGzip=Files.readAllBytes(data.resolve("text-1m.gz"));randomGzip=Files.readAllBytes(data.resolve("random-1m.gz"));
        integerLines=Files.readAllLines(data.resolve("integers.txt"),StandardCharsets.US_ASCII);
        for(String line:integerLines)if(!line.matches("-?[0-9]+"))throw new IllegalStateException("Invalid integer grammar");
        for(int size:new int[]{1024,65536,1048576}) {
            byte[] bytes=Files.readAllBytes(data.resolve("document-"+size+".json"));documentBytes.put(size,bytes);
            documents.put(size,SuiteMain.JSON.readValue(bytes,Document.class));
        }
        protoBytes=Files.readAllBytes(data.resolve("protobuf.bin"));proto=CanonicalRecord.Record.parseFrom(protoBytes);
        csvBytes=Files.readAllBytes(data.resolve("csv-10m.csv"));csvText=strictDecode(csvBytes);csvRows=parseCsv(csvText);
        for(List<String> row:csvRows)if(row.size()!=5)throw new IllegalStateException("Malformed CSV row");
        records=makeRecords();leftSet=makeSet(0,n);rightSet=makeSet(n/2,n+n/2);
        taggedInputs=new Tagged[n];for(int i=0;i<n;i++)taggedInputs[i]=new Tagged(i,i%4);
        pool=Executors.newFixedThreadPool(workers);
        http=HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1).connectTimeout(Duration.ofSeconds(15)).build();
        services=Files.isRegularFile(data.resolve("services.json"))?SuiteMain.JSON.readValue(Files.readAllBytes(data.resolve("services.json")),Map.class):Map.of();
    }

    void prepare(String id,String parameter,int iterations)throws Exception {
        if(iterations<1||iterations>100000000)throw new IllegalArgumentException("Invalid prepare count");
        sortInputs.clear();listInputs.clear();mapInputs.clear();
        long estimate=id.equals("K01")&&parameter.equals("sort")?(long)integers.length*8*iterations:
            id.equals("K03")&&parameter.endsWith("remove")?(long)n*128*iterations:id.equals("K02")&&parameter.equals("remove")?(long)n*8*iterations:0;
        long maximum=Long.parseLong(System.getenv().getOrDefault("BENCH_PREPARE_BYTES",String.valueOf(256L*1024*1024)));
        if(maximum<1||estimate>maximum)throw new IllegalArgumentException("Prepared state exceeds BENCH_PREPARE_BYTES; requested fixed work is infeasible under this memory budget");
        if(id.equals("K01")&&parameter.equals("sort"))for(int i=0;i<iterations;i++)sortInputs.add(integers.clone());
        if(id.equals("K02")&&parameter.equals("remove"))for(int i=0;i<iterations;i++)listInputs.add(new ArrayList<>(records));
        if(id.equals("K03")){initializeMapKeys();boolean ascii=parameter.startsWith("ascii-");
            if(parameter.endsWith("lookup")){if(ascii&&asciiLookupMap==null)asciiLookupMap=makeMap(true);else if(!ascii&&integerLookupMap==null)integerLookupMap=makeMap(false);}
            if(parameter.endsWith("remove"))for(int i=0;i<iterations;i++)mapInputs.add(makeMap(ascii));}
        if(id.equals("I01")||id.equals("I02"))prepareIo();
        if(id.startsWith("N"))initializeNative();
        if(id.equals("I03")&&echo==null)echo=openSocket(false);
        if(id.equals("I04")){initializeTls();if(parameter.equals("persistent-transfer")&&tls==null)tls=(SSLSocket)openSocket(true);}
        if(id.equals("M03")&&linked==null){int count=smoke?10000:1000000;for(int i=count-1;i>=0;i--)linked=new Link(i,linked);}
    }

    Result run(String id,String parameter,boolean check)throws Exception {
        return switch(id) {
            case "C01" -> {require(parameter,"transform");yield scalar(transform(integers),integers.length,"items",check);}
            case "C02" -> matrix(parameter,check);
            case "C03" -> invoices(parameter,check);
            case "C04" -> {require(parameter,"bits2048");byte[] result=BigInteger.ONE.shiftLeft(1024).add(BigInteger.valueOf(12345)).modPow(BigInteger.valueOf(65537),BigInteger.ONE.shiftLeft(2048).subtract(BigInteger.valueOf(159))).toByteArray();if(result[0]==0)result=Arrays.copyOfRange(result,1,result.length);yield bytes(result,1,"operations",check);}
            case "C05" -> {int size=switch(parameter){case "bytes1024"->1024;case "bytes1048576"->1048576;default->throw invalid();};size=Math.min(size,random.length);var digest=MessageDigest.getInstance("SHA-256");digest.update(random,0,size);yield bytes(digest.digest(),size,"bytes",check);}
            case "C06" -> gzip(parameter,check);
            case "K01" -> primitive(parameter,check);
            case "K02" -> list(parameter,check);
            case "K03" -> map(parameter,check);
            case "K04" -> sets(parameter,check);
            case "K05" -> text(parameter,check);
            case "K06" -> parse(parameter,check);
            case "M01" -> {require(parameter,"allocate-release");yield scalar(allocate(0),allocations,"items",check);}
            case "M02" -> {int survival=switch(parameter){case "survival-1pct"->1;case "survival-10pct"->10;case "survival-50pct"->50;default->throw invalid();};yield scalar(allocate(survival),allocations*10L,"items",check);}
            case "M03" -> retained(parameter,check);
            case "M04" -> buffers(parameter,check);
            case "M05" -> {if(!parameter.equals("limit-512MiB")&&!parameter.equals("limit-2048MiB"))throw invalid();yield scalar(allocate(10),allocations*10L,"items",check);}
            case "A01" -> tasks(parameter,check);
            case "A02" -> pipeline(parameter,check);
            case "A03" -> counter(parameter,check);
            case "A04" -> delay(parameter,check);
            case "A05" -> cancellation(parameter,check);
            case "S01" -> json(parameter,check);
            case "S02" -> protobuf(parameter,check);
            case "S03" -> transformation(parameter,check);
            case "S04" -> csv(parameter,check);
            case "I01" -> sequential(parameter,check);
            case "I02" -> randomReads(parameter,check);
            case "I03" -> network(parameter,false,check);
            case "I04" -> network(parameter,true,check);
            case "H01" -> {if(!parameter.startsWith("get-"))throw invalid();yield bytes("0123456789abcdef0123456789abcdef".getBytes(StandardCharsets.UTF_8),1,"responses",check);}
            case "H02" -> {String[]p=parameter.split("-");if(p.length<3||(!p[0].equals("get")&&!p[0].equals("post")))throw invalid();yield json("serialize-"+p[1],check);}
            case "H03" -> {if(!parameter.startsWith("get-"))throw invalid();yield run("C01","transform",check);}
            case "N01","N02" -> nativeCall(id,parameter,check);
            case "L01" -> representations(parameter,check);
            case "L02" -> dispatch(parameter,check);
            case "L03" -> exceptions(parameter,check);
            case "L04" -> invocation(parameter,check);
            default -> throw new IllegalArgumentException("Case is not owned by the Java primitive/service worker: "+id);
        };
    }

    static long transform(long[] values){long sum=0;for(long value:values){long x=(value+0x9e3779b97f4a7c15L)*0xbf58476d1ce4e5b9L;x^=x>>>29;sum+=Long.rotateLeft(x,17);}return sum;}

    private Result matrix(String p,boolean check){
        int size=switch(p){case "n128"->128;case "n512"->512;default->throw invalid();};if(smoke)size=16;
        double[] a=new double[size*size],b=new double[size*size],c=new double[size*size];
        for(int i=0;i<size;i++)for(int j=0;j<size;j++){a[i*size+j]=((i*17+j*13)%97)/97.0;b[i*size+j]=((i*7+j*19)%89)/89.0;}
        double total=0;for(int i=0;i<size;i++)for(int j=0;j<size;j++){double sum=0;for(int k=0;k<size;k++)sum+=a[i*size+k]*b[k*size+j];c[i*size+j]=sum;total+=sum;}
        long checksum=(long)Math.rint(total*1000000);return new Result(checksum,2L*size*size*size,"flops",check?scalarBytes(checksum):new byte[0],check?Map.of("n",size,"values",c):null);
    }
    private Result invoices(String p,boolean check){require(p,"invoices");int count=smoke?1000:100000;long sum=0;
        for(int i=0;i<count;i++){
            BigDecimal subtotal=BigDecimal.valueOf(100+i%10000,2).multiply(BigDecimal.valueOf(1+i%5))
                .multiply(BigDecimal.ONE.subtract(BigDecimal.valueOf(i%7,2))).setScale(2,RoundingMode.HALF_EVEN);
            BigDecimal tax=subtotal.multiply(new BigDecimal("0.0825")).setScale(2,RoundingMode.HALF_EVEN);
            sum=Math.addExact(sum,subtotal.add(tax).movePointRight(2).longValueExact());
        }return scalar(sum,count,"invoices",check);
    }
    private Result gzip(String p,boolean check)throws Exception {
        boolean compress=p.startsWith("compress-"),decompress=p.startsWith("decompress-");if(!compress&&!decompress)throw invalid();
        String suffix=p.substring(p.indexOf('-')+1);byte[]original=switch(suffix){case "text"->text;case "incompressible"->random;default->throw invalid();};byte[]output;
        int compressedBytes;
        if(compress){var target=new ByteArrayOutputStream();try(var gzip=new GZIPOutputStream(target)){gzip.write(original);}byte[]compressed=target.toByteArray();compressedBytes=compressed.length;
            if(check){try(var unzip=new GZIPInputStream(new ByteArrayInputStream(compressed))){if(!Arrays.equals(unzip.readAllBytes(),original))throw new IllegalStateException("Gzip roundtrip failed");}}output=original;
        }else {byte[]compressed=suffix.equals("text")?textGzip:randomGzip;compressedBytes=compressed.length;
            try(var unzip=new GZIPInputStream(new ByteArrayInputStream(compressed))){output=unzip.readAllBytes();}if(check&&!Arrays.equals(output,original))throw new IllegalStateException("Gzip output differs");}
        return new Result(fnv(output),original.length,"bytes",check?output:new byte[0],check?Map.of("compressed_bytes",compressedBytes,"level","zlib-default-6"):null);
    }
    private Result primitive(String p,boolean check){long sum=0;if(p.equals("scan")){for(long v:integers)sum+=v;return scalar(sum,integers.length,"items",check);}
        require(p,"sort");long[]copy=sortInputs.isEmpty()?integers.clone():sortInputs.removeFirst();Arrays.sort(copy);for(int i=0;i<copy.length;i++)sum+=copy[i]*(i+1L);
        byte[]semantic=new byte[0];if(check){var b=ByteBuffer.allocate(copy.length*8).order(ByteOrder.LITTLE_ENDIAN);for(long value:copy)b.putLong(value);semantic=b.array();}
        return new Result(sum,copy.length,"items",semantic,null);
    }
    private List<RefRecord>makeRecords(){var result=new ArrayList<RefRecord>(n);for(int i=0;i<n;i++)result.add(new RefRecord(i,"r"+i,i%17,i*3L,i%2==0));return result;}
    private Result list(String p,boolean check){List<RefRecord>list;if(p.equals("append")){list=new ArrayList<>(n);list.addAll(records);}else{list=records;if(p.equals("remove")){list=listInputs.isEmpty()?new ArrayList<>(records):listInputs.removeFirst();for(int i=list.size()-1;i>=0;i-=2)list.remove(i);}else require(p,"iterate");}long sum=0;for(RefRecord r:list)sum+=r.id;return scalar(sum,n,"items",check);}
    private void initializeMapKeys(){
        if(asciiKeys!=null)return;asciiKeys=new String[n*2];integerKeys=new Long[n*2];
        for(int i=0;i<n*2;i++){String digits=Integer.toString(i);asciiKeys[i]="k"+"0".repeat(8-digits.length())+digits;integerKeys[i]=Long.valueOf(i);}
    }
    private Map<Object,Long>makeMap(boolean ascii){initializeMapKeys();var map=new HashMap<Object,Long>((int)Math.ceil(n/.75));for(int i=0;i<n;i++)map.put(ascii?asciiKeys[i]:integerKeys[i],i*3L);return map;}
    private Result map(String p,boolean check){String[]v=p.split("-");if(v.length!=2)throw invalid();boolean ascii=v[0].equals("ascii");if(!ascii&&!v[0].equals("int64"))throw invalid();initializeMapKeys();
        Map<Object,Long>map;
        if(v[1].equals("lookup")){if(ascii){if(asciiLookupMap==null)asciiLookupMap=makeMap(true);map=asciiLookupMap;}else{if(integerLookupMap==null)integerLookupMap=makeMap(false);map=integerLookupMap;}}
        else map=v[1].equals("insert")?makeMap(ascii):mapInputs.isEmpty()?makeMap(ascii):mapInputs.removeFirst();
        long sum=0;switch(v[1]){case"insert"->{for(long value:map.values())sum+=value;}case"lookup"->{for(int i=0;i<2*n;i++)sum+=map.getOrDefault(ascii?asciiKeys[i]:integerKeys[i],0L);}case"remove"->{for(int i=0;i<n;i+=2)map.remove(ascii?asciiKeys[i]:integerKeys[i]);for(long value:map.values())sum+=value;}default->throw invalid();}return scalar(sum,v[1].equals("lookup")?n*2L:n,"items",check);}
    private static Set<Long>makeSet(int start,int end){var set=new HashSet<Long>((int)Math.ceil((end-start)/.75));for(long i=start;i<end;i++)set.add(i);return set;}
    private Result sets(String p,boolean check){boolean union=p.startsWith("union-"),intersection=p.startsWith("intersection-");if(!union&&!intersection)throw invalid();boolean build=p.endsWith("build-inclusive");if(!build&&!p.endsWith("prebuilt"))throw invalid();
        Set<Long>a=build?makeSet(0,n):leftSet,b=build?makeSet(n/2,n+n/2):rightSet;var result=new HashSet<>(a);if(union)result.addAll(b);else result.retainAll(b);long sum=0;for(long value:result)sum+=value;
        byte[]semantic=new byte[0];if(check)semantic=result.stream().sorted().map(String::valueOf).collect(Collectors.joining("\n")).getBytes(StandardCharsets.UTF_8);return new Result(sum,result.size(),"items",semantic,null);}
    private Result text(String p,boolean check)throws Exception {
        if(!Set.of("encode-ascii","encode-unicode","decode-ascii","decode-unicode","search-ascii","search-unicode").contains(p))throw invalid();
        boolean isAscii=p.endsWith("-ascii");byte[]corpus=isAscii?ascii:unicode;String content=isAscii?asciiText:unicodeText;
        String operation=p.substring(0,p.indexOf('-'));
        return switch(operation){
            case "encode"->bytes(content.getBytes(StandardCharsets.UTF_8),corpus.length,"bytes",check);
            case "decode"->{String decoded=strictDecode(corpus);if(check&&!decoded.equals(content))throw new IllegalStateException("Decoded text differs");
                Result result=bytes(corpus,corpus.length,"bytes",check);java.lang.ref.Reference.reachabilityFence(decoded);yield result;}
            case "search"->{String token=isAscii?"benchmark":"世界";long count=0;for(int offset=0;(offset=content.indexOf(token,offset))>=0;offset+=token.length())count++;yield scalar(count,corpus.length,"bytes",check);}
            default->throw invalid();
        };
    }
    private Result parse(String p,boolean check)throws Exception {
        if(p.equals("integer-parse")){long sum=0;for(String line:integerLines)sum+=Long.parseLong(line);return scalar(sum,integerLines.size(),"items",check);}
        require(p,"regex-extract");var matcher=regex.matcher(asciiText);long sum=0;while(matcher.find())sum+=matcher.end()-matcher.start();return scalar(sum,ascii.length,"bytes",check);
    }

    private long allocate(int survival){
        if(survival==0){LogicalRecord[]all=new LogicalRecord[allocations];long sum=0;for(int i=0;i<allocations;i++)all[i]=new LogicalRecord(i,i+1,i+2,i+3,i+4,i+5,i+6,i+7);for(LogicalRecord value:all)sum+=value.a;return sum;}
        var windows=new ArrayDeque<List<LogicalRecord>>();long sum=0;
        for(int epoch=0;epoch<10;epoch++){var retained=new ArrayList<LogicalRecord>();for(int i=0;i<allocations;i++){var value=new LogicalRecord(i,i+1,i+2,i+3,i+4,i+5,i+6,i+7);sum+=value.a;if(i%(100/survival)==0)retained.add(value);}windows.add(retained);if(windows.size()>3)windows.removeFirst();}
        java.lang.ref.Reference.reachabilityFence(windows);return sum;
    }
    private Result retained(String p,boolean check){require(p,"retain-query");int count=smoke?10000:1000000;if(linked==null){for(int i=count-1;i>=0;i--)linked=new Link(i,linked);}long sum=0;for(int repeat=0;repeat<10;repeat++)for(Link cursor=linked;cursor!=null;cursor=cursor.next)sum+=cursor.id;return scalar(sum,count*10L,"items",check);}
    private Result buffers(String p,boolean check){int split=p.lastIndexOf('-');if(split<0)throw invalid();String op=p.substring(0,split);int size=Math.min(Integer.parseInt(p.substring(split+1)),random.length);byte[]copy=Arrays.copyOf(random,size);long sum=0;switch(op){case"copy","slice","parse"->{for(byte b:copy)sum+=b&255;}default->throw invalid();}return scalar(sum,size,"bytes",check);}

    private Result tasks(String p,boolean check)throws Exception{require(p,"task-graph");int count=smoke?1000:100000;var permits=new Semaphore(1024);var sum=new AtomicLong();var done=new CountDownLatch(count);for(int i=0;i<count;i++){permits.acquire();final long id=i;pool.submit(()->{try{sum.addAndGet((id+1)*(id+3));}finally{permits.release();done.countDown();}});}if(!done.await(15,TimeUnit.SECONDS))throw new TimeoutException();return scalar(sum.get(),count,"tasks",check);}
    private Result pipeline(String p,boolean check)throws Exception{require(p,"pipeline");int count=smoke?1000:100000,producers=Math.max(1,workers/2),consumers=Math.max(1,workers-producers);var queue=new ArrayBlockingQueue<Message>(1024);var sum=new AtomicLong();var sent=new AtomicInteger();var consumed=new AtomicInteger();var threads=new ArrayList<Thread>();var failures=new ConcurrentLinkedQueue<Throwable>();
        for(int c=0;c<consumers;c++){Thread t=Thread.ofPlatform().unstarted(()->{try{while(true){Message value=queue.take();if(value.id<0)break;if(value.payload.length!=1024)throw new IllegalStateException();sum.addAndGet(value.id);consumed.incrementAndGet();}}catch(Throwable e){failures.add(e);}});threads.add(t);t.start();}
        byte[]payload=Arrays.copyOf(random,1024);var producerThreads=new ArrayList<Thread>();for(int pnum=0;pnum<producers;pnum++){Thread t=Thread.ofPlatform().unstarted(()->{try{int id;while((id=sent.getAndIncrement())<count)queue.put(new Message(id,payload));}catch(Throwable e){failures.add(e);}});producerThreads.add(t);t.start();}for(Thread t:producerThreads)t.join();for(int c=0;c<consumers;c++)queue.put(new Message(-1,new byte[0]));for(Thread t:threads)t.join();if(!failures.isEmpty()||consumed.get()!=count)throw new IllegalStateException("Pipeline lost work");return scalar(sum.get(),count,"messages",check);}
    private Result counter(String p,boolean check)throws Exception{if(!p.equals("atomic")&&!p.equals("lock"))throw invalid();int count=smoke?100:10000;var atomic=new AtomicLong();long[]locked={0};Object lock=new Object();var futures=new ArrayList<Future<?>>();for(int w=0;w<workers;w++)futures.add(pool.submit(()->{for(int i=0;i<count;i++)if(p.equals("atomic"))atomic.incrementAndGet();else synchronized(lock){locked[0]++;}}));for(Future<?>f:futures)f.get();return scalar(p.equals("atomic")?atomic.get():locked[0],(long)count*workers,"increments",check);}
    private Result delay(String p,boolean check)throws Exception{int count=switch(p){case"inflight-1000"->smoke?16:1000;case"inflight-10000"->smoke?32:10000;default->throw invalid();};URI uri=URI.create(service("delay_url"));allowLoopback(uri.getHost());ExecutorService executor=profile.equals("idiomatic")?Executors.newVirtualThreadPerTaskExecutor():Executors.newFixedThreadPool(Math.min(count,workers));long sum=0;try{var futures=new ArrayList<Future<Long>>();for(int i=0;i<count;i++)futures.add(executor.submit(()->{var response=http.send(HttpRequest.newBuilder(uri).timeout(Duration.ofSeconds(15)).GET().build(),HttpResponse.BodyHandlers.ofByteArray());if(response.statusCode()!=200||!Arrays.equals(response.body(),"benchmark-delay-ok".getBytes(StandardCharsets.US_ASCII)))throw new IOException("Delay response differs");return 1L;}));for(Future<Long>f:futures)sum+=f.get(20,TimeUnit.SECONDS);}finally{executor.shutdownNow();if(!executor.awaitTermination(15,TimeUnit.SECONDS))throw new IllegalStateException("Leaked delay tasks");}return scalar(sum,count,"requests",check);}
    private Result cancellation(String p,boolean check)throws Exception{if(!p.equals("deadline")&&!p.equals("child-failure"))throw invalid();int fanout=8;var executor=Executors.newFixedThreadPool(Math.min(workers,fanout));var started=new CountDownLatch(Math.min(workers,fanout));var release=new CountDownLatch(1);var active=new AtomicInteger();var futures=new ArrayList<Future<?>>();boolean observed=false;
        try{for(int i=0;i<fanout;i++){final int id=i;futures.add(executor.submit(()->{active.incrementAndGet();started.countDown();try{if(p.equals("child-failure")&&id==0)throw new IllegalStateException("injected");release.await();}catch(InterruptedException e){Thread.currentThread().interrupt();}finally{active.decrementAndGet();}}));}if(!started.await(5,TimeUnit.SECONDS))throw new TimeoutException();if(p.equals("child-failure")){try{futures.get(0).get(5,TimeUnit.SECONDS);}catch(ExecutionException expected){observed=true;}}else{try{futures.get(0).get(smoke?5:50,TimeUnit.MILLISECONDS);}catch(TimeoutException expected){observed=true;}}}finally{for(Future<?>future:futures)future.cancel(true);release.countDown();executor.shutdownNow();if(!executor.awaitTermination(5,TimeUnit.SECONDS))throw new IllegalStateException("Children did not quiesce");}if(!observed||active.get()!=0)throw new IllegalStateException("Cancellation contract failed");return new Result(0,fanout,"children",scalarBytes(0),check?Map.of("leaked_children",active.get(),"failure_or_deadline_observed",observed):null);}

    private Result json(String p,boolean check)throws Exception {
        int split=p.lastIndexOf('-');String op=p.substring(0,split);int size=Integer.parseInt(p.substring(split+1));
        if(!documents.containsKey(size))throw invalid();Document value;byte[] encoded=null;
        if(op.equals("serialize")){encoded=SuiteMain.JSON.writeValueAsBytes(documents.get(size));value=check?SuiteMain.JSON.readValue(encoded,Document.class):documents.get(size);if(encoded.length==0)throw new IllegalStateException();}
        else if(op.equals("deserialize"))value=SuiteMain.JSON.readValue(documentBytes.get(size),Document.class);else throw invalid();
        if(check&&!value.equals(documents.get(size)))throw new IllegalStateException("JSON semantic mismatch");
        Result result=bytes(documentBytes.get(size),documentBytes.get(size).length,"bytes",check);
        java.lang.ref.Reference.reachabilityFence(value);java.lang.ref.Reference.reachabilityFence(encoded);return result;
    }
    private Result protobuf(String p,boolean check)throws Exception {
        CanonicalRecord.Record value;byte[] encoded;
        if(p.equals("encode")){encoded=proto.toByteArray();value=proto;}
        else if(p.equals("decode")){value=CanonicalRecord.Record.parseFrom(protoBytes);encoded=check?value.toByteArray():protoBytes;}else throw invalid();
        if(check&&!Arrays.equals(encoded,protoBytes))throw new IllegalStateException("Protobuf canonical bytes differ");
        Result result=bytes(encoded,encoded.length,"bytes",check);java.lang.ref.Reference.reachabilityFence(value);return result;
    }
    private Result transformation(String p,boolean check){
        Map<Long,Long>sums;
        if(p.equals("pipeline"))sums=records.stream().filter(RefRecord::active).collect(Collectors.groupingBy(RefRecord::group,TreeMap::new,Collectors.summingLong(r->r.amount*2)));
        else {require(p,"imperative");sums=new TreeMap<>();for(RefRecord r:records)if(r.active)sums.merge(r.group,r.amount*2,Long::sum);}
        var lines=new ArrayList<String>();for(var e:sums.entrySet())lines.add(e.getKey()+":"+e.getValue());
        return bytes(String.join("\n",lines).getBytes(StandardCharsets.UTF_8),n,"records",check);
    }
    private Result csv(String p,boolean check)throws Exception {
        if(p.equals("parse")){
            List<List<String>> rows=parseCsv(csvText);
            if(check&&(!rows.equals(csvRows)||!emitCsv(rows).equals(csvText)))throw new IllegalStateException("CSV parse roundtrip differs");
            Result result=bytes(csvBytes,rows.size()-1,"rows",check);java.lang.ref.Reference.reachabilityFence(rows);return result;
        }
        require(p,"emit");byte[] encoded=emitCsv(csvRows).getBytes(StandardCharsets.UTF_8);
        if(check&&(!Arrays.equals(encoded,csvBytes)||!parseCsv(strictDecode(encoded)).equals(csvRows)))throw new IllegalStateException("CSV emit roundtrip differs");
        return bytes(encoded,csvRows.size()-1,"rows",check);
    }

    private void initializeScratch()throws Exception {
        if(temporary!=null)return;
        String configured=System.getenv("BENCH_SCRATCH_ROOT");
        Path parent=Path.of(configured==null||configured.isBlank()?System.getProperty("java.io.tmpdir"):configured).toAbsolutePath().normalize();
        Files.createDirectories(parent);
        for(int attempt=0;attempt<10;attempt++){
            String token=UUID.randomUUID().toString().replace("-","");Path candidate=parent.resolve("worker-java-"+ProcessHandle.current().pid()+"-"+token);
            try{Files.createDirectory(candidate);}catch(FileAlreadyExistsException collision){continue;}
            var ownership=new LinkedHashMap<String,Object>();var started=ProcessHandle.current().info().startInstant();
            ownership.put("schema_version",1);ownership.put("kind","benchmark-worker-scratch");ownership.put("language","java");ownership.put("pid",ProcessHandle.current().pid());
            ownership.put("process_start_utc",started.map(Object::toString).orElse(null));ownership.put("process_start_unix_ms",started.map(java.time.Instant::toEpochMilli).orElse(null));
            ownership.put("token",token);ownership.put("scratch_root_source",configured==null||configured.isBlank()?"os_temp":"environment");
            Files.write(candidate.resolve("ownership.json"),SuiteMain.JSON.writeValueAsBytes(ownership),StandardOpenOption.CREATE_NEW);
            temporary=candidate;scratchToken=token;file=candidate.resolve("io.bin");scratchFiles.add(file);return;
        }
        throw new IOException("Cannot allocate a unique worker scratch directory");
    }
    private void prepareIo()throws Exception {
        ioPrepared=false;if(random.length==0)throw new IllegalStateException("Empty canonical I/O corpus");initializeScratch();
        long length=smoke?65536:1073741824;Path staging=temporary.resolve("io-prepare-"+UUID.randomUUID().toString().replace("-","")+".tmp");scratchFiles.add(staging);
        try(var output=FileChannel.open(staging,StandardOpenOption.CREATE_NEW,StandardOpenOption.WRITE)){
            for(long written=0;written<length;){int offset=(int)(written%random.length),amount=(int)Math.min(Math.min(65536,random.length-offset),length-written);
                var chunk=ByteBuffer.wrap(random,offset,amount);while(chunk.hasRemaining())output.write(chunk);written+=amount;}
            output.force(true);
        }
        try(var input=FileChannel.open(staging,StandardOpenOption.READ)){
            var buffer=ByteBuffer.allocate(65536);
            for(long read=0;read<length;){int offset=(int)(read%random.length),amount=(int)Math.min(Math.min(buffer.capacity(),random.length-offset),length-read);
                buffer.clear();buffer.limit(amount);while(buffer.hasRemaining())if(input.read(buffer)<0)throw new EOFException("Incomplete canonical I/O preparation");
                if(!Arrays.equals(buffer.array(),0,amount,random,offset,offset+amount))throw new IllegalStateException("I/O preparation differs from canonical bytes");read+=amount;}
            buffer.clear();buffer.limit(1);if(input.read(buffer)!=-1)throw new IllegalStateException("I/O preparation length differs");
        }
        try{Files.move(staging,file,StandardCopyOption.ATOMIC_MOVE,StandardCopyOption.REPLACE_EXISTING);}
        catch(AtomicMoveNotSupportedException unsupported){Files.move(staging,file,StandardCopyOption.REPLACE_EXISTING);}
        scratchFiles.remove(staging);ioPrepared=true;
    }
    private void requirePreparedIo(){if(!ioPrepared||file==null)throw new IllegalStateException("PREPARE is required before I/O execution");}
    private Result sequential(String p,boolean check)throws Exception {
        String[]v=p.split("-",2);if(v.length!=2||(!v[1].equals("buffered")&&!v[1].equals("durable-flush")))throw invalid();
        requirePreparedIo();long length=smoke?65536:1073741824,checksum=0;
        if(v[0].equals("write")){
            try(var channel=FileChannel.open(file,StandardOpenOption.WRITE,StandardOpenOption.TRUNCATE_EXISTING)){
                for(long written=0;written<length;){int offset=(int)(written%random.length),amount=(int)Math.min(Math.min(65536,random.length-offset),length-written);
                    var buffer=ByteBuffer.wrap(random,offset,amount);while(buffer.hasRemaining())channel.write(buffer);
                    for(int i=offset;i<offset+amount;i++)checksum+=random[i]&255;written+=amount;}
                if(v[1].equals("durable-flush"))channel.force(true);
            }
            if(check&&fileByteSum()!=checksum)throw new IllegalStateException("Written file differs");
        }else {require(v[0],"read");checksum=fileByteSum();}
        return scalar(checksum,length,"bytes",check);
    }
    private long fileByteSum()throws Exception{long sum=0;try(var channel=FileChannel.open(file,StandardOpenOption.READ)){var buffer=ByteBuffer.allocate(65536);while(channel.read(buffer)>0){buffer.flip();while(buffer.hasRemaining())sum+=buffer.get()&255;buffer.clear();}}return sum;}
    private Result randomReads(String p,boolean check)throws Exception{require(p,"random-4KiB");requirePreparedIo();int count=smoke?16:256;long blocks=Files.size(file)/4096,sum=0;try(var channel=FileChannel.open(file,StandardOpenOption.READ)){var buffer=ByteBuffer.allocate(4096);for(int i=0;i<count;i++){buffer.clear();long offset=((long)i*7919%blocks)*4096;while(buffer.hasRemaining()){int read=channel.read(buffer,offset+buffer.position());if(read<0)throw new EOFException();}sum+=buffer.array()[0]&255;}}return scalar(sum,count,"reads",check);}
    private Result network(String p,boolean secure,boolean check)throws Exception{byte[]payload=random;Socket socket;boolean close=false;if(!secure){require(p,"echo-1MiB");if(echo==null)echo=openSocket(false);socket=echo;}else if(p.equals("handshake")){socket=openSocket(true);close=true;}else{require(p,"persistent-transfer");if(tls==null)tls=(SSLSocket)openSocket(true);socket=tls;}
        try{if(secure&&p.equals("handshake"))return new Result(1,1,"handshakes",scalarBytes(1),check?Map.of("tls_protocol",((SSLSocket)socket).getSession().getProtocol(),"cipher",((SSLSocket)socket).getSession().getCipherSuite()):null);
            Socket connection=socket;Future<byte[]>reader=pool.submit(()->connection.getInputStream().readNBytes(payload.length));socket.getOutputStream().write(payload);socket.getOutputStream().flush();byte[]response=reader.get(20,TimeUnit.SECONDS);if(response.length!=payload.length||!Arrays.equals(response,payload))throw new IOException("Echo differs");return bytes(response,response.length,"bytes",check);}finally{if(close){((SSLSocket)socket).getSession().invalidate();socket.close();}}
    }
    private void initializeTls()throws Exception {
        if(tlsContext!=null)return;
        var certificates=CertificateFactory.getInstance("X.509");var store=KeyStore.getInstance(KeyStore.getDefaultType());store.load(null,null);
        try(var input=Files.newInputStream(Path.of(service("tls_certificate")))){store.setCertificateEntry("benchmark-loopback",certificates.generateCertificate(input));}
        var trust=TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());trust.init(store);
        tlsContext=SSLContext.getInstance("TLSv1.3");tlsContext.init(null,trust.getTrustManagers(),new SecureRandom());
    }
    private Socket openSocket(boolean secure)throws Exception {
        String host=service("echo_host");allowLoopback(host);int port=((Number)services.get(secure?"tls_port":"echo_port")).intValue();Socket socket;
        if(secure){initializeTls();SSLSocket s=(SSLSocket)tlsContext.getSocketFactory().createSocket(host,port);s.setEnabledProtocols(new String[]{"TLSv1.3"});
            SSLParameters parameters=s.getSSLParameters();parameters.setEndpointIdentificationAlgorithm("HTTPS");s.setSSLParameters(parameters);s.setSoTimeout(15000);s.startHandshake();socket=s;
        }else socket=new Socket(host,port);
        socket.setTcpNoDelay(true);socket.setSoTimeout(15000);return socket;
    }

    private Result nativeCall(String id,String p,boolean check)throws Exception{initializeNative();try{if(id.equals("N01")){int count=switch(p){case"count-1"->1;case"count-1024"->1024;default->throw invalid();};long value=(long)nativeSum.invokeExact(nativeInts,(long)count);return scalar(value,count,"integers",check);}require(p,"preallocated");long value=(long)nativeBuffer.invokeExact(nativeBytes,(long)random.length);return scalar(value,random.length,"bytes",check);}catch(Throwable t){if(t instanceof Exception e)throw e;throw new IllegalStateException("Native invocation failed",t);}}
    private void initializeNative(){if(nativeArena!=null)return;String library=System.getenv("BENCH_NATIVE_LIBRARY");if(library==null)throw new IllegalArgumentException("BENCH_NATIVE_LIBRARY is required for native cases");nativeArena=Arena.ofShared();var lookup=SymbolLookup.libraryLookup(Path.of(library).toAbsolutePath(),nativeArena);var linker=Linker.nativeLinker();var descriptor=FunctionDescriptor.of(ValueLayout.JAVA_LONG,ValueLayout.ADDRESS,ValueLayout.JAVA_LONG);nativeSum=linker.downcallHandle(lookup.find("bench_sum").orElseThrow(),descriptor);nativeBuffer=linker.downcallHandle(lookup.find("bench_buffer").orElseThrow(),descriptor);nativeInts=nativeArena.allocate(1024*8L,8);for(int i=0;i<1024;i++)nativeInts.setAtIndex(ValueLayout.JAVA_LONG,i,i-512L);nativeBytes=nativeArena.allocate(random.length);MemorySegment.copy(random,0,nativeBytes,ValueLayout.JAVA_BYTE,0,random.length);}

    private Result representations(String p,boolean check){long sum=0;switch(p){case"primitive-array"->{long[]a=new long[n];for(int i=0;i<n;i++)a[i]=i;for(long value:a)sum+=value;}case"boxed-reference","reference-generic"->{var list=new ArrayList<Long>(n);for(long i=0;i<n;i++)list.add(i);for(Long value:list)sum+=value;}case"idiomatic-record"->{var list=makeRecords();for(RefRecord value:list)sum+=value.id;}default->throw invalid();}return scalar(sum,n,"items",check);}
    private Result dispatch(String p,boolean check){if(!p.equals("switch")&&!p.equals("pattern"))throw invalid();long sum=0;for(Tagged input:taggedInputs){long value;if(p.equals("switch"))value=switch(input.tag){case 0->input.id+1;case 1->input.id*2;case 2->input.id^85;default->input.id-3;};else value=switch(input){case Tagged(long id,int tag) when tag==0->id+1;case Tagged(long id,int tag) when tag==1->id*2;case Tagged(long id,int tag) when tag==2->id^85;default->input.id-3;};sum+=value;}return scalar(sum,n,"items",check);}
    private Result exceptions(String p,boolean check){int threshold=switch(p){case"failure-0pct"->0;case"failure-0.1pct"->1;case"failure-1pct"->10;case"failure-10pct"->100;default->throw invalid();};long sum=0;for(int i=0;i<n;i++){try{sum+=call1(i,i%1000<threshold);}catch(IllegalStateException expected){expected.getStackTrace();sum++;}}return scalar(sum,n,"items",check);}
    private static long call1(int i,boolean fail){return call2(i,fail);}private static long call2(int i,boolean fail){return call3(i,fail);}private static long call3(int i,boolean fail){if(fail)throw new IllegalStateException("benchmark");return i;}
    public static long target(long x){return x*3+1;}
    private Result invocation(String p,boolean check)throws Exception{long sum=0;if(p.equals("invoke-capturing")){long offset=7;LongUnaryOperator function=x->target(x)+offset;for(int i=0;i<n;i++)sum+=function.applyAsLong(i);}else if(p.equals("invoke-noncapturing")){LongUnaryOperator function=Workloads::target;for(int i=0;i<n;i++)sum+=function.applyAsLong(i);}else{boolean cached=p.endsWith("cached")&&!p.endsWith("uncached");if(!p.startsWith("reflection-lookup-")&&!p.startsWith("reflection-invoke-"))throw invalid();Method method=Workloads.class.getMethod("target",long.class);for(int i=0;i<n;i++){Method selected=cached?method:Workloads.class.getMethod("target",long.class);if(p.startsWith("reflection-invoke-"))sum+=(long)selected.invoke(null,(long)i);else{if(selected.getParameterCount()!=1)throw new IllegalStateException();sum+=target(i);}}}return scalar(sum,n,"items",check);}

    static List<List<String>> parseCsv(String text){var rows=new ArrayList<List<String>>();var row=new ArrayList<String>();var field=new StringBuilder();boolean quoted=false,closed=false;for(int i=0;i<text.length();i++){char c=text.charAt(i);if(quoted){if(c=='"'){if(i+1<text.length()&&text.charAt(i+1)=='"'){field.append('"');i++;}else{quoted=false;closed=true;}}else field.append(c);}else if(c=='"'&&field.length()==0&&!closed)quoted=true;else if(c==','){row.add(field.toString());field.setLength(0);closed=false;}else if(c=='\r'||c=='\n'){if(c=='\r'&&i+1<text.length()&&text.charAt(i+1)=='\n')i++;row.add(field.toString());rows.add(row);row=new ArrayList<>();field.setLength(0);closed=false;}else{if(closed||c=='"')throw new IllegalArgumentException("Malformed CSV quoting");field.append(c);}}if(quoted)throw new IllegalArgumentException("Unterminated CSV field");if(field.length()>0||!row.isEmpty()){row.add(field.toString());rows.add(row);}return rows;}
    static String emitCsv(List<List<String>>rows){var text=new StringBuilder();for(List<String>row:rows){for(int i=0;i<row.size();i++){if(i>0)text.append(',');String field=row.get(i);boolean quote=field.indexOf(',')>=0||field.indexOf('"')>=0||field.indexOf('\r')>=0||field.indexOf('\n')>=0;if(quote)text.append('"').append(field.replace("\"","\"\"")).append('"');else text.append(field);}text.append("\r\n");}return text.toString();}
    private String service(String key){Object value=services.get(key);if(!(value instanceof String text)||text.isBlank())throw new IllegalArgumentException("Missing controller-owned service: "+key);return text;}
    private static void allowLoopback(String host)throws Exception{if(host==null||!InetAddress.getByName(host).isLoopbackAddress())throw new IllegalArgumentException("Only controller-owned loopback services allowed");}
    static long fnv(byte[]bytes){long hash=0xcbf29ce484222325L;for(byte value:bytes){hash^=value&255;hash*=0x100000001b3L;}return hash;}
    private static byte[]scalarBytes(long value){return Long.toUnsignedString(value).getBytes(StandardCharsets.US_ASCII);}
    private static Result scalar(long value,long units,String unit,boolean check){return new Result(value,units,unit,check?scalarBytes(value):new byte[0],null);}
    private static Result bytes(byte[]bytes,long units,String unit,boolean check){return new Result(fnv(bytes),units,unit,check?bytes:new byte[0],null);}
    private static void require(String actual,String expected){if(!actual.equals(expected))throw invalid();}
    private static IllegalArgumentException invalid(){return new IllegalArgumentException("Unknown parameter for case");}
    private static String strictDecode(byte[]bytes)throws CharacterCodingException{return StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT).onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(bytes)).toString();}
    @FunctionalInterface interface CleanupAction {void run()throws Exception;}
    private static void cleanupAttempt(List<String> errors,String name,CleanupAction action){
        try{action.run();}catch(Exception error){errors.add(name+":"+error.getClass().getSimpleName());if(error instanceof InterruptedException)Thread.currentThread().interrupt();}
    }
    private void cleanupScratch(List<String> errors)throws Exception {
        if(temporary==null||scratchToken==null)return;Path marker=temporary.resolve("ownership.json");var ownership=SuiteMain.JSON.readTree(Files.readAllBytes(marker));
        if(!scratchToken.equals(ownership.path("token").asText())||!"benchmark-worker-scratch".equals(ownership.path("kind").asText()))throw new IOException("Worker scratch ownership marker differs");
        for(Path path:scratchFiles)cleanupAttempt(errors,"scratch-file",()->Files.deleteIfExists(path));
        try(var remaining=Files.list(temporary)){if(remaining.anyMatch(path->!path.equals(marker))){errors.add("scratch-directory:not-empty");return;}}
        cleanupAttempt(errors,"scratch-marker",()->Files.deleteIfExists(marker));cleanupAttempt(errors,"scratch-directory",()->Files.deleteIfExists(temporary));
    }
    public void close(){
        if(closed)return;closed=true;var errors=new ArrayList<String>();
        cleanupAttempt(errors,"plain-echo",()->{if(echo!=null)echo.close();});cleanupAttempt(errors,"secure-echo",()->{if(tls!=null)tls.close();});
        cleanupAttempt(errors,"http-client",()->{http.shutdownNow();if(!http.awaitTermination(Duration.ofSeconds(5)))throw new IllegalStateException("HTTP client termination incomplete");});
        cleanupAttempt(errors,"platform-pool",()->{pool.shutdownNow();if(!pool.awaitTermination(5,TimeUnit.SECONDS))throw new IllegalStateException("Pool termination incomplete");});
        cleanupAttempt(errors,"native-arena",()->{if(nativeArena!=null)nativeArena.close();});cleanupAttempt(errors,"scratch",()->cleanupScratch(errors));
        if(!errors.isEmpty())try{System.err.println(SuiteMain.JSON.writeValueAsString(Map.of("status","cleanup_incomplete","language","java","errors",errors)));}catch(Exception ignored){}
    }
}
