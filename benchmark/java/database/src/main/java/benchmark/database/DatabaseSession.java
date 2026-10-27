package benchmark.database;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.apache.commons.csv.CSVFormat;
import org.hibernate.Session;
import org.hibernate.SessionFactory;
import org.hibernate.boot.MetadataSources;
import org.hibernate.boot.registry.StandardServiceRegistryBuilder;
import java.io.*;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.MessageDigest;
import java.sql.*;
import java.util.*;
import java.util.regex.*;
import java.util.stream.Collectors;

import static benchmark.database.Model.*;

/** One process owns one anchor/factory/pool. Every logical operation leases a connection and fresh Session. */
public final class DatabaseSession implements AutoCloseable {
    private final Path root;
    private final String engine, access;
    private final Connection anchor;
    private final HikariDataSource pool;
    private SessionFactory factory;
    private final Map<String,String> queries = new HashMap<>();
    private final Path dataDirectory;
    private final boolean ownsInitializedNetwork;
    private final Map<String,String> fixtureHashes;
    private final Map<String,Object> canonicalState;
    private volatile boolean requiresReset;
    private static final String[][] TABLES={{"customers.csv","customer","id,email,name,balance_cents,version"},{"products.csv","product","id,sku,name,price_cents"},{"orders.csv","orders","id,customer_id,status,created_epoch_ms,total_cents,version"},{"order_lines.csv","order_line","id,order_id,line_no,product_id,quantity,unit_price_cents"}};
    private static volatile Object blackhole;
    private static Path nativeDirectory;
    private static final Pattern PARAM = Pattern.compile(":([a-z_]+)");

