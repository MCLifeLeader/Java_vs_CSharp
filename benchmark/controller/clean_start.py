"""Recover only this checkout's stale, birth-identified benchmark resources."""

import contextvars
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
import uuid

_CURRENT=contextvars.ContextVar('benchmark_run_lease',default=None)
_PROTECTED={'tools','suite-inputs','suite-traces','suite-data','data','run-leases','leases','resource-markers','quarantine'}


def repo_identity(root):
    value=os.path.normcase(str(Path(root).resolve()))
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def process_identity(pid):
    """Absence and permission failure differ: unknown identity is never stale."""
    if type(pid) is not int or pid<1:
        return {'pid':pid,'state':'unknown','creation_id':None}
    if os.name=='nt':
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:
            error=ctypes.get_last_error()
            return {'pid':pid,'state':'dead' if error in (87,1168) else 'unknown','creation_id':None}
        try:
            values=[wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle,*[ctypes.byref(value) for value in values]):
                return {'pid':pid,'state':'unknown','creation_id':None}
            created=(values[0].dwHighDateTime<<32)|values[0].dwLowDateTime
            exited=(values[1].dwHighDateTime<<32)|values[1].dwLowDateTime
            return {'pid':pid,'state':'dead' if exited else 'alive','creation_id':'win-filetime:'+str(created)}
        finally:
            kernel.CloseHandle(handle)
    if Path('/proc').is_dir():
        try:
            fields=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
            boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            return {'pid':pid,'state':'dead' if fields[0]=='Z' else 'alive','creation_id':'linux:'+boot+':'+fields[19]}
        except FileNotFoundError:
            return {'pid':pid,'state':'dead','creation_id':None}
        except (PermissionError,OSError,ValueError,IndexError):
            return {'pid':pid,'state':'unknown','creation_id':None}
    return {'pid':pid,'state':'unknown','creation_id':None}


def owner_state(owner):
    if not isinstance(owner,dict) or not owner.get('creation_id'):
        return 'unknown'
    actual=process_identity(owner.get('pid'))
    if actual['state']=='unknown':
        return 'unknown'
    if actual['state']=='dead' or actual['creation_id']!=owner['creation_id']:
        return 'stale'
    return 'active'


def _lease_state(lease):
    state=owner_state(lease.get('owner_process'))
    # Written only after the run has stopped creating resources, while holding
    # the checkout lock. A later command in the same interpreter can recover it.
    if state=='active' and lease.get('status') in ('closed','cleanup_pending'):
        return 'stale'
    return state


def _atomic(path,value):
    from controller.benchmarkctl import atomic_json
    atomic_json(path,value)


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _safe_temp(root,path):
    root=Path(root).resolve()
    work=root/'.work'
    absolute=Path(path)
    if not absolute.is_absolute():
        raise ValueError('Temporary resource must have an absolute path')
    resolved=absolute.resolve()
    if not resolved.is_relative_to(work) or resolved==work:
        raise ValueError('Temporary resource escapes this checkout .work directory')
    relative=resolved.relative_to(work)
    if relative.parts[0] in _PROTECTED:
        raise ValueError('Recovery never deletes tools, immutable inputs, traces or lease inventories')
    if relative.parts==('runs',):
        raise ValueError('A run lease may own only its unique run directory, never all runs')
    return resolved


def _file_identity(path):
    stat=Path(path).stat()
    return {'device':stat.st_dev,'inode':stat.st_ino,'birth_ns':getattr(stat,'st_birthtime_ns',None),'directory':Path(path).is_dir()}


class RepoLock:
    """Kernel lock is released after a crash; never unlink its shared inode."""
    def __init__(self,root):
        self.path=Path(root).resolve()/'.work/run.lock'
        self.file=None
    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.file=self.path.open('a+b')
        if self.file.seek(0,2)==0:
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as error:
            self.file.close()
            self.file=None
            raise ValueError('Another benchmark run or cleanup owns this checkout lock') from error
        return self
    def __exit__(self,*_):
        if self.file:
            self.file.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_UN)
            self.file.close()
            self.file=None


