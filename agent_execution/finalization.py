"""Finalize a permitted dirty candidate into exactly one commit in its own feature worktree.

The native adapter captures a dirty candidate as a content-addressed snapshot artifact (tracked
binary diff plus untracked file content) and the verifier only accepts committed candidates.
This module is the missing step between them. It is offline: it only runs local git commands in
the attempt worktree, never writes global git config, never pushes or merges, and never calls a
service. Every refusal happens before the first mutation, so a refused worktree is unchanged.
"""
import base64
from dataclasses import replace
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile

from .contracts import ContractError, ResultStatus
from .tool_authority import CANDIDATE_PROTECTED, is_protected

_SHA = re.compile(r'[0-9a-f]{40}')


def _run(workspace, *args, env=None, data=None, check=True):
    environment = dict(os.environ)
    environment.update(env or {})
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY'):
        if not (env and key in env):
            environment.pop(key, None)
    done = subprocess.run(['git', '-C', str(workspace), *args], input=data, capture_output=True,
                          timeout=60, env=environment)
    if check and done.returncode:
        raise ContractError('candidate finalization git command failed: ' + (args[0] if args else ''))
    return done


def _out(workspace, *args, **kwargs):
    return _run(workspace, *args, **kwargs).stdout


def _protected(path):
    return is_protected(path, extra=CANDIDATE_PROTECTED)


def _permitted(path, allowed):
    pure = PurePosixPath(path)
    if pure.is_absolute() or '..' in pure.parts or '.' in pure.parts:
        return False
    return any(path == entry or path.startswith(entry.rstrip('/') + '/') for entry in allowed)


def commit_message(result, snapshot_ref):
    """Deterministic message; the trailers are what makes recovery after a restart exact."""
    return (f'feat(execution): candidate for {result.task_id}\n\n'
            f'Execution-Attempt: {result.attempt_id}\n'
            f'Execution-Generation: {result.generation}\n'
            f'Candidate-Snapshot: {snapshot_ref.ref}\n')


