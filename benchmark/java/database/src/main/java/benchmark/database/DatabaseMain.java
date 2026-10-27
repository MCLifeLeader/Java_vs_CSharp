package benchmark.database;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.*;

public final class DatabaseMain {
    public static void main(String[] args)throws Exception{
        var json=new ObjectMapper();
        try{
            if(args.length!=4)throw new IllegalArgumentException("Expected root engine access CSV-directory");
            try(var db=new DatabaseSession(Path.of(args[0]),args[1],args[2],Path.of(args[3]))){
                emit(json,DatabaseSession.map("status","ready","metadata",db.metadata(),"state",db.stateDigest()));
                var reader=new BufferedReader(new InputStreamReader(System.in,StandardCharsets.UTF_8));String text;
                while((text=reader.readLine())!=null)try{
                    Map<String,Object> request=json.readValue(text,new TypeReference<>(){});String id=(String)request.get("case_id");if("QUIT".equals(id))break;
                    @SuppressWarnings("unchecked")var input=(Map<String,Object>)request.getOrDefault("input",Map.of());
                    @SuppressWarnings("unchecked")var trace=(List<Map<String,Object>>)request.get("trace");
                    Object result=switch(id){case "STATE"->db.stateDigest();case "METADATA"->db.metadata();case "RESET"->db.reset(Boolean.TRUE.equals(request.get("inject_failure")));case "AUDIT"->db.audit((String)request.get("workload"),trace);case "COMMITTED"->db.committedOrders(((List<?>)request.get("ids")).stream().map(value->((Number)value).longValue()).toList());case "BATCH"->db.batch((String)request.get("workload"),trace,((Number)request.get("iterations")).intValue(),((Number)request.getOrDefault("workers",1)).intValue());case "H06"->db.checkout(input);case "H04"->db.execute("D01",input);case "H05-read"->db.execute("D03",input);case "H05-write"->db.execute("D06",input);default->db.execute(id,input);};
                    emit(json,DatabaseSession.map("status","ok","case_id",id,"result",result));
                }catch(Exception e){emit(json,DatabaseSession.map("status","error","reason",e.getClass().getSimpleName()));}
            }
        }catch(Exception e){emit(json,DatabaseSession.map("status","blocked","reason",e.getClass().getSimpleName()));System.exit(1);}
    }
    private static void emit(ObjectMapper json,Object value)throws Exception{System.out.println(json.writeValueAsString(value));System.out.flush();}
}