def _docker(arguments):
    completed=subprocess.run(['docker',*arguments],capture_output=True,text=True,timeout=30,shell=False,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    if completed.returncode:
        raise ValueError('Docker ownership inspection or cleanup failed')
    return completed.stdout.strip()


def _container_labels(container):
    return json.loads(_docker(['inspect','--format','{{json .Config.Labels}}',container])) or {}


def _temporary_action(root,path,identity,lease):
    target=_safe_temp(root,path)
    if not target.exists():
        return None
    if _file_identity(target)!=identity:
        raise ValueError('Owned temporary path was replaced; refusing recovery')
    return {'kind':'remove_temp','path':str(target),'identity':identity,'lease':str(lease)}


def plan_cleanup(root):
    """Read-only: no mkdir, lock creation, process signal, container or file mutation."""
    root=Path(root).resolve()
    repo=repo_identity(root)
    actions,blockers,protected,leases=[],[],[],[]
    for directory in ('run-leases','leases'):
        for path in sorted((root/'.work'/directory).glob('*.json')):
            try:
                lease=_read(path)
                if lease.get('repo_id')!=repo:
                    blockers.append({'path':str(path),'reason':'Lease lacks this checkout identity; legacy ownership needs review'})
                    continue
                state=_lease_state(lease)
                leases.append((path,lease,state))
                if state=='active':
                    protected.append({'lease':str(path),'reason':'Exact owner PID and creation identity are active'})
                elif state=='unknown':
                    blockers.append({'lease':str(path),'reason':'Cannot establish owner creation identity'})
            except (ValueError,OSError,TypeError):
                blockers.append({'path':str(path),'reason':'Ownership lease is unreadable or invalid'})
    containers={}
    known_containers=any(lease.get('container_id') or lease.get('container_name') for _,lease,_ in leases)
    warnings=[]
    if shutil.which('docker'):
        try:
            ids=_docker(['ps','--all','--filter','label=benchmark.repo='+repo,'--format','{{.ID}}']).splitlines()
            containers={container:_container_labels(container) for container in ids if container}
        except (ValueError,OSError,subprocess.TimeoutExpired):
            if known_containers:
                blockers.append({'reason':'Known owned container leases exist but Docker cannot be inspected'})
            else:
                warnings.append('Docker unavailable; no known container resource lease requires it')
    elif known_containers:
        blockers.append({'reason':'Known owned containers require Docker for safe recovery'})
    matched=set()
    for path,lease,state in leases:
        if state!='stale':
            continue
        try:
            owner=lease.get('owner_token')
            for container,labels in containers.items():
                if labels.get('benchmark.owner')!=owner:
                    continue
                if labels.get('benchmark.repo')!=repo or labels.get('benchmark.owner.pid')!=str(lease['owner_process']['pid']) or labels.get('benchmark.owner.creation')!=lease['owner_process']['creation_id']:
                    raise ValueError('Container labels disagree with ownership lease')
                matched.add(container)
                actions.append({'kind':'remove_container','container_id':container,'owner_token':owner,'owner_process':lease['owner_process'],'lease':str(path)})
            for process in lease.get('processes',[]):
                if process.get('finished'):
                    continue
                actual=process_identity(process.get('pid'))
                if actual['state']=='unknown':
                    raise ValueError('Cannot establish tracked child process identity')
                if actual['state']=='alive' and actual['creation_id']==process.get('creation_id'):
                    actions.append({'kind':'terminate_process','process':process,'lease':str(path)})
                elif actual['state']=='alive':
                    # PID reuse is harmless: never terminate the replacement.
                    warnings.append('Tracked PID was reused; replacement process is protected: '+str(process['pid']))
                elif os.name!='nt' and _group_members(process):
                    actions.append({'kind':'terminate_process','process':process,'lease':str(path)})
            for temporary in lease.get('temporary_paths',[]):
                action=_temporary_action(root,temporary['path'],temporary['identity'],path)
                if action:
                    actions.append(action)
            actions.append({'kind':'remove_lease','path':str(path)})
        except (ValueError,OSError,KeyError,TypeError) as error:
            blockers.append({'lease':str(path),'reason':str(error)})
    for container,labels in containers.items():
        if container in matched:
            continue
        owners=[state for _,lease,state in leases if lease.get('owner_token')==labels.get('benchmark.owner')]
        if 'active' not in owners:
            blockers.append({'container_id':container,'reason':'Repo-labeled container has no recoverable birth-identified lease'})
    # Legacy directories never become adopted merely by having a familiar name.
    owned={action['path'] for action in actions if action['kind']=='remove_temp'}
    tracked={temporary['path'] for _,lease,_ in leases for temporary in lease.get('temporary_paths',[])}
    for pattern in ('build-stage-*','service-deployment-*','run-init-*'):
        for path in (root/'.work').glob(pattern):
            if str(path.resolve()) not in owned|tracked:
                blockers.append({'path':str(path.resolve()),'reason':'Legacy temporary directory lacks a verified process lease; review before removal'})
    for path in (root/'.work/runs').glob('*'):
        if str(path.resolve()) not in owned|tracked:
            blockers.append({'path':str(path.resolve()),'reason':'Run scratch directory lacks its exact recorded filesystem identity'})
    return {'ready':not actions and not blockers and not protected,'status':'clean' if not actions and not blockers and not protected else 'needs_cleanup' if actions and not blockers and not protected else 'blocked',
            'repo_id':repo,'actions':actions,'blockers':blockers,'protected':protected,'warnings':warnings,'dry_run':True}


def _group_members(process):
    if os.name=='nt' or process.get('process_group')!=process.get('pid'):
        return []
    leader=process_identity(process['pid'])
    if leader['state']=='unknown':
        raise ValueError('Cannot establish process group leader identity')
    if leader['state']=='alive' and leader['creation_id']!=process['creation_id']:
        return []  # A reused PID/session is never ours.
    members=[]
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            fields=path.read_text().rsplit(')',1)[1].split()
            if int(fields[2])!=process['pid'] or int(fields[3])!=process['pid'] or fields[0]=='Z':
                continue
            identity=process_identity(int(path.parent.name))
            original=process['creation_id'].rsplit(':',1)
            current=identity['creation_id'].rsplit(':',1)
            if original[0]!=current[0] or int(current[1])<int(original[1]):
                raise ValueError('Process session identity is ambiguous')
            members.append(identity)
        except FileNotFoundError:
            continue
        except (PermissionError,TypeError,IndexError):
            raise ValueError('Cannot inspect owned process session')
    return members


def _terminate(process):
    actual=process_identity(process['pid'])
    if actual['state']=='unknown':
        raise ValueError('Child identity became ambiguous; refusing termination')
    group=_group_members(process) if os.name!='nt' else []
    if actual['state']=='dead' or actual['creation_id']!=process['creation_id']:
        if not group:
            return
    if group:
        os.killpg(process['pid'],signal.SIGTERM)
    elif os.name=='nt':
        _terminate_windows(process)
        return
    else:
        os.kill(process['pid'],signal.SIGTERM)
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        current=process_identity(process['pid'])
        if (current['state']=='dead' or current['creation_id']!=process['creation_id']) and not _group_members(process):
            return
        time.sleep(.05)
    if _group_members(process):
        os.killpg(process['pid'],signal.SIGKILL)
    elif process_identity(process['pid']).get('creation_id')==process['creation_id']:
        os.kill(process['pid'],signal.SIGKILL)


def _terminate_windows(process):
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
    kernel.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT]
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.OpenProcess(1|0x1000|0x100000,False,process['pid'])
    try:
        if not handle:
            if process_identity(process['pid'])['state']=='dead':
                return
            raise ValueError('Cannot open tracked orphan child')
        times=[wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle,*[ctypes.byref(value) for value in times]):
            raise ValueError('Cannot inspect tracked orphan handle')
        created='win-filetime:'+str((times[0].dwHighDateTime<<32)|times[0].dwLowDateTime)
        if created!=process['creation_id']:
            return
        if not kernel.TerminateProcess(handle,1) and not times[1].dwLowDateTime and not times[1].dwHighDateTime:
            raise ValueError('Cannot terminate same tracked orphan child')
        if kernel.WaitForSingleObject(handle,5000)!=0:
            raise ValueError('Tracked orphan child did not terminate')
    finally:
        if handle:
            kernel.CloseHandle(handle)