class CandidateFinalizer:
    """artifacts: ArtifactStore. author: (name, email) fixed identity, used via environment only."""

    def __init__(self, artifacts, author, *, commit_date=None):
        if (type(author) is not tuple or len(author) != 2
                or not all(type(item) is str and item.strip() and '\n' not in item and '\x00' not in item
                           for item in author)):
            raise ContractError('finalization requires an explicit fixed author identity')
        self.artifacts, self.author, self.commit_date = artifacts, author, commit_date

    # -- snapshot -----------------------------------------------------------------------
    def _load(self, result):
        if result.status != ResultStatus.CANDIDATE_COMPLETE or result.result_commit is not None \
                or result.diff_ref is None or not result.changes:
            raise ContractError('finalization requires a dirty candidate with snapshot evidence')
        try:
            data = json.loads(self.artifacts.read(result.diff_ref))
            patch = base64.b64decode(data['tracked_diff_b64'], validate=True)
            untracked = {name: base64.b64decode(value, validate=True) for name, value in data['untracked'].items()}
            base = data['base_commit']
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ContractError('candidate snapshot is malformed') from exc
        if type(base) is not str or not _SHA.fullmatch(base) or base != result.base_commit:
            raise ContractError('candidate snapshot base does not match the attempt base')
        return base, patch, untracked

    # -- checks -------------------------------------------------------------------------
    def _gate(self, result, *, terminated, current_generation, policy_check, workspace, branch, head_ok):
        if not callable(terminated) or terminated() is not True:
            raise ContractError('agent termination is not proven; refusing to finalize')
        if type(current_generation) is not int or current_generation != result.generation:
            raise ContractError('stale ownership generation; refusing to finalize')
        try:
            allowed = policy_check()
        except Exception as exc:
            raise ContractError('policy re-check failed; refusing to finalize') from exc
        if allowed is not True:
            raise ContractError('policy no longer permits finalization')
        path = Path(workspace)
        if not path.is_absolute() or path.resolve() != path or not path.is_dir():
            raise ContractError('finalization requires an isolated canonical worktree')
        gitdir = _out(path, 'rev-parse', '--path-format=absolute', '--git-dir').strip()
        common = _out(path, 'rev-parse', '--path-format=absolute', '--git-common-dir').strip()
        if gitdir == common or _out(path, 'rev-parse', '--show-toplevel').decode().strip() != str(path):
            raise ContractError('candidate is not in its own linked worktree')
        current = _out(path, 'branch', '--show-current').decode().strip()
        if current != branch or not current.startswith(('feat/', 'fix/')):
            raise ContractError('worktree is not on the attempt feature branch')

    def _changed(self, workspace):
        tracked = _out(workspace, 'diff', '--name-only', '-z', 'HEAD').decode().split('\0')
        untracked = _out(workspace, 'ls-files', '--others', '--exclude-standard', '-z').decode().split('\0')
        return (tuple(sorted(set(name for name in (*tracked, *untracked) if name))),
                tuple(sorted(name for name in untracked if name)))

    def _expected_tree(self, workspace, base, patch, untracked, modes):
        with tempfile.TemporaryDirectory() as directory:
            env = {'GIT_INDEX_FILE': str(Path(directory) / 'index')}
            _run(workspace, 'read-tree', base, env=env)
            if patch:
                _run(workspace, 'apply', '--cached', '--binary', '-', env=env, data=patch)
            for name, content in untracked.items():
                blob = _out(workspace, 'hash-object', '-w', '--stdin', data=content).decode().strip()
                _run(workspace, 'update-index', '--add', '--cacheinfo', f'{modes.get(name, "100644")},{blob},{name}',
                     env=env)
            return _out(workspace, 'write-tree', env=env).decode().strip()

    def _recovered(self, workspace, result, base, patch, untracked, message):
        head = _out(workspace, 'rev-parse', 'HEAD').decode().strip()
        if _out(workspace, 'rev-list', '--parents', '-n', '1', 'HEAD').decode().split() != [head, base]:
            return None
        if _out(workspace, 'log', '-1', '--format=%B', 'HEAD').decode().strip() != message.strip():
            return None
        if _out(workspace, 'status', '--porcelain', '--untracked-files=all'):
            raise ContractError('candidate commit exists but the worktree has further changes')
        modes = {}
        for name in untracked:
            line = _out(workspace, 'ls-tree', 'HEAD', '--', name).decode().split()
            if line:
                modes[name] = line[0]
        if self._expected_tree(workspace, base, patch, untracked, modes) \
                != _out(workspace, 'rev-parse', 'HEAD^{tree}').decode().strip():
            raise ContractError('existing attempt commit does not match the captured snapshot')
        return head

    # -- public -------------------------------------------------------------------------
    def finalize(self, result, *, terminated, current_generation, policy_check, branch, allowed_paths,
                 expected_head=None):
        """Return `result` with result_commit set and diff_ref cleared (a committed candidate).

        terminated: callable -> True only when the agent process group is proven exited.
        policy_check: callable -> True only if a fresh policy evaluation still allows finalization.
        branch: the attempt's feature branch. expected_head: defaults to result.base_commit.
        allowed_paths: non-empty tuple of repository-relative files or directories.
        """
        if type(allowed_paths) is not tuple or not allowed_paths or not all(
                type(item) is str and item for item in allowed_paths):
            raise ContractError('finalization requires explicit allowed paths')
        base, patch, untracked = self._load(result)
        for name in (*result.changes, *untracked):
            if _protected(name):
                raise ContractError('protected or secret path cannot be finalized')
        workspace = Path(result.workspace)
        self._gate(result, terminated=terminated, current_generation=current_generation,
                   policy_check=policy_check, workspace=workspace, branch=branch, head_ok=None)
        message = commit_message(result, result.diff_ref)
        head = _out(workspace, 'rev-parse', 'HEAD').decode().strip()
        expected = expected_head or result.base_commit
        if head != expected:
            recovered = self._recovered(workspace, result, base, patch, untracked, message)
            if recovered is None:
                raise ContractError('worktree HEAD is not the expected candidate base')
            return replace(result, result_commit=recovered, diff_ref=None)
        if _out(workspace, 'diff', '--cached', '--name-only', '-z'):
            raise ContractError('index is not clean; refusing to finalize')
        if _out(workspace, 'ls-files', '-u'):
            raise ContractError('unmerged paths; refusing to finalize')
        changes, current_untracked = self._changed(workspace)
        for name in changes:
            if _protected(name):
                raise ContractError('protected or secret path cannot be finalized')
        if changes != tuple(sorted(result.changes)) or set(current_untracked) != set(untracked):
            raise ContractError('working tree changed after the candidate was captured')
        if _out(workspace, 'diff', '--binary', 'HEAD') != patch:
            raise ContractError('working tree changed after the candidate was captured')
        for name in current_untracked:
            path = workspace / name
            if path.is_symlink() or not path.is_file() or path.read_bytes() != untracked[name]:
                raise ContractError('working tree changed after the candidate was captured')
        for name in changes:
            if not _permitted(name, allowed_paths):
                raise ContractError('changed path is outside the allowed paths')
            if (workspace / name).is_symlink():
                raise ContractError('symlink candidate paths require separate review')
        env = {'GIT_AUTHOR_NAME': self.author[0], 'GIT_AUTHOR_EMAIL': self.author[1],
               'GIT_COMMITTER_NAME': self.author[0], 'GIT_COMMITTER_EMAIL': self.author[1],
               'GIT_CONFIG_NOSYSTEM': '1'}
        if self.commit_date:
            env.update(GIT_AUTHOR_DATE=self.commit_date, GIT_COMMITTER_DATE=self.commit_date)
        _run(workspace, 'add', '-A', '--', *changes)
        done = _run(workspace, '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                    'commit', '-q', '-F', '-', env=env, data=message.encode(), check=False)
        if done.returncode:
            _run(workspace, 'reset', '-q', 'HEAD', '--', *changes, check=False)
            raise ContractError('candidate commit failed; index restored')
        commit = _out(workspace, 'rev-parse', 'HEAD').decode().strip()
        if _out(workspace, 'status', '--porcelain', '--untracked-files=all'):
            raise ContractError('worktree is not clean after finalization')
        return replace(result, result_commit=commit, diff_ref=None)
