"""Finalization of permitted dirty candidates, using disposable Git repositories and worktrees."""
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent_execution.context import ArtifactStore
from agent_execution.contracts import (
    Attempt, AttemptState, Claim, ContractError, ExecutionResult, Repository, ResultStatus, Usage,
)
from agent_execution.finalization import CandidateFinalizer
from agent_execution.native import NativeAdapter
from agent_execution.tests.fixtures import ARTIFACT, task
from agent_execution.verification import CommitVerifier

BRANCH = 'feat/1453-candidate'
AUTHOR = ('Nomad Finalizer', 'finalizer@example.invalid')
CLEAN_ENV = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull}


def git(path, *args, data=None):
    env = dict(os.environ, **CLEAN_ENV, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@e.invalid',
               GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@e.invalid')
    done = subprocess.run(['git', '-C', str(path), *args], input=data, capture_output=True, env=env)
    assert done.returncode == 0, done.stderr
    return done.stdout.decode().strip()


class CandidateFinalization(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.repo, self.work = root / 'repo', root / 'wt'
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'main')
        (self.repo / 'module.py').write_text('print(1)\n')
        (self.repo / 'old.txt').write_text('old\n')
        (self.repo / 'src').mkdir()
        (self.repo / 'src' / 'keep.py').write_text('x = 1\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-q', '-m', 'base')
        self.base = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'worktree', 'add', '-q', '-b', BRANCH, str(self.work))
        self.artifacts = ArtifactStore(root / 'artifacts')
        self.finalizer = CandidateFinalizer(self.artifacts, AUTHOR, commit_date='2026-01-01T00:00:00Z')

    def edit(self):
        (self.work / 'module.py').write_text('print(2)\n')
        (self.work / 'src' / 'new.py').write_text('y = 2\n')
        (self.work / 'old.txt').unlink()

    def capture(self, claim_generation=7):
        # The real native snapshot producer, so the format under test is the one collect() emits.
        changes, commit, ref = NativeAdapter._snapshot(SimpleNamespace(artifacts=self.artifacts),
                                                      SimpleNamespace(workspace=str(self.work)))
        self.assertIsNone(commit)
        return ExecutionResult(execution_id='exec-1', attempt_id='11111111-1111-4111-8111-111111111111',
                               task_id='openproject:fixture', agent_id='local', generation=claim_generation,
                               status=ResultStatus.CANDIDATE_COMPLETE, workspace=str(self.work),
                               base_commit=self.base, usage=Usage(), changes=changes, diff_ref=ref)

    def capture_unchecked(self):
        import base64, json
        names = [n for n in git(self.work, 'ls-files', '--others', '--exclude-standard').split('\n') if n]
        extra = {n: base64.b64encode((self.work / n).read_bytes()).decode() for n in names}
        ref = self.artifacts.put(json.dumps({'base_commit': self.base, 'untracked': extra, 'tracked_diff_b64':
                                             base64.b64encode(subprocess.run(
                                                 ['git', '-C', str(self.work), 'diff', '--binary', 'HEAD'],
                                                 capture_output=True).stdout).decode()}, sort_keys=True).encode())
        tracked = git(self.work, 'diff', '--name-only', 'HEAD').split('\n')
        return ExecutionResult(execution_id='exec-1', attempt_id='11111111-1111-4111-8111-111111111111',
                               task_id='openproject:fixture', agent_id='local', generation=7,
                               status=ResultStatus.CANDIDATE_COMPLETE, workspace=str(self.work),
                               base_commit=self.base, usage=Usage(),
                               changes=tuple(sorted(set(n for n in (*tracked, *names) if n))), diff_ref=ref)

    def finalize(self, result, **overrides):
        arguments = dict(terminated=lambda: True, current_generation=result.generation,
                         policy_check=lambda: True, branch=BRANCH, allowed_paths=('module.py', 'old.txt', 'src'))
        arguments.update(overrides)
        return self.finalizer.finalize(result, **arguments)

    def fingerprint(self):
        digest = hashlib.sha256()
        for path in sorted(self.work.rglob('*')):
            if '.git' in path.relative_to(self.work).parts[:1]:
                continue
            digest.update(str(path.relative_to(self.work)).encode())
            if path.is_file():
                digest.update(path.read_bytes())
        digest.update(git(self.work, 'rev-parse', 'HEAD').encode())
        digest.update(git(self.work, 'ls-files', '-s').encode())
        digest.update(git(self.work, 'status', '--porcelain', '--untracked-files=all').encode())
        return digest.hexdigest()

    def assertRefused(self, result, **overrides):
        before = self.fingerprint()
        with self.assertRaises(ContractError):
            self.finalize(result, **overrides)
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(git(self.work, 'rev-parse', 'HEAD'), self.base)

    def test_permitted_dirty_candidate_commits_exact_snapshot(self):
        self.edit()
        result = self.capture()
        done = self.finalize(result)
        self.assertIsNone(done.diff_ref)
        self.assertEqual(done.result_commit, git(self.work, 'rev-parse', 'HEAD'))
        self.assertEqual(git(self.work, 'rev-list', '--count', f'{self.base}..HEAD'), '1')
        self.assertEqual(git(self.work, 'rev-parse', 'HEAD^'), self.base)
        self.assertEqual(git(self.work, 'status', '--porcelain', '--untracked-files=all'), '')
        changed = set(git(self.work, 'diff', '--name-only', self.base, 'HEAD').split())
        self.assertEqual(changed, set(result.changes))
        self.assertEqual(git(self.work, 'show', 'HEAD:src/new.py'), 'y = 2')
        self.assertEqual(git(self.work, 'log', '-1', '--format=%an <%ae>|%cn <%ce>'),
                         'Nomad Finalizer <finalizer@example.invalid>|Nomad Finalizer <finalizer@example.invalid>')
        self.assertIn(f'Execution-Attempt: {result.attempt_id}', git(self.work, 'log', '-1', '--format=%B'))

    def test_changed_snapshot_or_additional_edit_refused_without_modification(self):
        for mutate in (lambda: (self.work / 'module.py').write_text('print(3)\n'),
                       lambda: (self.work / 'src' / 'extra.py').write_text('z = 3\n'),
                       lambda: (self.work / 'src' / 'new.py').write_text('y = 99\n'),
                       lambda: (self.work / 'src' / 'new.py').unlink()):
            self.edit()
            result = self.capture()
            mutate()
            self.assertRefused(result)
            git(self.work, 'checkout', '-q', '--', '.')
            git(self.work, 'clean', '-fdq')

    def test_protected_or_unapproved_path_refused(self):
        (self.work / 'module.py').write_text('print(2)\n')
        (self.work / 'config.pem').write_text('secret\n')
        # Collection now refuses PEM names too; preserve the independent finalizer check.
        with self.assertRaisesRegex(ContractError, 'protected_candidate_path'):
            self.capture()
        self.assertRefused(self.capture_unchecked(), allowed_paths=('module.py', 'config.pem'))
        (self.work / 'config.pem').unlink()
        # The native collector already refuses protected names, so forge a snapshot that lacks that check.
        (self.work / 'CLAUDE.md').write_text('rules\n')
        forged = self.capture_unchecked()
        self.assertRefused(forged, allowed_paths=('module.py', 'CLAUDE.md'))
        (self.work / 'CLAUDE.md').unlink()
        (self.work / 'src' / 'other.py').write_text('o = 1\n')
        self.assertRefused(self.capture(), allowed_paths=('module.py',))
        self.assertRefused(self.capture(), allowed_paths=())

    def test_shared_policy_and_snapshot_refuse_before_content_collection(self):
        import json
        from agent_execution import native
        from agent_execution.finalization import _protected
        from agent_execution.tool_authority import PROTECTED, ToolAuthority, is_protected
        names = [*('src/' + name + '/file' for name in sorted(PROTECTED - {'.git'})),
                 'src/.env.local', 'src/.environment', 'src/private.pem', 'src/private.key',
                 'src/credentials.json', 'src/id_rsa.backup', 'src/id_ed25519.pub',
                 'src/id_ecdsa_sk', 'src/bad\nname.pem', 'src/' + 'x' * 200 + '.pem']
        self.assertTrue(_protected('AUTONOMY.template.md'))
        self.assertFalse(is_protected('AUTONOMY.template.md'))  # readable at launch, never committed
        authority = ToolAuthority(None, None, None, SimpleNamespace(workspace=str(self.work)))
        real_git = native._git
        for name in names:
            with self.subTest(name=name):
                self.assertTrue(is_protected(name))
                self.assertTrue(_protected(name))
                with self.assertRaises(ContractError):
                    authority._path({'file_path': name})
                path = self.work / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'NEVER COLLECT THIS CONTENT')
                def inspect(workspace, *args):
                    self.assertNotIn('--binary', args)
                    return real_git(workspace, *args)
                with patch.object(native, '_git', side_effect=inspect), \
                        patch.object(Path, 'read_bytes', side_effect=AssertionError('content read')), \
                        patch.object(self.artifacts, 'put', side_effect=AssertionError('artifact written')):
                    with self.assertRaises(ContractError) as caught:
                        self.capture()
                reason = json.loads(str(caught.exception))
                self.assertEqual(reason, {'reason': 'protected_candidate_path', 'path': name[:160]})
                self.assertNotIn('NEVER COLLECT', str(caught.exception))
                self.assertNotIn('\n', str(caught.exception))
                path.unlink()
                if path.parent != self.work / 'src':
                    path.parent.rmdir()
        self.assertTrue(is_protected('src/.git/config'))
        self.assertTrue(_protected('src/.git/config'))
        self.assertFalse(is_protected('src/ordinary.py'))
        self.assertFalse(_protected('src/ordinary.py'))
        self.assertEqual(authority._path({'file_path': 'src/ordinary.py'}),
                         str(self.work / 'src/ordinary.py'))
        self.edit()
        self.assertIsNotNone(self.capture().diff_ref)

    def test_tracked_protected_name_refused_before_diff(self):
        from agent_execution import native
        path = self.work / 'src/private.pem'
        path.write_text('original')
        git(self.work, 'add', '.')
        git(self.work, 'commit', '-q', '-m', 'fixture protected base')
        path.write_text('secret changed content')
        real_git = native._git
        def inspect(workspace, *args):
            self.assertNotIn('--binary', args)
            return real_git(workspace, *args)
        with patch.object(native, '_git', side_effect=inspect), \
                patch.object(self.artifacts, 'put', side_effect=AssertionError('artifact written')):
            with self.assertRaisesRegex(ContractError, 'protected_candidate_path'):
                self.capture()

    def test_forged_protected_snapshot_refused_before_worktree_content_reads(self):
        for name in ('src/.env.local', 'src/private.pem', 'src/private.key',
                     'src/credentials.json', 'src/.aws/token', 'src/id_rsa.backup'):
            with self.subTest(name=name):
                path = self.work / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('secret')
                forged = self.capture_unchecked()
                before = self.fingerprint()
                with patch.object(Path, 'read_bytes', side_effect=AssertionError('content read')):
                    with self.assertRaisesRegex(ContractError, 'protected or secret path'):
                        self.finalize(forged, allowed_paths=('src',))
                self.assertEqual(self.fingerprint(), before)
                path.unlink()

    def test_revoked_policy_or_stale_generation_cannot_finalize(self):
        self.edit()
        result = self.capture()
        self.assertRefused(result, policy_check=lambda: False)
        self.assertRefused(result, policy_check=lambda: 1 / 0)
        self.assertRefused(result, current_generation=8)
        self.assertRefused(result, terminated=lambda: False)
        self.assertRefused(result, branch='feat/other')
        self.assertRefused(replace(result, base_commit='b' * 40))

    def test_restart_after_commit_recovers_same_candidate_without_second_commit(self):
        self.edit()
        result = self.capture()
        first = self.finalize(result)
        restarted = CandidateFinalizer(self.artifacts, AUTHOR, commit_date='2026-01-01T00:00:00Z')
        again = restarted.finalize(result, terminated=lambda: True, current_generation=result.generation,
                                   policy_check=lambda: True, branch=BRANCH,
                                   allowed_paths=('module.py', 'old.txt', 'src'))
        self.assertEqual(again.result_commit, first.result_commit)
        self.assertEqual(git(self.work, 'rev-list', '--count', f'{self.base}..HEAD'), '1')
        # A further edit after the commit is not silently recovered.
        (self.work / 'module.py').write_text('print(5)\n')
        with self.assertRaises(ContractError):
            self.finalize(result)

    def test_finalized_commit_passes_commit_verifier_precondition(self):
        self.edit()
        result = self.finalize(self.capture())
        spec = replace(task(), repository=Repository(identity='fixture', path=str(self.repo),
                                                     base_commit=self.base))
        run = Attempt(execution_id=result.execution_id, attempt_id=result.attempt_id, attempt_number=1,
                      task_id=result.task_id, task_revision='r1', agent_id='local', state=AttemptState.VERIFYING,
                      workspace=str(self.work), base_commit=self.base, context_ref=ARTIFACT,
                      policy_revision='p1', claim=Claim(owner_id='scheduler-1', generation=7,
                                                         lease_expires_at='2026-10-20T12:00:00Z'))
        verifier = CommitVerifier(self.artifacts, Path(self.tmp.name) / 'runtime', ())
        self.assertEqual(verifier._pin(spec, run, result), result.result_commit)


if __name__ == '__main__':
    unittest.main()