def cleanup_owned(root,*,dry_run=True,_locked=False):
    if dry_run:
        return plan_cleanup(root)
    if not _locked:
        with RepoLock(root):
            return cleanup_owned(root,dry_run=False,_locked=True)
    report=plan_cleanup(root)
    report['dry_run']=False
    report['completed_actions']=[]
    if report['blockers'] or report['protected']:
        return report
    for action in sorted(report['actions'],key=lambda action:({'terminate_process':0,'remove_container':1,'remove_temp':2,'remove_lease':3}[action['kind']],-len(action.get('path','')))):
        try:
            if action['kind']=='terminate_process':
                _terminate(action['process'])
            elif action['kind']=='remove_container':
                labels=_container_labels(action['container_id'])
                lease=_read(action['lease'])
                if labels.get('benchmark.repo')!=report['repo_id'] or labels.get('benchmark.owner')!=action['owner_token'] or labels.get('benchmark.owner.pid')!=str(action['owner_process']['pid']) or labels.get('benchmark.owner.creation')!=action['owner_process']['creation_id'] or _lease_state(lease)!='stale':
                    raise ValueError('Container ownership changed after cleanup planning')
                _docker(['rm','--force','--volumes',action['container_id']])
            elif action['kind']=='remove_temp':
                path=_safe_temp(root,action['path'])
                if path.exists() and _file_identity(path)!=action['identity']:
                    raise ValueError('Temporary path changed after cleanup planning')
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            else:
                Path(action['path']).unlink(missing_ok=True)
            report['completed_actions'].append(action)
        except (ValueError,OSError,subprocess.TimeoutExpired) as error:
            report['ready']=False
            report['status']='blocked'
            report['blockers'].append({'action':action,'reason':str(error)})
            return report
    checked=plan_cleanup(root)
    report.update(ready=checked['ready'],status=checked['status'],blockers=checked['blockers'],protected=checked['protected'])
    return report


