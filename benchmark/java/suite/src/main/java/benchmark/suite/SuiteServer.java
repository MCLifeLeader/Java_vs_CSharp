package benchmark.suite;

import benchmark.database.DatabaseSession;
import jakarta.servlet.Filter;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.net.InetAddress;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.web.servlet.context.ServletWebServerApplicationContext;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.context.annotation.Bean;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@SpringBootApplication
@RestController
public class SuiteServer {
    private static Path data;
    private static Map<Integer,Workloads.Document> documents;
    private static volatile long[] computeIntegers;
    private static DatabaseSession database;
    private static final AtomicInteger activeRequests=new AtomicInteger();
    private static final Object admissionGate=new Object();
    private static boolean accepting=true;

    static void start(Path input,Map<String,String> options)throws Exception {
        data=input.toAbsolutePath();
        var loaded=new HashMap<Integer,Workloads.Document>();
        for(int size:new int[]{1024,65536,1048576})loaded.put(size,SuiteMain.JSON.readValue(Files.readAllBytes(data.resolve("document-"+size+".json")),Workloads.Document.class));
        documents=Map.copyOf(loaded);
        if(options.containsKey("engine")) {
            Path root=Path.of(options.getOrDefault("root","benchmark")).toAbsolutePath();
            java.io.PrintStream protocol=System.out;
            try { System.setOut(System.err);database=new DatabaseSession(root,options.get("engine"),options.getOrDefault("access","raw"),data); }
            finally {System.setOut(protocol);}
        }
        SpringApplication application=new SpringApplication(SuiteServer.class);
        application.setDefaultProperties(Map.of("server.address","127.0.0.1","server.port",options.getOrDefault("port","0"),
            "spring.main.banner-mode","off","logging.level.root","WARN","spring.main.log-startup-info","false",
            "server.compression.enabled","false","server.tomcat.accesslog.enabled","false",
            "spring.jackson.deserialization.fail-on-unknown-properties","true",
            "spring.jackson.mapper.allow-coercion-of-scalars","false","spring.jackson.deserialization.accept-float-as-int","false"));
        ConfigurableApplicationContext context=application.run();
        ((ch.qos.logback.classic.LoggerContext)org.slf4j.LoggerFactory.getILoggerFactory()).getLoggerList().forEach(logger->logger.setLevel(ch.qos.logback.classic.Level.OFF));
        int port=((ServletWebServerApplicationContext)context).getWebServer().getPort();
        SuiteMain.emit(Map.of("status","ready","protocol",2,"language","java","runtime",Runtime.version().toString(),
            "port",port,"framework","Spring Boot MVC/Tomcat","supported",database==null?List.of("H01","H02","H03"):List.of("H01","H02","H03","H04","H05","H06")));
        Runtime.getRuntime().addShutdownHook(new Thread(()->{context.close();if(database!=null)try{database.close();}catch(Exception ignored){}}));
        new java.util.concurrent.CountDownLatch(1).await();
    }

