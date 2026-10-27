"""Independent Java smoke protocol checks; never performance evidence."""
import argparse, json, os, queue, shutil, subprocess, sys, tempfile, threading
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument("--jar",type=Path,default=ROOT/"java/suite/target/suite.jar")
parser.add_argument("--data",type=Path,default=ROOT/".work/suite-data/smoke")
parser.add_argument("--native",type=Path,default=os.getenv("BENCH_NATIVE_LIBRARY"))
parser.add_argument("--output",type=Path,default=ROOT/".work/java-suite-validation.json")
args=parser.parse_args()
sys.path.insert(0,str(ROOT))
from controller.toolchain import activate_toolchain
activate_toolchain(ROOT)
from controller.services import LocalServices
from validation.suite_oracle import expected_suite
results=[]
with tempfile.TemporaryDirectory(prefix='java-suite-validation-') as directory:
    data=Path(directory)
    for fixture in args.data.iterdir():
        if fixture.is_file():shutil.copy2(fixture,data/fixture.name)
    shutil.copy2(args.jar,data/'suite.jar')
    environment=os.environ.copy()
    if args.native:environment['BENCH_NATIVE_LIBRARY']=str(Path(args.native).resolve())
    with LocalServices(ROOT,data):
        p=subprocess.Popen(['java','--enable-native-access=ALL-UNNAMED','-jar',str(data/'suite.jar'),'--data',str(data),'--scale','smoke','--workers','4'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',env=environment)
        diagnostics=[];diagnostic_reader=threading.Thread(target=lambda:[diagnostics.append(line) for line in p.stderr],daemon=True);diagnostic_reader.start()
        lines=queue.Queue()
        threading.Thread(target=lambda:[lines.put(line) for line in p.stdout],daemon=True).start()
        def receive():
            line=lines.get(timeout=45)
            try:return json.loads(line)
            except Exception:raise AssertionError(line)
        def command(text):
            p.stdin.write(text+'\n');p.stdin.flush();return receive()
        ready=receive();assert ready['status']=='ready',ready
        try:
            for case in json.loads((ROOT/'config/cases.json').read_text())['cases']:
                cid=case['case_id']
                if cid[0] in 'DHP' or cid=='L05':continue
                if cid.startswith('N') and not args.native:
                    results.extend({'case_id':cid,'parameter_id':parameter['parameter_id'],'skipped':True,'reason':'--native/BENCH_NATIVE_LIBRARY not supplied'} for parameter in case['parameters']);continue
                for parameter in case['parameters']:
                    pid=parameter['parameter_id'];entry={'case_id':cid,'parameter_id':pid}
                    try:
                        assert command(f'PREPARE\t{cid}\t{pid}\t1')['status']=='prepared'
                        actual=command(f'CHECK\t{cid}\t{pid}');entry['check']=actual
                        expected=expected_suite(data,cid,pid,workers=4);entry['expected']=expected
                        assert actual['status']=='ok',actual
                        differences={key:(actual.get(key),expected[key]) for key in ('checksum','semantic_hash','work_units','unit') if actual.get(key)!=expected[key]}
                        assert not differences,differences
                        assert command(f'PREPARE\t{cid}\t{pid}\t2')['status']=='prepared'
                        batch=command(f'BATCH\t{cid}\t{pid}\t2');entry['batch']=batch
                        assert batch['status']=='ok',batch
                        assert batch['checksum']==f"{int(expected['checksum'],16)*2&((1<<64)-1):016x}",(batch,expected)
                        assert batch['work_units']==expected['work_units']*2,(batch,expected)
                        entry['passed']=True
                    except Exception as error:
                        entry['passed']=False;entry['error']=str(error)
                    results.append(entry)
                    print(cid,pid,entry['passed'],entry.get('error',''),flush=True)
        finally:
            p.stdin.write('QUIT\n');p.stdin.flush()
            try:p.wait(timeout=15)
            except subprocess.TimeoutExpired:p.kill();p.wait()
            diagnostic_reader.join(timeout=5)
        output={'ready':ready,'checks':results,'stderr':''.join(diagnostics),'evidence_kind':'correctness-only smoke; no performance admission'}
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(output,indent=2),encoding='utf-8')
        failures=[row for row in results if not row.get('skipped') and not row['passed']]
        print(json.dumps({'passed':sum(row.get('passed',False) for row in results),'failed':len(failures),'skipped':sum(row.get('skipped',False) for row in results)}))
        sys.exit(bool(failures))