def ensure_clean_start(root):
    report=cleanup_owned(root,dry_run=False)
    if not report['ready']:
        raise ValueError('Clean start blocked: '+json.dumps(report['blockers'] or report['protected']))
    return report


class _WindowsJob:
    def __init__(self,pid):
        self.handle=None
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        class Basic(ctypes.Structure):
            _fields_=[('UserTime',ctypes.c_longlong),('JobTime',ctypes.c_longlong),('Flags',wintypes.DWORD),
                      ('MinimumWorkingSet',ctypes.c_size_t),('MaximumWorkingSet',ctypes.c_size_t),
                      ('ActiveProcessLimit',wintypes.DWORD),('Affinity',ctypes.c_size_t),('PriorityClass',wintypes.DWORD),('SchedulingClass',wintypes.DWORD)]
        class Io(ctypes.Structure):
            _fields_=[(name,ctypes.c_ulonglong) for name in ('ReadOperations','WriteOperations','OtherOperations','ReadBytes','WriteBytes','OtherBytes')]
        class Extended(ctypes.Structure):
            _fields_=[('Basic',Basic),('Io',Io),('ProcessMemory',ctypes.c_size_t),('JobMemory',ctypes.c_size_t),('PeakProcessMemory',ctypes.c_size_t),('PeakJobMemory',ctypes.c_size_t)]
        self.kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype=wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
        self.kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        self.kernel.OpenProcess.restype=wintypes.HANDLE
        self.kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=self.kernel.OpenProcess(0x0100|1|0x1000,False,pid)
        try:
            self.handle=self.kernel.CreateJobObjectW(None,None)
            limits=Extended()
            limits.Basic.Flags=0x2000
            if not handle or not self.handle or not self.kernel.SetInformationJobObject(self.handle,9,ctypes.byref(limits),ctypes.sizeof(limits)) or not self.kernel.AssignProcessToJobObject(self.handle,handle):
                self.close()
                raise OSError(ctypes.get_last_error(),'Cannot contain the just-created benchmark child in its cleanup job')
        finally:
            if handle:
                self.kernel.CloseHandle(handle)
    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle=None


def current_run(root=None):
    lease=_CURRENT.get()
    if lease is not None and root is not None and lease.root!=Path(root).resolve():
        return None
    return lease