    public DatabaseSession(Path root, String engine, String access, Path data) throws Exception {
        this.root=root.toAbsolutePath(); this.engine=engine; this.access=access; this.dataDirectory=data;this.ownsInitializedNetwork=!engine.equals("sqlite")&&"1".equals(System.getenv("BENCH_DB_INITIALIZE"));
        if (!List.of("raw","orm").contains(access)) throw new IllegalArgumentException("Unknown access");
        if(data==null)throw new IllegalArgumentException("Canonical CSV directory required for verified startup");
        fixtureHashes=new LinkedHashMap<>();for(var table:TABLES)fixtureHashes.put(table[0],fileHash(data.resolve(table[0])));
        canonicalState=fixtureState(data);verifyFixtures();
        var cfg=new HikariConfig(); cfg.setMaximumPoolSize(engine.equals("sqlite")?1:16); cfg.setMinimumIdle(0);
        cfg.setConnectionTimeout(15000); cfg.setPoolName("benchmark-owned");
        String url;
        if (engine.equals("sqlite")) {
            synchronized(DatabaseSession.class) { if(nativeDirectory==null){ nativeDirectory=Files.createTempDirectory("bench-sqlite-native-"); System.setProperty("org.sqlite.tmpdir",nativeDirectory.toString()); } }
            Class.forName("org.sqlite.JDBC");
            url="jdbc:sqlite:file:bench_"+UUID.randomUUID().toString().replace("-","")+"?mode=memory&cache=shared";
            anchor=DriverManager.getConnection(url); configure(anchor);
        } else {
            anchor=null; url=System.getenv("BENCH_DB_CONNECTION"); guard(engine,url);
            String user=System.getenv("BENCH_DB_USER"), password=System.getenv("BENCH_DB_PASSWORD");
            if(user!=null) cfg.setUsername(user); if(password!=null)cfg.setPassword(password);
            if(engine.equals("postgresql")) { cfg.addDataSourceProperty("prepareThreshold","0"); cfg.addDataSourceProperty("preparedStatementCacheQueries","0"); cfg.addDataSourceProperty("connectTimeout","15"); cfg.addDataSourceProperty("socketTimeout","15"); }
            else { cfg.addDataSourceProperty("loginTimeout","15"); cfg.addDataSourceProperty("queryTimeout","15"); cfg.addDataSourceProperty("connectRetryCount","0"); cfg.addDataSourceProperty("disableStatementPooling","true"); }
        }
        cfg.setJdbcUrl(url);try{pool=new HikariDataSource(cfg);}catch(Exception error){if(anchor!=null)try{anchor.close();}catch(Exception cleanup){error.addSuppressed(cleanup);}throw error;}
        try{
        try(var paths=Files.list(this.root.resolve("queries").resolve(engine))) {
            for(Path p:paths.filter(p->p.toString().endsWith(".sql")).toList()) queries.put(p.getFileName().toString(),Files.readString(p));
        }
        if(engine.equals("sqlite") || "1".equals(System.getenv("BENCH_DB_INITIALIZE"))) {
            try(var c=open()) {
                if(!engine.equals("sqlite")) try(var q=c.createStatement();var r=q.executeQuery("SELECT COUNT(*) FROM information_schema.tables WHERE table_type='BASE TABLE' AND table_schema='"+(engine.equals("postgresql")?"public":"dbo")+"'")) {r.next();if(r.getLong(1)!=0)throw new IllegalStateException("Only empty owned database may initialize");}
                // All three selected engines support transactional schema creation.
                String schema=Files.readString(this.root.resolve("schema").resolve(engine+".sql")).replaceAll("(?m)^--.*$", "");
                begin(c);try{try(var s=c.createStatement()){for(String statement:schema.split(";"))if(!statement.isBlank())s.execute(statement);}
                    importData(c,data);verifyFixtures();assertCanonical(stateDigest(c));c.commit();
                }catch(Exception e){c.rollback();throw e;}finally{c.setAutoCommit(true);}
            }
        }
        try(var c=open()){assertCanonical(stateDigest(c));}
        if(access.equals("orm")) {
        String dialect=switch(engine){case "sqlite"->"org.hibernate.community.dialect.SQLiteDialect";case "postgresql"->"org.hibernate.dialect.PostgreSQLDialect";case "sqlserver"->"org.hibernate.dialect.SQLServerDialect";default->throw new IllegalArgumentException();};
        var registry=new StandardServiceRegistryBuilder()
            .applySetting("hibernate.dialect",dialect).applySetting("hibernate.boot.allow_jdbc_metadata_access","false")
            .applySetting("hibernate.hbm2ddl.auto","none").applySetting("hibernate.show_sql","false")
            .applySetting("hibernate.cache.use_second_level_cache","false").applySetting("hibernate.cache.use_query_cache","false")
            .applySetting("hibernate.jdbc.batch_size","0").applySetting("hibernate.connection.handling_mode","DELAYED_ACQUISITION_AND_HOLD")
            .build();
        factory=new MetadataSources(registry).addAnnotatedClass(Customer.class).addAnnotatedClass(Product.class).addAnnotatedClass(Order.class).addAnnotatedClass(Line.class).buildMetadata().buildSessionFactory();
        }else factory=null;
        var prewarmed=new ArrayList<Connection>();try{for(int i=0;i<(engine.equals("sqlite")?1:16);i++)prewarmed.add(open());}finally{for(var c:prewarmed)c.close();}
        }catch(Exception error){try{close();}catch(Exception cleanup){error.addSuppressed(cleanup);}throw error;}
    }
    private static void guard(String engine,String url) {
        String owned=System.getenv("BENCH_DB_OWNED_NAME");
        if(owned==null||!owned.matches("bench_[a-f0-9]{32}")||url==null)throw new IllegalArgumentException("Owned network target required");
        String host,db;
        if(engine.equals("postgresql")){var uri=URI.create(url.substring(5));host=uri.getHost();db=uri.getPath().substring(1);}
        else if(engine.equals("sqlserver")){
            var match=Pattern.compile("^jdbc:sqlserver://([^:;]+)(?::[0-9]+)?;(.*)$",Pattern.CASE_INSENSITIVE).matcher(url);
            if(!match.matches())throw new IllegalArgumentException("Explicit SQLServer URL required");host=match.group(1);
            var name=Pattern.compile("(?:^|;)databaseName=([^;]+)(?:;|$)",Pattern.CASE_INSENSITIVE).matcher(match.group(2));
            if(!name.find())throw new IllegalArgumentException("Explicit DB required");db=name.group(1);
        }else throw new IllegalArgumentException("Unknown engine");
        var hosts=Arrays.asList(Optional.ofNullable(System.getenv("BENCH_DB_ALLOWED_HOSTS")).orElse("").split(","));
        if(!owned.equals(db)||hosts.stream().noneMatch(h->h.equalsIgnoreCase(host)))throw new IllegalArgumentException("Target not allowlisted and owned");
        verifyLease(engine,host,owned);
    }
    private static void verifyLease(String engine,String host,String database){
        String token=System.getenv("BENCH_DB_OWNER_TOKEN"),file=System.getenv("BENCH_DB_OWNERSHIP_FILE");
        if(token==null||!token.matches("[a-f0-9]{32}")||file==null||!Path.of(file).isAbsolute())throw new IllegalArgumentException("Controller ownership lease required");
        try{var lease=new ObjectMapper().readTree(Files.readString(Path.of(file)));boolean hostFound=false,databaseFound=false;
            for(var h:lease.path("allowed_hosts"))if(host.equalsIgnoreCase(h.asText()))hostFound=true;
            for(var d:lease.path("databases"))if(database.equals(d.asText()))databaseFound=true;
            if(!lease.path("status").asText().equals("active")||!lease.path("owner_token").asText().equals(token)||!lease.path("engine").asText().equals(engine)||!hostFound||!databaseFound)throw new IllegalArgumentException("Controller ownership lease does not match target");
        }catch(IOException e){throw new IllegalArgumentException("Controller ownership lease unreadable",e);}
    }
    private Connection open() throws SQLException {var c=pool.getConnection();configure(c);return c;}
    private void configure(Connection c) throws SQLException {if(engine.equals("sqlite"))try(var s=c.createStatement()){s.execute("PRAGMA foreign_keys=ON");s.execute("PRAGMA read_uncommitted=0");s.execute("PRAGMA busy_timeout=5000");}}
    private void begin(Connection c)throws SQLException{if(!engine.equals("sqlite"))c.setTransactionIsolation(Connection.TRANSACTION_READ_COMMITTED);c.setAutoCommit(false);}
    private PreparedStatement statement(Connection c,String file,Map<String,Object> p)throws SQLException{
        var names=new ArrayList<String>();var matcher=PARAM.matcher(queries.getOrDefault(file,file));var source=new StringBuilder();
        while(matcher.find()){names.add(matcher.group(1));matcher.appendReplacement(source,"?");}matcher.appendTail(source);
        var s=c.prepareStatement(source.toString());s.setQueryTimeout(15);
        for(int i=0;i<names.size();i++){String key=names.get(i);Object value=p.get(key);if(value==null)throw new IllegalArgumentException("Missing parameter "+key);if(value instanceof String str)s.setString(i+1,str);else s.setLong(i+1,((Number)value).longValue());}
        return s;
    }
    private List<Map<String,Object>> rows(Connection c,String file,Map<String,Object>p)throws SQLException{
        try(var s=statement(c,file,p);var r=s.executeQuery()){var result=new ArrayList<Map<String,Object>>();int n=r.getMetaData().getColumnCount();while(r.next()){var row=new LinkedHashMap<String,Object>();for(int i=1;i<=n;i++)row.put(r.getMetaData().getColumnLabel(i),r.getObject(i));result.add(row);}return result;}
    }
    private int change(Connection c,String file,Map<String,Object>p)throws SQLException{try(var s=statement(c,file,p)){return s.executeUpdate();}}
    public static Map<String,Object> map(Object...pairs){var r=new LinkedHashMap<String,Object>();for(int i=0;i<pairs.length;i+=2)r.put((String)pairs[i],pairs[i+1]);return r;}
    private static long n(Map<String,Object>p,String key){if(!p.containsKey(key))throw new IllegalArgumentException("Missing input "+key);return ((Number)p.get(key)).longValue();}
    private static Map<String,Object> cm(Customer c){return map("id",c.id,"email",c.email,"name",c.name,"balance_cents",c.balanceCents,"version",c.version);}
    private static Map<String,Object> om(Order o){return map("id",o.id,"customer_id",o.customerId,"status",o.status,"created_epoch_ms",o.createdEpochMs,"total_cents",o.totalCents,"version",o.version);}
    private static Map<String,Object> lm(Line l){return map("id",l.id,"order_id",l.orderId,"line_no",l.lineNo,"product_id",l.productId,"quantity",l.quantity,"unit_price_cents",l.unitPriceCents);}
    private static Map<String,Object> graph(Order o){if(o.lines.size()!=5)throw new IllegalStateException("Expected five lines");var r=om(o);r.put("customer",cm(o.customer));r.put("lines",o.lines.stream().sorted(Comparator.comparingLong(l->l.lineNo)).map(DatabaseSession::lm).toList());return r;}
    private static Map<String,Object> rawGraph(List<Map<String,Object>> rows){if(rows.size()!=5)throw new IllegalStateException("Expected five-line graph");var x=rows.getFirst();return map("id",x.get("order_id"),"customer_id",x.get("customer_id"),"status",x.get("status"),"created_epoch_ms",x.get("created_epoch_ms"),"total_cents",x.get("total_cents"),"version",x.get("order_version"),"customer",map("id",x.get("customer_record_id"),"email",x.get("email"),"name",x.get("customer_name"),"balance_cents",x.get("balance_cents"),"version",x.get("customer_version")),"lines",rows.stream().map(l->map("id",l.get("line_id"),"order_id",l.get("line_order_id"),"line_no",l.get("line_no"),"product_id",l.get("product_id"),"quantity",l.get("quantity"),"unit_price_cents",l.get("unit_price_cents"))).toList());}
    @SuppressWarnings("unchecked")
    private Order newOrder(Map<String,Object>p){
        var o=new Order();o.id=n(p,"order_id");o.customerId=n(p,"customer_id");o.status=n(p,"status");o.createdEpochMs=n(p,"created_epoch_ms");o.totalCents=n(p,"total_cents");o.version=0;
        var lines=(List<Map<String,Object>>)p.get("lines");if(lines==null||lines.size()!=5)throw new IllegalArgumentException("Exactly five lines required");long total=0;
        for(var l:lines){var x=new Line();x.id=n(l,"line_id");x.orderId=o.id;x.lineNo=n(l,"line_no");x.productId=n(l,"product_id");x.quantity=n(l,"quantity");x.unitPriceCents=n(l,"unit_price_cents");x.order=o;if(x.id<=0||x.productId<=0||x.quantity<=0||x.unitPriceCents<0)throw new IllegalArgumentException("Invalid line");total=Math.addExact(total,Math.multiplyExact(x.quantity,x.unitPriceCents));o.lines.add(x);}
        if(o.id<=0||o.customerId<=0||o.status<0||o.status>3||o.createdEpochMs<0||o.totalCents!=total||!o.lines.stream().map(l->l.lineNo).sorted().toList().equals(List.of(1L,2L,3L,4L,5L)))throw new IllegalArgumentException("Invalid order/total");return o;
    }
    public Object execute(String id,Map<String,Object>p)throws Exception{
        if(requiresReset)throw new IllegalStateException("A failed reset requires canonical recovery before operations resume");
        if(id.equals("D08")){
            @SuppressWarnings("unchecked") var ops=(List<Map<String,Object>>)p.get("trace");
            var mix=Map.of("D01",60,"D02",15,"D05",10,"D06",10,"D07",5);
            if(ops==null||ops.isEmpty()||ops.size()%20!=0||mix.entrySet().stream().anyMatch(e->ops.stream().filter(o->e.getKey().equals(o.get("case_id"))).count()*100!=ops.size()*e.getValue()))throw new IllegalArgumentException("D08 proportions differ");
            var result=new ArrayList<Object>();for(var op:ops){@SuppressWarnings("unchecked")var input=(Map<String,Object>)op.get("input");try{result.add(execute((String)op.get("case_id"),input));}catch(Exception e){result.add(map("status","error","category",failureCategory(e)));}}return result;
        }
        try(var c=open()){return access.equals("orm")?orm(c,id,p):raw(c,id,p);}
    }
    @SuppressWarnings("unchecked")
    public Object audit(String workload,List<Map<String,Object>> trace)throws Exception{
        if(trace==null||trace.isEmpty()||trace.size()>1000)throw new IllegalArgumentException("AUDIT requires1..1000 serial operations");
        var cases=new ArrayList<String>();var inputs=new ArrayList<Map<String,Object>>();
        for(var op:trace){String id=(String)op.getOrDefault("case_id",workload);if(!List.of("D01","D02","D03","D04","D05","D06","D07","H06").contains(id))throw new IllegalArgumentException("AUDIT requires individual database operations");cases.add(id);inputs.add((Map<String,Object>)op.getOrDefault("input",op));}
        var results=new ArrayList<Object>();for(int i=0;i<cases.size();i++){String id=cases.get(i);results.add(map("case_id",id,"result",id.equals("H06")?checkout(inputs.get(i)):execute(id,inputs.get(i))));}
        return map("operations",trace.size(),"results",results);
    }
    public List<Long> committedOrders(List<Long> ids)throws Exception{
        var keys=new ArrayList<>(new TreeSet<>(ids));if(keys.stream().anyMatch(id->id==null||id<=0))throw new IllegalArgumentException("Positive order IDs required");
        var found=new TreeSet<Long>();try(var c=open()){
            for(int start=0;start<keys.size();start+=500){var chunk=keys.subList(start,Math.min(start+500,keys.size()));try(var statement=c.prepareStatement("SELECT id FROM orders WHERE id IN ("+String.join(",",Collections.nCopies(chunk.size(),"?"))+")")){
                statement.setQueryTimeout(15);for(int i=0;i<chunk.size();i++)statement.setLong(i+1,chunk.get(i));try(var rows=statement.executeQuery()){while(rows.next())found.add(rows.getLong(1));}
            }}
        }return new ArrayList<>(found);
    }
    public Object batch(String workload,List<Map<String,Object>> trace,int iterations)throws Exception{return batch(workload,trace,iterations,1);}
    @SuppressWarnings("unchecked")
    public Object batch(String workload,List<Map<String,Object>> trace,int iterations,int workers)throws Exception{
        if(trace==null||trace.isEmpty()||iterations<=0)throw new IllegalArgumentException("Nonempty trace required");
        if(!List.of(1,8,32).contains(workers)||(engine.equals("sqlite")&&workers!=1))throw new IllegalArgumentException("Network workers must be1/8/32; SQLite one worker");
        if(iterations!=1&&trace.stream().anyMatch(op->List.of("D05","D06","D07","D08","H06").contains((String)op.getOrDefault("case_id",workload))))throw new IllegalArgumentException("Mutation trace cannot wrap");
        if(workload.equals("D08")){var mix=Map.of("D01",60,"D02",15,"D05",10,"D06",10,"D07",5);if(trace.size()%20!=0||mix.entrySet().stream().anyMatch(e->trace.stream().filter(op->e.getKey().equals(op.get("case_id"))).count()*100!=trace.size()*e.getValue()))throw new IllegalArgumentException("D08 batch proportions differ");}
        var owners=new HashMap<Long,Integer>();var inserts=new HashSet<Long>();var deletes=new HashSet<Long>();int[] partitions=new int[trace.size()];
        for(int i=0;i<trace.size();i++){var op=trace.get(i);String id=(String)op.getOrDefault("case_id",workload);var input=(Map<String,Object>)op.getOrDefault("input",op);int owner=((Number)op.getOrDefault("worker_partition",i)).intValue()%workers;partitions[i]=owner;if(owner<0)throw new IllegalArgumentException("Negative worker partition");if(List.of("D05","H06").contains(id)){long customer=n(input,"customer_id");if(owners.containsKey(customer)&&owners.get(customer)!=owner)throw new IllegalArgumentException("Write trace crosses customer ownership");owners.put(customer,owner);}if(List.of("D06","H06").contains(id)&&!inserts.add(n(input,"order_id")))throw new IllegalArgumentException("Duplicate insert trace");if(id.equals("D07")&&!deletes.add(n(input,"order_id")))throw new IllegalArgumentException("Duplicate delete trace");}
        long[] latencies=new long[Math.multiplyExact(trace.size(),iterations)];var failures=new java.util.concurrent.ConcurrentHashMap<String,Long>();var successes=new java.util.concurrent.atomic.AtomicLong();
        var jobs=new ArrayList<List<Integer>>();for(int w=0;w<workers;w++){var indexes=new ArrayList<Integer>();for(int i=0;i<trace.size();i++)if(partitions[i]==w)indexes.add(i);jobs.add(indexes);}
        var ready=new java.util.concurrent.CountDownLatch(workers);var startGate=new java.util.concurrent.CountDownLatch(1);var executor=java.util.concurrent.Executors.newFixedThreadPool(workers);var futures=new ArrayList<java.util.concurrent.Future<?>>();
        try{for(int w=0;w<workers;w++){final int worker=w;futures.add(executor.submit(()->{ready.countDown();try{startGate.await();}catch(InterruptedException e){Thread.currentThread().interrupt();return;}for(int repeat=0;repeat<iterations;repeat++)for(int index:jobs.get(worker)){var op=trace.get(index);long start=System.nanoTime();try{String id=(String)op.getOrDefault("case_id",workload);var input=(Map<String,Object>)op.getOrDefault("input",op);blackhole=id.equals("H06")?checkout(input):execute(id,input);successes.incrementAndGet();}catch(Exception e){failures.merge(failureCategory(e),1L,Long::sum);}finally{latencies[repeat*trace.size()+index]=System.nanoTime()-start;}}}));}ready.await();long wall=System.nanoTime();startGate.countDown();for(var future:futures)future.get();return map("attempts",latencies.length,"successes",successes.get(),"categorized_failures",failures,"latencies_ns",latencies,"wall_seconds",(System.nanoTime()-wall)/1e9,"workers",workers,"last_result",blackhole,"timing_boundary","fixed platform workers; connection lease through context/transaction/result consumption/disposal; failures included","admitted",false);}finally{executor.shutdownNow();}
    }
    private static String failureCategory(Exception e){if(e instanceof jakarta.persistence.OptimisticLockException)return "conflict";if(e instanceof SQLTimeoutException)return "timeout";if(e instanceof org.hibernate.exception.ConstraintViolationException)return "constraint";if(e instanceof SQLException sql){if("40P01".equals(sql.getSQLState())||sql.getErrorCode()==1205)return "deadlock";if("40001".equals(sql.getSQLState()))return "conflict";if(sql.getErrorCode()==5)return "busy";if(sql.getErrorCode()==6)return "locked";if(sql.getErrorCode()==19||List.of(2627,2601,547).contains(sql.getErrorCode())||("23505".equals(sql.getSQLState())||"23503".equals(sql.getSQLState())||"23514".equals(sql.getSQLState())))return "constraint";}if(e.getCause() instanceof Exception cause)return failureCategory(cause);return e.getClass().getSimpleName();}
    public Object reset()throws Exception{return reset(false);}
    public Object reset(boolean injectFailure)throws Exception{
        if(!engine.equals("sqlite")&&!ownsInitializedNetwork)throw new IllegalStateException("Reset requires this session initialized the empty allowlisted controller-owned database");
        if(!engine.equals("sqlite"))guard(engine,System.getenv("BENCH_DB_CONNECTION"));
        requiresReset=true;verifyFixtures();
        try(var c=open()){begin(c);try{for(String table:List.of("order_line","orders","product","customer"))try(var s=c.createStatement()){s.executeUpdate("DELETE FROM "+table);}
            if(injectFailure)throw new IllegalStateException("Injected reset rollback after delete");
            importData(c,dataDirectory);verifyFixtures();var restored=stateDigest(c);assertCanonical(restored);c.commit();requiresReset=false;return restored;
        }catch(Exception e){c.rollback();throw e;}finally{c.setAutoCommit(true);}}
    }
    private Object raw(Connection c,String id,Map<String,Object>p)throws Exception{
        if(id.equals("D01"))return rows(c,"D01.sql",p).getFirst();if(id.equals("D02"))return rows(c,"D02.sql",p);if(id.equals("D03"))return rawGraph(rows(c,"D03.sql",p));if(id.equals("D04"))return rows(c,"D04.sql",p);
        begin(c);try{Object result;
            switch(id){
                case "D05"->{var row=rows(c,"D05-read.sql",p).getFirst();long old=n(row,"version");var u=new HashMap<>(p);u.put("new_balance_cents",Math.addExact(n(row,"balance_cents"),n(p,"delta_cents")));u.put("old_version",p.getOrDefault("expected_version",old));u.put("new_version",Math.addExact(old,1));if(change(c,"D05-update.sql",u)!=1)throw new jakarta.persistence.OptimisticLockException();row.put("balance_cents",u.get("new_balance_cents"));row.put("version",u.get("new_version"));result=row;}
                case "D06"->result=insertRaw(c,p);
                case "D07"->{var graph=rows(c,"D07-load.sql",p);if(graph.size()!=5)throw new IllegalStateException();for(var line:graph)if(change(c,"D07-child.sql",map("line_id",line.get("line_id"),"order_id",line.get("order_id")))!=1)throw new jakarta.persistence.OptimisticLockException();var d=new HashMap<>(p);d.put("old_version",p.getOrDefault("expected_version",graph.getFirst().get("order_version")));if(change(c,"D07-parent.sql",d)!=1)throw new jakarta.persistence.OptimisticLockException();result=map("order_id",n(p,"order_id"),"deleted_lines",5L,"deleted",true);}
                default->throw new IllegalArgumentException("Unknown database case");
            }
            if(Boolean.TRUE.equals(p.get("inject_failure")))throw new IllegalStateException("Injected rollback");c.commit();return result;
        }catch(Exception e){c.rollback();throw e;}finally{c.setAutoCommit(true);}
    }
    private Object insertRaw(Connection c,Map<String,Object>p)throws Exception{
        var o=newOrder(p);var f=om(o);f.put("order_id",o.id);if(change(c,"D06-order.sql",f)!=1)throw new IllegalStateException();
        var first=lm(o.lines.getFirst());first.put("line_id",o.lines.getFirst().id);
        var names=new ArrayList<String>();var matcher=PARAM.matcher(queries.get("D06-line.sql"));while(matcher.find())names.add(matcher.group(1));
        try(var lineStatement=statement(c,"D06-line.sql",first)){
            for(var l:o.lines){var values=lm(l);values.put("line_id",l.id);for(int i=0;i<names.size();i++)lineStatement.setLong(i+1,((Number)values.get(names.get(i))).longValue());if(lineStatement.executeUpdate()!=1)throw new IllegalStateException();}
        }
        var r=om(o);r.put("lines",o.lines.stream().sorted(Comparator.comparingLong(l->l.lineNo)).map(DatabaseSession::lm).toList());return r;
    }
    private Object orm(Connection c,String id,Map<String,Object>p)throws Exception{
        try(Session s=factory.withOptions().connection(c).openSession()){
            if(id.equals("D01"))return cm(s.createQuery("from BenchCustomer where id=:id",Customer.class).setParameter("id",n(p,"customer_id")).getSingleResult());
            if(id.equals("D02"))return s.createQuery("from BenchOrder where status=:status and (createdEpochMs>:epoch or (createdEpochMs=:epoch and id>:id)) order by createdEpochMs,id",Order.class).setParameter("status",n(p,"status")).setParameter("epoch",n(p,"cursor_created_epoch_ms")).setParameter("id",n(p,"cursor_id")).setMaxResults(50).getResultList().stream().map(DatabaseSession::om).toList();
            if(id.equals("D03"))return graph(s.createQuery("select o from BenchOrder o join fetch o.customer join fetch o.lines where o.id=:id",Order.class).setParameter("id",n(p,"order_id")).getSingleResult());
            if(id.equals("D04"))return s.createQuery("select status,count(id),sum(totalCents) from BenchOrder where createdEpochMs>=:start and createdEpochMs<:end group by status order by status",Object[].class).setParameter("start",n(p,"start_epoch_ms")).setParameter("end",n(p,"end_epoch_ms")).getResultList().stream().map(r->map("status",r[0],"order_count",r[1],"total_cents",r[2])).toList();
            if(!engine.equals("sqlite"))c.setTransactionIsolation(Connection.TRANSACTION_READ_COMMITTED);
            var tx=s.beginTransaction();try{Object result;
                switch(id){
                    case "D05"->{var customer=s.createQuery("from BenchCustomer where id=:id",Customer.class).setParameter("id",n(p,"customer_id")).getSingleResult();if(p.containsKey("expected_version")&&n(p,"expected_version")!=customer.version)throw new jakarta.persistence.OptimisticLockException();customer.balanceCents=Math.addExact(customer.balanceCents,n(p,"delta_cents"));s.flush();result=cm(customer);}
                    case "D06"->{var o=newOrder(p);s.persist(o);for(var l:o.lines)s.persist(l);s.flush();var r=om(o);r.put("lines",o.lines.stream().map(DatabaseSession::lm).toList());result=r;}
                    case "D07"->{var o=s.createQuery("select o from BenchOrder o join fetch o.customer join fetch o.lines where o.id=:id",Order.class).setParameter("id",n(p,"order_id")).getSingleResult();if(o.lines.size()!=5)throw new IllegalStateException();if(p.containsKey("expected_version")&&n(p,"expected_version")!=o.version)throw new jakarta.persistence.OptimisticLockException();for(var l:o.lines)s.remove(l);s.remove(o);s.flush();result=map("order_id",o.id,"deleted_lines",5L,"deleted",true);}
                    default->throw new IllegalArgumentException("Unknown database case");
                }
                if(Boolean.TRUE.equals(p.get("inject_failure")))throw new IllegalStateException("Injected rollback");tx.commit();return result;
            }catch(Exception e){tx.rollback();throw e;}
        }
    }
    public Object checkout(Map<String,Object>p)throws Exception{
        if(requiresReset)throw new IllegalStateException("A failed reset requires canonical recovery before operations resume");
        try(var c=open()){
            if(access.equals("orm"))try(Session s=factory.withOptions().connection(c).openSession()){
                if(!engine.equals("sqlite"))c.setTransactionIsolation(Connection.TRANSACTION_READ_COMMITTED);var tx=s.beginTransaction();try{var o=newOrder(p);var customer=s.createQuery("from BenchCustomer where id=:id",Customer.class).setParameter("id",o.customerId).getSingleResult();for(var l:o.lines){var product=s.createQuery("from BenchProduct where id=:id",Product.class).setParameter("id",l.productId).getSingleResult();if(l.unitPriceCents!=product.priceCents)throw new IllegalArgumentException("Price mismatch");}customer.balanceCents=Math.subtractExact(customer.balanceCents,o.totalCents);s.persist(o);for(var l:o.lines)s.persist(l);s.flush();if(Boolean.TRUE.equals(p.get("inject_failure")))throw new IllegalStateException("Injected rollback");tx.commit();return map("id",o.id,"total_cents",o.totalCents,"version",customer.version);}catch(Exception e){tx.rollback();throw e;}}
            begin(c);try{var o=newOrder(p);var customer=rows(c,"D01.sql",map("customer_id",o.customerId)).getFirst();for(var l:o.lines){var product=rows(c,"H06-product.sql",map("product_id",l.productId)).getFirst();if(l.unitPriceCents!=n(product,"price_cents"))throw new IllegalArgumentException("Price mismatch");}insertRaw(c,p);long version=Math.addExact(n(customer,"version"),1);if(change(c,"D05-update.sql",map("customer_id",o.customerId,"old_version",customer.get("version"),"new_version",version,"new_balance_cents",Math.subtractExact(n(customer,"balance_cents"),o.totalCents)))!=1)throw new jakarta.persistence.OptimisticLockException();if(Boolean.TRUE.equals(p.get("inject_failure")))throw new IllegalStateException("Injected rollback");c.commit();return map("id",o.id,"total_cents",o.totalCents,"version",version);}catch(Exception e){c.rollback();throw e;}finally{c.setAutoCommit(true);}
        }
    }
    @FunctionalInterface private interface RowAction{void accept(Map<String,Object> row)throws Exception;}
    private static void fixtureRows(Path data,String[] t,RowAction action)throws Exception{
            var columns=t[2].split(",");try(var reader=Files.newBufferedReader(data.resolve(t[0]),StandardCharsets.UTF_8);var csv=CSVFormat.RFC4180.builder().setHeader().setSkipHeaderRecord(true).get().parse(reader)){
                if(!csv.getHeaderNames().equals(List.of(columns)))throw new IllegalArgumentException("CSV header mismatch");
                long previous=0;
                for(var row:csv){if(row.size()!=columns.length)throw new IllegalArgumentException("CSV field count mismatch");var p=new LinkedHashMap<String,Object>();for(int i=0;i<columns.length;i++){String key=columns[i],value=row.get(i);if(List.of("email","name","sku").contains(key)){int max=key.equals("email")?254:key.equals("sku")?64:128;if(value.isEmpty()||value.length()>max||value.endsWith(" ")||value.chars().anyMatch(ch->ch<32||ch>126))throw new IllegalArgumentException("Noncanonical string");p.put(key,value);}else p.put(key,Long.parseLong(value));}
                    long id=n(p,"id");if(id<=previous)throw new IllegalArgumentException("Canonical CSV IDs must be positive and strictly ascending");previous=id;action.accept(p);}
            }
    }
    private static String fileHash(Path file)throws Exception{return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(file)));}
    private void verifyFixtures()throws Exception{for(var item:fixtureHashes.entrySet())if(!item.getValue().equals(fileHash(dataDirectory.resolve(item.getKey()))))throw new IllegalArgumentException("Canonical CSV changed during database session");}
    private void assertCanonical(Map<String,Object> actual){if(!canonicalState.equals(actual))throw new IllegalStateException("Database state differs from canonical CSV snapshot");}
    private static void appendRow(MessageDigest digest,String table,Collection<?> fields){digest.update((table+"\t"+fields.stream().map(Object::toString).collect(Collectors.joining("\t"))+"\n").getBytes(StandardCharsets.UTF_8));}
    private static Map<String,Object> fixtureState(Path data)throws Exception{
        var digest=MessageDigest.getInstance("SHA-256");var counts=new LinkedHashMap<String,Integer>();
        for(var t:TABLES){int[] count={0};fixtureRows(data,t,row->{appendRow(digest,t[1],row.values());count[0]++;});counts.put(t[1],count[0]);}
        return map("sha256",HexFormat.of().formatHex(digest.digest()),"counts",counts);
    }
    private void importData(Connection c,Path data)throws Exception{
        for(var t:TABLES){String statement="INSERT INTO "+t[1]+" ("+t[2]+") VALUES ("+Arrays.stream(t[2].split(",")).map(x->":"+x).collect(Collectors.joining(","))+")";fixtureRows(data,t,row->change(c,statement,row));}
    }
    public Object stateDigest()throws Exception{
        try(var c=open()){return stateDigest(c);}
    }
    private static Map<String,Object> stateDigest(Connection c)throws Exception{
        var digest=MessageDigest.getInstance("SHA-256");var counts=new LinkedHashMap<String,Integer>();
        for(var t:TABLES){int count=0;try(var s=c.createStatement();var r=s.executeQuery("SELECT "+t[2]+" FROM "+t[1]+" ORDER BY id")){while(r.next()){var fields=new ArrayList<String>();for(int i=1;i<=r.getMetaData().getColumnCount();i++)fields.add(r.getString(i));appendRow(digest,t[1],fields);count++;}}counts.put(t[1],count);}
        return map("sha256",HexFormat.of().formatHex(digest.digest()),"counts",counts);
    }
    public Object metadata()throws Exception{
        var m=map("engine",engine,"access",access,"orm",org.hibernate.Version.getVersionString(),"driver",pool.getDriverClassName());
        m.put("schema_sha256",HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(root.resolve("schema").resolve(engine+".sql")))));
        var hashes=new TreeMap<String,String>();for(var q:queries.entrySet())hashes.put(q.getKey(),HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(q.getValue().getBytes(StandardCharsets.UTF_8))));m.put("query_sha256",hashes);
        m.put("pool_limit",engine.equals("sqlite")?1:16);m.put("connection_acquisition_timeout_seconds",15);m.put("command_timeout_seconds",15);m.put("orm_policy","fresh tracked Session per operation, no second-level/query cache, no lazy loads, JDBC batch_size=0");m.put("native_parity_admission",engine.equals("sqlite")?"blocked until cross-language native version/build comparison":"requires same server/image/settings evidence");
        try(var c=open()){m.put("driver_version",c.getMetaData().getDriverVersion());m.put("server_version",c.getMetaData().getDatabaseProductVersion());if(engine.equals("sqlite")){
            try(var s=c.createStatement();var r=s.executeQuery("SELECT sqlite_version(),sqlite_source_id()")){r.next();m.put("sqlite_version",r.getString(1));m.put("sqlite_source_id",r.getString(2));}
            var opts=new ArrayList<String>();try(var s=c.createStatement();var r=s.executeQuery("PRAGMA compile_options")){while(r.next())opts.add(r.getString(1));}m.put("compile_options",opts);
            var libs=new ArrayList<Object>();
            if(nativeDirectory!=null)try(var paths=Files.list(nativeDirectory)){for(var file:paths.filter(p->p.getFileName().toString().matches(".*\\.(dll|so|dylib)$")).toList())if(Files.isRegularFile(file))libs.add(map("name",file.getFileName().toString(),"sha256",HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(file)))));}
            m.put("native_libraries",libs);m.put("native_hash_status",libs.isEmpty()?"unavailable: locate Xerial extracted native library before admission":"measured");m.put("build_parity","compare with .NET metadata before admission");
        }}return m;
    }
    @Override public void close()throws Exception{
        Exception failure=null;
        try{if(factory!=null)factory.close();}catch(Exception error){failure=error;}
        try{pool.close();}catch(Exception error){if(failure==null)failure=error;else failure.addSuppressed(error);}
        try{if(anchor!=null)anchor.close();}catch(Exception error){if(failure==null)failure=error;else failure.addSuppressed(error);}
        if(failure!=null)throw failure;
    }
}