    @Bean
    Filter requestAccounting(){
        return (request,response,chain)->{
            HttpServletRequest http=(HttpServletRequest)request;
            if(http.getRequestURI().startsWith("/__benchmark/")){chain.doFilter(request,response);return;}
            synchronized(admissionGate){
                if(!accepting){((HttpServletResponse)response).setStatus(503);return;}
                activeRequests.incrementAndGet();
            }
            try{chain.doFilter(request,response);}finally{activeRequests.decrementAndGet();}
        };
    }
    private static boolean loopback(HttpServletRequest request){
        try{return request.getRemoteAddr()!=null&&InetAddress.getByName(request.getRemoteAddr()).isLoopbackAddress();}
        catch(Exception error){return false;}
    }
    private static long[] integers()throws Exception{
        if(computeIntegers==null)synchronized(SuiteServer.class){
            if(computeIntegers==null){
                byte[] bytes=Files.readAllBytes(data.resolve("integers.i64le"));
                if(bytes.length==0||bytes.length%8!=0)throw new IllegalArgumentException("Malformed integer fixture");
                var input=ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN);long[] values=new long[bytes.length/8];
                for(int i=0;i<values.length;i++)values[i]=input.getLong();computeIntegers=values;
            }
        }
        return computeIntegers;
    }
    @GetMapping(value="/plaintext",produces="text/plain;charset=UTF-8")
    ResponseEntity<byte[]> plaintext(){return ResponseEntity.ok().contentType(MediaType.parseMediaType("text/plain;charset=UTF-8"))
        .body("0123456789abcdef0123456789abcdef".getBytes(StandardCharsets.UTF_8));}
    @GetMapping(value="/json",produces="application/json")
    ResponseEntity<Workloads.Document> json(@RequestParam(defaultValue="1024")int size){
        Workloads.Document document=documents.get(size);if(document==null)throw new IllegalArgumentException("Invalid size");
        return ResponseEntity.ok().contentType(MediaType.APPLICATION_JSON).body(document);
    }
    @PostMapping(value="/json",consumes="application/json",produces="application/json")
    ResponseEntity<Workloads.Document> post(@RequestParam(defaultValue="1024")int size,@RequestBody Workloads.Document document){
        Workloads.Document expected=documents.get(size);if(expected==null||!expected.equals(document))throw new IllegalArgumentException("Invalid document");
        return ResponseEntity.ok().contentType(MediaType.APPLICATION_JSON).body(document);
    }
    @GetMapping(value="/compute",produces="text/plain;charset=UTF-8")
    ResponseEntity<String> compute()throws Exception{return ResponseEntity.ok().contentType(MediaType.parseMediaType("text/plain;charset=UTF-8"))
        .body(String.format("%016x",Workloads.transform(integers())));}
    @GetMapping(value="/customer/{id}",produces="application/json")
    ResponseEntity<Object> customer(@PathVariable long id)throws Exception{
        if(database==null)return ResponseEntity.status(503).build();
        return ResponseEntity.ok(database.execute("D01",Map.of("customer_id",id)));
    }
    @GetMapping(value="/order/{id}",produces="application/json")
    ResponseEntity<Object> order(@PathVariable long id)throws Exception{
        if(database==null)return ResponseEntity.status(503).build();
        return ResponseEntity.ok(database.execute("D03",Map.of("order_id",id)));
    }
    @PostMapping(value="/order",consumes="application/json",produces="application/json")
    ResponseEntity<Object> postOrder(@RequestBody Map<String,Object> input)throws Exception{
        if(database==null)return ResponseEntity.status(503).build();
        return ResponseEntity.status(201).body(database.execute("D06",input));
    }
    @PostMapping(value="/checkout",consumes="application/json",produces="application/json")
    ResponseEntity<Object> checkout(@RequestBody Map<String,Object> input)throws Exception{
        if(database==null)return ResponseEntity.status(503).build();
        return ResponseEntity.status(201).body(database.checkout(input));
    }
    @GetMapping(value="/__benchmark/active",produces="application/json")
    ResponseEntity<Object> active(HttpServletRequest request){
        if(!loopback(request))return ResponseEntity.status(403).build();
        return ResponseEntity.ok(Map.of("active",activeRequests.get()));
    }
    @PostMapping(value="/__benchmark/quiesce",produces="application/json")
    ResponseEntity<Object> quiesce(HttpServletRequest request)throws Exception{
        if(!loopback(request))return ResponseEntity.status(403).build();
        synchronized(admissionGate){accepting=false;}
        long deadline=System.nanoTime()+30_000_000_000L;
        while(activeRequests.get()!=0&&System.nanoTime()<deadline)Thread.sleep(5);
        return ResponseEntity.status(activeRequests.get()==0?200:503).body(Map.of("active",activeRequests.get()));
    }
    @PostMapping(value="/__benchmark/reset",produces="application/json")
    ResponseEntity<Object> reset(HttpServletRequest request)throws Exception{
        if(!loopback(request))return ResponseEntity.status(403).build();
        Object result=database==null?Map.of("status","ready"):database.reset();
        synchronized(admissionGate){accepting=true;}
        return ResponseEntity.ok(result);
    }
    @GetMapping(value="/__benchmark/state",produces="application/json")
    ResponseEntity<Object> state(HttpServletRequest request)throws Exception{
        if(!loopback(request))return ResponseEntity.status(403).build();
        if(database==null)return ResponseEntity.status(503).build();
        return ResponseEntity.ok(database.stateDigest());
    }
    @GetMapping(value="/__benchmark/metadata",produces="application/json")
    ResponseEntity<Object> metadata(HttpServletRequest request)throws Exception{
        if(!loopback(request))return ResponseEntity.status(403).build();
        if(database==null)return ResponseEntity.status(503).build();
        return ResponseEntity.ok(database.metadata());
    }
    @PostMapping(value="/__benchmark/committed",consumes="application/json",produces="application/json")
    ResponseEntity<Object> committed(HttpServletRequest request,@RequestBody List<Long> ids)throws Exception{
        if(!loopback(request))return ResponseEntity.status(403).build();
        if(database==null)return ResponseEntity.status(503).build();
        if(ids.size()>500||ids.stream().anyMatch(id->id==null||id<=0))throw new IllegalArgumentException("Invalid audit IDs");
        return ResponseEntity.ok(database.committedOrders(ids));
    }
    @ExceptionHandler(Exception.class)
    ResponseEntity<Map<String,Object>> failure(Exception error){
        int status=error instanceof IllegalArgumentException||error instanceof org.springframework.http.converter.HttpMessageNotReadableException||error instanceof org.springframework.web.method.annotation.MethodArgumentTypeMismatchException?400:
            error instanceof jakarta.persistence.OptimisticLockException?409:500;
        String code=status==400?"invalid_request":status==409?"conflict":"operation_failed";
        return ResponseEntity.status(status).body(Map.of("error",code));
    }
}