class RunLease:
    def __init__(self,root,kind='campaign',campaign_id=None):
        self.root=Path(root).resolve()
        self.kind,self.campaign_id=kind,campaign_id
        self.owner_token=uuid.uuid4().hex
        self.owner_process=process_identity(os.getpid())
        self.repo_id=repo_identity(self.root)
        self.path=self.root/'.work/run-leases'/(self.owner_token+'.json')
        self.directory=self.root/'.work/runs'/self.owner_token
        self.scratch=self.directory/'scratch'
        self.processes=[]
        self.temporary_paths=[]
        self.jobs={}
        self.clean_start_report=None
        self.lock=self.context_token=None
        self.status='creating'
    def _save(self):
        _atomic(self.path,{'schema':1,'kind':self.kind,'campaign_id':self.campaign_id,'repo_id':self.repo_id,
                           'owner_token':self.owner_token,'owner_process':self.owner_process,'status':self.status,
                           'processes':self.processes,'temporary_paths':self.temporary_paths})
    def __enter__(self):
        if current_run(self.root) is not None:
            raise ValueError('Nested benchmark run leases are not allowed')
        if self.owner_process['state']!='alive' or not self.owner_process['creation_id']:
            raise ValueError('Cannot establish current process creation identity for clean-start ownership')
        self.lock=RepoLock(self.root)
        self.lock.__enter__()
        try:
            self.clean_start_report=cleanup_owned(self.root,dry_run=False,_locked=True)
            if not self.clean_start_report['ready']:
                raise ValueError('Clean start blocked: '+json.dumps(self.clean_start_report['blockers'] or self.clean_start_report['protected']))
            self._save()  # Ownership intent precedes the first created resource.
            self.directory.mkdir(parents=True,exist_ok=False)
            self.temporary_paths.append({'path':str(self.directory),'identity':_file_identity(self.directory)})
            self._save()
            self.scratch.mkdir()
            self.status='active'
            self._save()
            self.context_token=_CURRENT.set(self)
            return self
        except BaseException:
            try:
                if self.path.exists():
                    self.status='cleanup_pending'
                    self._save()
            finally:
                self.lock.__exit__(None,None,None)
                self.lock=None
            raise
    def environment(self):
        return {'BENCH_SCRATCH_ROOT':str(self.scratch)}
    def track_temp(self,path):
        path=_safe_temp(self.root,path)
        if not path.exists():
            raise ValueError('Register an existing uniquely-created temporary resource')
        if any(path==Path(item['path']) or path.is_relative_to(Path(item['path'])) for item in self.temporary_paths):
            return
        self.temporary_paths.append({'path':str(path),'identity':_file_identity(path)})
        self._save()
    def track_process(self,process):
        if any(item['pid']==process.pid and not item.get('finished') for item in self.processes):
            return
        identity=process_identity(process.pid)
        if identity['state']!='alive' or not identity['creation_id']:
            if process.poll() is not None:
                return
            try:
                process.kill()
                process.wait(timeout=5)
            finally:
                raise ValueError('Cannot establish newly-created child identity')
        try:
            if os.name=='nt':
                self.jobs[process.pid]=_WindowsJob(process.pid)
                identity['containment']='Windows kill-on-close JobObject'
            else:
                group=os.getpgid(process.pid)
                if group!=process.pid:
                    raise ValueError('Tracked Linux child must use start_new_session=True')
                identity['process_group']=group
                identity['containment']='Dedicated process session/group'
            self.processes.append(identity)
            self._save()
        except BaseException:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            job=self.jobs.pop(process.pid,None)
            if job:
                job.close()
            raise
    def process_finished(self,process):
        entries=[item for item in self.processes if item['pid']==process.pid and not item.get('finished')]
        if not entries:
            return
        job=self.jobs.pop(process.pid,None)
        if job:
            job.close()
        else:
            entry=entries[-1]
            if process.poll() is None:
                _terminate(entry)
            if os.name!='nt' and entry.get('process_group')==entry['pid']:
                _terminate(entry)
        entries[-1]['finished']=True
        self._save()
    def __exit__(self,exception_type,exception,traceback):
        failures=[]
        try:
            for pid,job in list(self.jobs.items()):
                job.close()
                self.jobs.pop(pid,None)
            for process in self.processes:
                if not process.get('finished'):
                    try:
                        _terminate(process)
                        process['finished']=True
                    except (ValueError,OSError) as error:
                        failures.append(str(error))
            for temporary in sorted(self.temporary_paths,key=lambda item:len(item['path']),reverse=True):
                try:
                    path=_safe_temp(self.root,temporary['path'])
                    if not path.exists():
                        continue
                    if _file_identity(path)!=temporary['identity']:
                        raise ValueError('Owned temporary path was replaced before teardown')
                    shutil.rmtree(path) if path.is_dir() else path.unlink()
                except (ValueError,OSError) as error:
                    failures.append(str(error))
            self.status='cleanup_pending' if failures else 'closed'
            try:
                self._save()
                if not failures:
                    self.path.unlink(missing_ok=True)
            except OSError as error:
                failures.append('Cannot persist final cleanup receipt: '+str(error))
        finally:
            if self.context_token is not None:
                _CURRENT.reset(self.context_token)
                self.context_token=None
            if self.lock:
                self.lock.__exit__(exception_type,exception,traceback)
                self.lock=None
        if failures and exception is None:
            raise ValueError('Owned cleanup incomplete; next clean start must recover it: '+'; '.join(failures))
