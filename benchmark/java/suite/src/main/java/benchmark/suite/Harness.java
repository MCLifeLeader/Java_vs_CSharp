package benchmark.suite;

import java.nio.file.Path;
import java.util.concurrent.TimeUnit;
import org.openjdk.jmh.annotations.*;
import org.openjdk.jmh.infra.Blackhole;
import org.openjdk.jmh.runner.Runner;
import org.openjdk.jmh.runner.options.OptionsBuilder;

/** Independent JMH corroboration; fixed-work common-controller results remain primary. */
@State(Scope.Thread)
@BenchmarkMode(Mode.Throughput)
@OutputTimeUnit(TimeUnit.SECONDS)
@Warmup(iterations=3,time=1)
@Measurement(iterations=5,time=1)
@Fork(5)
public class Harness {
    public static void main(String[]args)throws Exception{launch(args);}
    @Param({"C01","K01"})public String caseId;
    @Param({"transform"})public String parameter;
    Workloads workloads;
    @Setup(Level.Trial)public void setup()throws Exception{workloads=new Workloads(Path.of(System.getProperty("bench.data",".")),System.getProperty("bench.scale","S").equals("smoke"),Integer.getInteger("bench.workers",4),"controlled");}
    @Setup(Level.Invocation)public void prepare()throws Exception{workloads.prepare(caseId,parameter,1);}
    @Benchmark public void operation(Blackhole blackhole)throws Exception{blackhole.consume(workloads.run(caseId,parameter,false).checksum());}
    @TearDown(Level.Trial)public void teardown()throws Exception{workloads.close();}
    static void launch(String[]args)throws Exception {
        String caseId=System.getProperty("bench.case","C01"),parameter=System.getProperty("bench.parameter",caseId.equals("K01")?"scan":"transform");
        var options=new OptionsBuilder().include(Harness.class.getSimpleName()).param("caseId",caseId).param("parameter",parameter)
            .jvmArgsAppend("--enable-native-access=ALL-UNNAMED","-Dbench.data="+System.getProperty("bench.data","."),"-Dbench.scale="+System.getProperty("bench.scale","S"),"-Dbench.workers="+Integer.getInteger("bench.workers",4))
            .resultFormat(org.openjdk.jmh.results.format.ResultFormatType.JSON).result(System.getProperty("bench.jmh.output","jmh-results.json"));
        if(Boolean.getBoolean("bench.jmh.smoke"))options.forks(1).warmupIterations(0).measurementIterations(1).measurementTime(org.openjdk.jmh.runner.options.TimeValue.milliseconds(100));
        new Runner(options.build()).run();
    }
}
