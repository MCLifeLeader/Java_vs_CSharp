"""Activate already provisioned exact tools in this process; never change the machine."""

import os
from pathlib import Path
import shutil


def activate_toolchain(root):
    root=Path(root).resolve()
    if shutil.which('java') and shutil.which('javac'):
        return {'jdk':'existing process PATH','activated':False}
    home=root/'.work/tools/jdk-25.0.3+9'
    if (home/'Contents/Home').is_dir():
        home=home/'Contents/Home'
    executable='.exe' if os.name=='nt' else ''
    if not all((home/'bin'/(name+executable)).is_file() for name in ('java','javac')):
        return {'jdk':None,'activated':False,'reason':'Provision the pinned JDK using tools/bootstrap.py jdk'}
    release=(home/'release').read_text(encoding='utf-8')
    if 'IMPLEMENTOR="Microsoft"' not in release or 'JAVA_RUNTIME_VERSION="25.0.3+9-LTS"' not in release:
        raise ValueError('Local JDK release identity differs from pinned Microsoft25.0.3+9-LTS')
    os.environ['JAVA_HOME']=str(home)
    os.environ['PATH']=str(home/'bin')+os.pathsep+os.environ.get('PATH','')
    return {'jdk':str(home),'activated':True,'scope':'Current process and its children only'}
