package benchmark.suite;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.LinkedHashMap;
import java.util.Map;

public final class SuiteMain {
    static final ObjectMapper JSON = new ObjectMapper();
    public static void main(String[] args) throws Exception {
        System.setProperty("debug","false");System.setProperty("logging.level.root","OFF");
        System.setProperty("logging.level.org.springframework","OFF");System.setProperty("logging.level.org.hibernate","OFF");
        Map<String,String> options = new LinkedHashMap<>();
        boolean server=false, jmh=false;
        for (int i=0;i<args.length;i++) {
            if(args[i].equals("--server")) server=true;
            else if(args[i].equals("--jmh")) jmh=true;
            else if(args[i].startsWith("--") && i+1<args.length) options.put(args[i].substring(2),args[++i]);
            else throw new IllegalArgumentException("Unknown argument");
        }
        Path data=Path.of(options.getOrDefault("data","."));
        String scale=options.getOrDefault("scale","smoke");
        if(!scale.equals("smoke")&&!scale.equals("S"))throw new IllegalArgumentException("Scale must be smoke or S");
        boolean smoke=scale.equals("smoke");
        int workers=Integer.parseInt(options.getOrDefault("workers","4"));
        if(workers<1 || workers>256) throw new IllegalArgumentException("Worker count outside1..256");
        if(server) { SuiteServer.start(data,options); return; }
        if(jmh) throw new IllegalArgumentException("Launch JMH with java/suite/run_jmh.py so forked JVMs receive the complete classpath");
        try(Workloads workloads=new Workloads(data,smoke,workers,options.getOrDefault("profile","controlled"))) {
            emit(Map.of("status","ready","protocol",2,"language","java","runtime",Runtime.version().toString(),
                        "supported",Workloads.SUPPORTED,"scale",smoke?"smoke":"S","workers",workers));
            var reader=new BufferedReader(new InputStreamReader(System.in,StandardCharsets.UTF_8));
            String line;
            while((line=reader.readLine())!=null) {
                if(line.equals("QUIT")) break;
                try {
                    String[] fields=line.split("\t",-1);
                    if(fields.length==4&&fields[0].equals("PREPARE")) {
                        int count=Integer.parseInt(fields[3]);workloads.prepare(fields[1],fields[2],count);
                        emit(Map.of("status","prepared","case_id",fields[1],"parameter_id",fields[2],"iterations",count));continue;
                    }
                    boolean check=fields.length==3 && fields[0].equals("CHECK");
                    boolean batch=fields.length==4 && fields[0].equals("BATCH");
                    if(!check && !batch) throw new IllegalArgumentException("Expected CHECK or BATCH");
                    int iterations=check?1:Integer.parseInt(fields[3]);
                    if(iterations<1 || iterations>100000000) throw new IllegalArgumentException("Invalid iterations");
                    long checksum=0, units=0;
                    Workloads.Result result=null;
                    for(int i=0;i<iterations;i++) {
                        result=workloads.run(fields[1],fields[2],check);
                        checksum+=result.checksum(); units=Math.addExact(units,result.units());
                    }
                    Map<String,Object> output=new LinkedHashMap<>();
                    output.put("status","ok");output.put("case_id",fields[1]);output.put("parameter_id",fields[2]);
                    output.put("checksum",String.format("%016x",checksum));output.put("work_units",units);
                    output.put("unit",result.unit());output.put("correctness",true);
                    if(batch) output.put("iterations",iterations);
                    if(check) {
                        output.put("semantic_hash",hex(MessageDigest.getInstance("SHA-256").digest(result.semantic())));
                        if(result.details()!=null) output.put("details",result.details());
                    }
                    emit(output);
                } catch(Exception exception) {
                    emit(Map.of("status","error","reason",exception.getClass().getSimpleName(),"message",safeMessage(exception)));
                }
            }
        }
    }
    private static String safeMessage(Exception e) {
        return e instanceof IllegalArgumentException?String.valueOf(e.getMessage()):"Operation failed; inspect separately retained diagnostics";
    }
    static String hex(byte[] bytes){return java.util.HexFormat.of().formatHex(bytes);}
    static void emit(Object value)throws Exception{System.out.println(JSON.writeValueAsString(value));System.out.flush();}
}
