"""Real flock/descriptor transfer in disposable paths; no GPU or shared services used."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_execution.contracts import ContractError
from agent_execution.resources import GpuLease


class GpuAdmission(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / 'gpu.lock'

    def lease(self, **kwargs):
        return GpuLease(lock_path=self.path, **kwargs)

    def test_competing_connections_refused_until_owner_releases(self):
        with self.lease():
            with self.assertRaises(ContractError):
                with self.lease():
                    self.fail('admitted competing workload')
        with self.lease():
            pass

    def test_inherited_descriptor_retains_lock_after_controller_detaches(self):
        lease = self.lease().__enter__()
        self.addCleanup(lease.detach)
        child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read()'],
                                  stdin=subprocess.PIPE, pass_fds=(lease.fd,))
        self.addCleanup(lambda: child.wait(timeout=5))
        self.addCleanup(child.stdin.close)
        lease.detach()
        with self.assertRaises(ContractError):
            with self.lease():
                self.fail('controller exit released live native reservation')
        child.stdin.close()
        child.wait(timeout=5)
        with self.lease():
            pass

    def test_pending_restoration_blocks_after_lock_release(self):
        pending = self.root / 'gpu_guard_state.json'
        with self.lease():
            pending.write_text('{"restore_failed":true}')
        with self.assertRaises(ContractError):
            with self.lease(recovery_paths=(pending,)):
                self.fail('pending restore admitted incompatible work')
        pending.unlink()
        with self.lease(recovery_paths=(pending,)):
            pass

    def test_symlink_hardlink_and_world_writable_lock_refused(self):
        target = self.root / 'other'
        target.touch(mode=0o600)
        self.path.symlink_to(target)
        with self.assertRaises((ContractError, OSError)):
            with self.lease():
                pass
        self.path.unlink()
        os.link(target, self.path)
        with self.assertRaises(ContractError):
            with self.lease():
                pass
        self.path.unlink()
        self.path.touch(mode=0o600)
        self.path.chmod(0o666)
        with self.assertRaises(ContractError):
            with self.lease():
                pass


if __name__ == '__main__':
    unittest.main()
