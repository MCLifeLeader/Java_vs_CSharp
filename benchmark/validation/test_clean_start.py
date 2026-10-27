"""Ownership, crash recovery, exclusion and child containment contracts."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller import clean_start as clean
from controller.infrastructure import DatabaseEnvironment


class CleanStartTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.no_docker = patch.object(clean.shutil, 'which', return_value=None)
        self.no_docker.start()
        self.addCleanup(self.no_docker.stop)

    def lease(self, *, owner=None, status='active', paths=(), processes=(), **extra):
        owner = owner or {'pid': 2147483646, 'creation_id': 'missing-process-birth'}
        lease = {'schema': 1, 'repo_id': clean.repo_identity(self.root), 'owner_token': 'a' * 32,
                 'owner_process': owner, 'status': status, 'processes': list(processes),
                 'temporary_paths': [{'path': str(path), 'identity': clean._file_identity(path)} for path in paths], **extra}
        path = self.root / '.work/run-leases' / ('a' * 32 + '.json')
        clean._atomic(path, lease)
        return path, lease

    def test_empty_readonly_plan_has_no_filesystem_side_effect(self):
        self.assertTrue(clean.plan_cleanup(self.root)['ready'])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_actual_current_process_has_stable_creation_identity(self):
        identity = clean.process_identity(os.getpid())
        self.assertEqual(identity['state'], 'alive')
        self.assertTrue(identity['creation_id'])
        self.assertEqual(identity, clean.process_identity(os.getpid()))

    def test_pid_reuse_never_makes_replacement_our_process(self):
        owner = {'pid': 123, 'creation_id': 'old'}
        with patch.object(clean, 'process_identity', return_value={'pid': 123, 'state': 'alive', 'creation_id': 'new'}):
            self.assertEqual(clean.owner_state(owner), 'stale')
            with patch.object(clean.os, 'kill', side_effect=AssertionError('Replacement signaled')):
                clean._terminate(owner)

    def test_exact_active_owner_is_protected(self):
        self.lease(owner=clean.process_identity(os.getpid()))
        report = clean.cleanup_owned(self.root, dry_run=False)
        self.assertFalse(report['ready'])
        self.assertTrue(report['protected'])
        self.assertEqual(report['completed_actions'], [])

    def test_unknown_owner_is_not_treated_as_dead(self):
        self.lease(owner={'pid': 123, 'creation_id': 'old'})
        with patch.object(clean, 'process_identity', return_value={'pid': 123, 'state': 'unknown', 'creation_id': None}):
            self.assertTrue(clean.plan_cleanup(self.root)['blockers'])
            with self.assertRaisesRegex(ValueError, 'ambiguous'):
                clean._terminate({'pid': 123, 'creation_id': 'old'})

    def test_other_checkout_lease_is_preserved(self):
        path, lease = self.lease(repo_id='other-checkout')
        self.assertFalse(clean.cleanup_owned(self.root, dry_run=False)['ready'])
        self.assertEqual(clean._read(path), lease)

    def test_stale_owned_scratch_is_planned_then_removed(self):
        scratch = self.root / '.work/runs' / ('a' * 32)
        scratch.mkdir(parents=True)
        (scratch / 'worker.bin').write_bytes(b'leftover')
        path, _ = self.lease(paths=[scratch])
        self.assertFalse(clean.cleanup_owned(self.root, dry_run=True)['ready'])
        self.assertTrue(scratch.exists())
        self.assertTrue(path.exists())
        report = clean.cleanup_owned(self.root, dry_run=False)
        self.assertTrue(report['ready'], report)
        self.assertFalse(scratch.exists())
        self.assertFalse(path.exists())

    def test_changed_filesystem_identity_blocks_removal(self):
        scratch = self.root / '.work/owned-temp'
        scratch.mkdir(parents=True)
        path, lease = self.lease(paths=[scratch])
        lease['temporary_paths'][0]['identity']['inode'] += 1
        clean._atomic(path, lease)
        self.assertTrue(clean.cleanup_owned(self.root, dry_run=False)['blockers'])
        self.assertTrue(scratch.exists())

    def test_immutable_input_and_tool_paths_are_never_recovered(self):
        for name in ('suite-data', 'suite-inputs', 'suite-traces', 'tools', 'data', 'quarantine'):
            with self.subTest(name=name):
                path = self.root / '.work' / name
                path.mkdir(parents=True)
                self.lease(paths=[path])
                self.assertFalse(clean.cleanup_owned(self.root, dry_run=False)['ready'])
                self.assertTrue(path.exists())

    def test_legacy_prefix_alone_does_not_authorize_deletion(self):
        scratch = self.root / '.work/build-stage-unleased'
        scratch.mkdir(parents=True)
        self.assertTrue(clean.plan_cleanup(self.root)['blockers'])
        self.assertTrue(scratch.exists())

    def test_unknown_run_directory_blocks_start(self):
        scratch = self.root / '.work/runs/unleased'
        scratch.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'Clean start blocked'):
            with clean.RunLease(self.root):
                self.fail('An unleased run directory must block new work')

    def test_kernel_lock_excludes_simultaneous_same_checkout(self):
        with clean.RepoLock(self.root):
            with self.assertRaisesRegex(ValueError, 'checkout lock'):
                with clean.RepoLock(self.root):
                    self.fail('Second lock acquired')

    def test_run_lease_restores_context_and_removes_its_scratch(self):
        previous = clean.current_run()
        with clean.RunLease(self.root) as run:
            self.assertIs(clean.current_run(self.root), run)
            self.assertTrue(run.clean_start_report['ready'])
            scratch = Path(run.environment()['BENCH_SCRATCH_ROOT'])
            (scratch / 'worker-private-file').write_bytes(b'content')
        self.assertIs(clean.current_run(), previous)
        self.assertFalse(scratch.exists())
        self.assertTrue(clean.plan_cleanup(self.root)['ready'])

    def test_docker_missing_only_blocks_known_owned_containers(self):
        self.lease(container_name='bench-' + 'a' * 32)
        self.assertTrue(clean.plan_cleanup(self.root)['blockers'])

    def test_receipt_write_failure_still_releases_context_and_kernel_lock(self):
        previous = clean.current_run()
        run = clean.RunLease(self.root).__enter__()
        with patch.object(run, '_save', side_effect=PermissionError('Receipt write blocked')):
            with self.assertRaisesRegex(ValueError, 'cleanup incomplete'):
                run.__exit__(None, None, None)
        self.assertIs(clean.current_run(), previous)
        with clean.RepoLock(self.root):
            pass

    def test_incomplete_cleanup_is_recoverable_in_same_interpreter(self):
        run = clean.RunLease(self.root).__enter__()
        with patch.object(clean.shutil, 'rmtree', side_effect=PermissionError('Temporary sharing violation')):
            with self.assertRaisesRegex(ValueError, 'cleanup incomplete'):
                run.__exit__(None, None, None)
        self.assertEqual(clean._read(run.path)['status'], 'cleanup_pending')
        with clean.RunLease(self.root) as next_run:
            self.assertTrue(next_run.clean_start_report['ready'])
            self.assertTrue(next_run.clean_start_report['completed_actions'])

    def test_container_recovery_requires_all_birth_and_checkout_labels(self):
        _, lease = self.lease(container_id='123456789abc')
        labels = {'benchmark.owner': lease['owner_token'], 'benchmark.repo': clean.repo_identity(self.root),
                  'benchmark.owner.pid': str(lease['owner_process']['pid']), 'benchmark.owner.creation': lease['owner_process']['creation_id']}
        containers = ['123456789abc']
        commands = []
        def docker(arguments):
            commands.append(arguments)
            if arguments[0] == 'ps':
                return '\n'.join(containers)
            if arguments[0] == 'inspect':
                return json.dumps(labels)
            if arguments[:3] == ['rm', '--force', '--volumes']:
                containers.clear()
                return ''
            self.fail('Unexpected Docker operation: ' + repr(arguments))
        with patch.object(clean.shutil, 'which', return_value='docker'), patch.object(clean, '_docker', side_effect=docker):
            labels['benchmark.owner.creation'] = 'different-birth'
            self.assertFalse(clean.cleanup_owned(self.root, dry_run=False)['ready'])
            self.assertTrue(containers)
            labels['benchmark.owner.creation'] = lease['owner_process']['creation_id']
            self.assertTrue(clean.cleanup_owned(self.root, dry_run=False)['ready'])
        self.assertFalse(any('prune' in command for command in commands))

    def test_repo_labeled_container_without_lease_blocks(self):
        with patch.object(clean.shutil, 'which', return_value='docker'), patch.object(clean, '_docker', return_value='123'), patch.object(clean, '_container_labels', return_value={'benchmark.repo': clean.repo_identity(self.root)}):
            self.assertTrue(clean.plan_cleanup(self.root)['blockers'])

    def test_database_intent_precedes_create_and_id_precedes_start(self):
        config = {'process_limits': {'cpu_affinity': [0]}, 'infrastructure': {'postgresql': {
            'image': 'postgres@sha256:' + 'a' * 64, 'cpu_affinity': [1], 'memory_bytes': 1024 ** 3}}}
        environment = DatabaseEnvironment(self.root, config, 'postgresql')
        calls = []
        def docker(arguments, **kwargs):
            calls.append(arguments[0])
            if arguments[0] == 'create':
                receipt = clean._read(environment.lease)
                self.assertEqual(receipt['container_name'], environment.name)
                self.assertEqual(receipt['repo_id'], clean.repo_identity(self.root))
                self.assertEqual(receipt['owner_process'], clean.process_identity(os.getpid()))
                return '123456789abc'
            if arguments[0] == 'start':
                self.assertEqual(clean._read(environment.lease)['container_id'], '123456789abc')
            if arguments[0] == 'port':
                return '127.0.0.1:12345'
            if arguments[0] == 'exec':
                return 'accepting connections'
            if arguments[0] == 'inspect':
                return json.dumps({'benchmark.owner': environment.owner, 'benchmark.repo': environment.repo_id,
                                   'benchmark.owner.pid': str(environment.owner_process['pid']), 'benchmark.owner.creation': environment.owner_process['creation_id']})
            return ''
        with patch.object(environment, 'docker', side_effect=docker):
            with environment:
                self.assertIsNotNone(clean.current_run(self.root))
                self.assertTrue(environment.lease.exists())
        self.assertLess(calls.index('create'), calls.index('start'))
        self.assertFalse(environment.lease.exists())

    def test_process_finished_kills_remaining_descendant(self):
        marker = self.root / 'descendant.pid'
        child = "import time; time.sleep(120)"
        source = "import subprocess,sys,pathlib,time; p=subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); pathlib.Path(" + repr(str(marker)) + ").write_text(str(p.pid)); time.sleep(.5)"
        with clean.RunLease(self.root) as run:
            process = subprocess.Popen([sys.executable, '-c', source], start_new_session=os.name != 'nt')
            try:
                run.track_process(process)
                deadline = time.monotonic() + 10
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(marker.exists())
                identity = clean.process_identity(int(marker.read_text()))
                self.assertEqual(identity['state'], 'alive')
                process.wait(timeout=10)
                run.process_finished(process)
                deadline = time.monotonic() + 5
                while clean.owner_state(identity) == 'active' and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertNotEqual(clean.owner_state(identity), 'active')
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_crashed_run_is_recovered_on_next_enter(self):
        marker = self.root / 'ready'
        module_root = str(Path(__file__).resolve().parents[1])
        source = "import sys,pathlib,time,subprocess,os;sys.path.insert(0," + repr(module_root) + ");from controller.clean_start import RunLease; r=RunLease(" + repr(str(self.root)) + ");r.__enter__();p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'],start_new_session=os.name!='nt');r.track_process(p);pathlib.Path(" + repr(str(marker)) + ").write_text(str(p.pid));time.sleep(120)"
        process = subprocess.Popen([sys.executable, '-c', source], start_new_session=os.name != 'nt')
        try:
            deadline = time.monotonic() + 15
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(marker.exists())
            child_identity = clean.process_identity(int(marker.read_text()))
            self.assertEqual(child_identity['state'], 'alive')
            process.kill()
            process.wait(timeout=5)
            with clean.RunLease(self.root) as run:
                self.assertTrue(run.clean_start_report['ready'])
                self.assertTrue(run.clean_start_report['completed_actions'])
            deadline = time.monotonic() + 5
            while clean.owner_state(child_identity) == 'active' and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertNotEqual(clean.owner_state(child_identity), 'active')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


if __name__ == '__main__':
    unittest.main()
