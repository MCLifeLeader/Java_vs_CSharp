"""Run JMH on a regular classpath so its forked JVMs find generated harnesses."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
from bootstrap_maven import bootstrap

def main():
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
    from controller.toolchain import activate_toolchain
    activate_toolchain(Path(__file__).resolve().parents[2])
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,required=True)
    parser.add_argument('--case',default='C01')
    parser.add_argument('--parameter',default='transform')
    parser.add_argument('--workers',type=int,default=4)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--smoke',action='store_true',help='Only prove harness startup; output is not performance evidence')
    args=parser.parse_args()
    project=Path(__file__).resolve().parent
    maven=Path(bootstrap()["windows" if os.name=="nt" else "unix"])
    classpath=project/'target/classpath.txt'
    subprocess.run([str(maven),'-q','-f',str(project/'pom.xml'),'dependency:build-classpath',f'-Dmdep.outputFile={classpath}'],check=True)
    args.output.resolve().parent.mkdir(parents=True,exist_ok=True)
    command=['java','--enable-native-access=ALL-UNNAMED',f'-Dbench.data={args.data.resolve()}',f'-Dbench.case={args.case}',f'-Dbench.parameter={args.parameter}',f'-Dbench.workers={args.workers}',f'-Dbench.jmh.output={args.output.resolve()}',f'-Dbench.scale={"smoke" if args.smoke else "S"}',f'-Dbench.jmh.smoke={str(args.smoke).lower()}','-cp',os.pathsep.join([str(project/'target/classes'),classpath.read_text().strip()]),'benchmark.suite.Harness']
    subprocess.run(command,check=True)

if __name__=='__main__':main()
