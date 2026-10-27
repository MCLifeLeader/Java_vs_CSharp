"""Disposable, controller-owned database containers. Secrets never enter command logs."""

import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time
import uuid


class DatabaseEnvironment:
    """One engine at a time; only generated databases and labeled resources are changed."""

    def __init__(self, root, config, engine):
        if engine not in ("sqlserver", "postgresql"):
            raise ValueError("Only network engines use disposable infrastructure")
        self.root, self.config, self.engine = Path(root).resolve(), config, engine
        self.owner = uuid.uuid4().hex
        self.name = "bench-" + self.owner
        self.databases, self.logs = set(), []
        self.password = secrets.token_urlsafe(24) + "Aa1!"
        self.container = self.port = None
        self.lease = self.root / ".work" / "leases" / (self.owner + ".json")
        self.profile = config.get("infrastructure", {}).get(engine, {})
        self.run = self.owned_run = None
        self.owner_process = None
        self.repo_id = None
        self.creation_attempted = False

    def docker(self, arguments, *, stdin=None, timeout=60, allow_failure=False):
        command = ["docker", *arguments]
        process = subprocess.Popen(command, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=False,
                                   start_new_session=os.name != 'nt',
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            if self.run:
                self.run.track_process(process)
            stdout, stderr = process.communicate(stdin, timeout=timeout)
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.communicate()
            raise
        finally:
            if self.run:
                self.run.process_finished(process)
        completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        # None of these command arguments contains a secret. Never inspect/dump
        # Config.Env, container logs, env-file contents or connection strings.
        self.logs.append({"command": command, "exit_code": completed.returncode,
                          "stdout": completed.stdout, "stderr": completed.stderr})
        if completed.returncode and not allow_failure:
            raise ValueError("Disposable infrastructure command failed: " + arguments[0])
        return completed.stdout.strip()

    def __enter__(self):
        image = self.profile.get("image")
        if not isinstance(image, str) or not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image):
            raise ValueError("An exact immutable database image digest must be resolved before container startup")
        if self.engine == "sqlserver" and self.config.get("sqlserver_eula_accepted") is not True:
            raise ValueError("SQL Server requires the operator's recorded EULA acceptance before startup")
        cpus = self.profile.get("cpu_affinity")
        memory = self.profile.get("memory_bytes")
        if (not isinstance(cpus, list) or not cpus or any(type(cpu) is not int or cpu < 0 for cpu in cpus)
                or type(memory) is not int or memory < 256 * 1024 * 1024):
            raise ValueError("Database requires explicit non-overcommitted CPU affinity and memory budget")
        app_cpus = set(self.config.get("process_limits", {}).get("cpu_affinity", []))
        if not app_cpus:
            raise ValueError("An explicit application CPU allocation is required before database startup")
        if app_cpus.intersection(cpus):
            raise ValueError("Database and application CPU allocations overlap")
        from controller.clean_start import RunLease, current_run, repo_identity
        self.run = current_run(self.root)
        if self.run is None:
            self.owned_run = RunLease(self.root, kind="database-infrastructure")
            self.run = self.owned_run.__enter__()
        self.owner_process = self.run.owner_process
        self.repo_id = repo_identity(self.root)
        try:
            self._save_lease()  # Intent is durable before any container exists.
            self.docker(["pull", image], timeout=300)
        except BaseException:
            self.close()
            raise
        environment = {"MSSQL_SA_PASSWORD": self.password, "MSSQL_PID": self.profile.get("edition", "Developer"), "ACCEPT_EULA": "Y"} if self.engine == "sqlserver" else {
            "POSTGRES_PASSWORD": self.password, "POSTGRES_USER": "benchmark", "POSTGRES_DB": "postgres"}
        env_file = None
        try:
            descriptor, env_file = tempfile.mkstemp(prefix="benchmark-env-", suffix=".txt", dir=self.run.scratch)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                self.run.track_temp(env_file)
                for key, value in environment.items():
                    output.write(key + "=" + value + "\n")
            port = "1433" if self.engine == "sqlserver" else "5432"
            self.creation_attempted = True
            self.container = self.docker(["create", "--name", self.name,
                                          "--label", "benchmark.owner=" + self.owner,
                                          "--label", "benchmark.repo=" + self.repo_id,
                                          "--label", "benchmark.owner.pid=" + str(self.owner_process['pid']),
                                          "--label", "benchmark.owner.creation=" + self.owner_process['creation_id'],
                                          "--cpuset-cpus", ",".join(str(cpu) for cpu in cpus), "--memory", str(memory),
                                          "--publish", "127.0.0.1::" + port, "--env-file", env_file, image])
            self._save_lease()  # Save the created ID before the container can start.
            self.docker(["start", self.container])
            mapping = self.docker(["port", self.container, port + "/tcp"])
            self.port = int(mapping.rsplit(":", 1)[1])
            deadline = time.monotonic() + self.profile.get("readiness_timeout_seconds", 120)
            while time.monotonic() < deadline:
                if self.engine == "postgresql":
                    result = self.docker(["exec", self.container, "pg_isready", "-h", "127.0.0.1", "-U", "benchmark", "-d", "postgres"], allow_failure=True)
                    if "accepting connections" in result:
                        break
                else:
                    try:
                        self._sql("SELECT 1")
                        break
                    except ValueError:
                        pass
                time.sleep(.5)
            else:
                raise ValueError("Disposable database never became ready")
            return self
        except BaseException:
            self.close()
            raise
        finally:
            if env_file:
                Path(env_file).unlink(missing_ok=True)

    def _sql(self, sql):
        if self.engine == "postgresql":
            return self.docker(["exec", self.container, "psql", "-U", "benchmark", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c", sql])
        descriptor, secret_file = tempfile.mkstemp(prefix="benchmark-sqlcmd-", suffix=".txt", dir=self.run.scratch if self.run else self.root / ".work")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                if self.run:
                    self.run.track_temp(secret_file)
                output.write("SQLCMDPASSWORD=" + self.password + "\n")
            return self.docker(["exec", "--env-file", secret_file, self.container, "/opt/mssql-tools18/bin/sqlcmd",
                                "-S", "localhost", "-U", "sa", "-C", "-b", "-Q", sql])
        finally:
            Path(secret_file).unlink(missing_ok=True)

    def create_database(self):
        name = "bench_" + uuid.uuid4().hex
        if not re.fullmatch(r"bench_[0-9a-f]{32}", name):
            raise ValueError("Invalid owned database identity")
        sql = 'CREATE DATABASE "' + name + '"' if self.engine == "postgresql" else "CREATE DATABASE [" + name + "]"
        self._sql(sql)
        self.databases.add(name)
        self._save_lease()
        return name

    def drop_database(self, name):
        if name not in self.databases or not re.fullmatch(r"bench_[0-9a-f]{32}", name):
            raise ValueError("Refusing to reset or drop a database not created by this controller instance")
        self._sql('DROP DATABASE "' + name + '"' if self.engine == "postgresql" else "ALTER DATABASE [" + name + "] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [" + name + "]")
        self.databases.remove(name)
        self._save_lease()

    def _save_lease(self):
        from controller.benchmarkctl import atomic_json
        atomic_json(self.lease, {"owner_token": self.owner, "engine": self.engine, "container_id": self.container,
                                 "schema": 1, "repo_id": self.repo_id, "container_name": self.name,
                                 "owner_process": self.owner_process, "run_token": self.run.owner_token if self.run else None,
                                 "allowed_hosts": ["127.0.0.1"], "databases": sorted(self.databases), "status": "active"})

    def worker_environment(self, name, language):
        if name not in self.databases:
            raise ValueError("Database is not owned by this environment")
        # The baseline is an explicitly named isolated-lab TLS-off profile. It
        # must never be confused with a validated TLS production profile.
        if self.engine == "sqlserver":
            connection = (f"Server=127.0.0.1,{self.port};Database={name};User Id=sa;Password={self.password};Encrypt=False;Max Pool Size=16;Connect Timeout=15;ConnectRetryCount=0" if language == "csharp" else
                          f"jdbc:sqlserver://127.0.0.1:{self.port};databaseName={name};user=sa;password={self.password};encrypt=false;loginTimeout=15")
        else:
            connection = (f"Host=127.0.0.1;Port={self.port};Database={name};Username=benchmark;Password={self.password};SSL Mode=Disable;Maximum Pool Size=16;Timeout=15;Command Timeout=15;Max Auto Prepare=0" if language == "csharp" else
                          f"jdbc:postgresql://127.0.0.1:{self.port}/{name}?user=benchmark&password={self.password}&sslmode=disable&prepareThreshold=0&connectTimeout=15&socketTimeout=15")
        return {**os.environ, **(self.run.environment() if self.run else {}), "BENCH_DB_CONNECTION": connection, "BENCH_DB_ALLOWED_HOSTS": "127.0.0.1",
                "BENCH_DB_OWNED_NAME": name, "BENCH_DB_INITIALIZE": "1",
                "BENCH_DB_OWNERSHIP_FILE": str(self.lease.resolve()), "BENCH_DB_OWNER_TOKEN": self.owner}

    def resource_snapshot(self):
        """Common cgroup counters for the entire owned database container."""
        if not self.container:
            raise ValueError("Owned database container is not running")
        cpu = self.docker(["exec",self.container,"cat","/sys/fs/cgroup/cpu.stat"])
        memory = self.docker(["exec",self.container,"cat","/sys/fs/cgroup/memory.current"])
        fields = dict(line.split() for line in cpu.splitlines())
        return {"cpu_seconds":int(fields["usage_usec"])/1e6,"cgroup_memory_bytes":int(memory),
                "memory_boundary":"Entire database container including shared buffers, file cache and kernel charges; not process RSS",
                "collector":"Owned Linux cgroup-v2 cpu.stat/memory.current"}

    def close(self):
        error = None
        try:
            if not self.container and self.creation_attempted:
                # A CLI timeout can happen after Docker has created the object.
                # Preserve the intent lease if absence cannot be proven.
                identifiers = self.docker(["ps", "--all", "--filter", "name=^/" + self.name + "$", "--format", "{{.ID}}"])
                if identifiers:
                    if len(identifiers.splitlines()) != 1:
                        raise ValueError("Ambiguous created container identity")
                    self.container = identifiers
            if self.container:
                labels = json.loads(self.docker(["inspect", "--format", '{{json .Config.Labels}}', self.container]))
                expected = {"benchmark.owner": self.owner, "benchmark.repo": self.repo_id,
                            "benchmark.owner.pid": str(self.owner_process['pid']),
                            "benchmark.owner.creation": self.owner_process['creation_id']}
                if any(labels.get(key) != value for key, value in expected.items()):
                    raise ValueError("Refusing cleanup: container ownership labels differ")
                self.docker(["rm", "--force", "--volumes", self.container])
                self.container = None
            self.lease.unlink(missing_ok=True)
        except BaseException as failure:
            error = failure
            # Keep the last durable lease for proactive recovery on next start.
            if self.lease.exists():
                from controller.benchmarkctl import atomic_json
                receipt = json.loads(self.lease.read_text(encoding="utf-8"))
                receipt['status'] = 'cleanup_pending'
                atomic_json(self.lease, receipt)
        finally:
            if self.owned_run:
                owned, self.owned_run = self.owned_run, None
                owned.__exit__(type(error) if error else None, error, None)
                self.run = None
        if error:
            raise error

    def __exit__(self, *_):
        self.close()
