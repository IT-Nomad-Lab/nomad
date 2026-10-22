"""Integration policy for verified candidates: the exact verified commit must contain the
current tip of the default branch (V17).

When the default branch has moved past the candidate's base, the controller merges the tip into
the attempt's feature branch with a deterministic merge commit and verifies that commit again.
The merge is computed with `git merge-tree` first, so a conflict leaves the worktree untouched;
only a clean result is applied, and only as a fast-forward of a clean worktree. Nothing here
moves the default branch, pushes, stashes or resets.
"""
from dataclasses import replace
from pathlib import Path
import re

from .contracts import ContractError
from .finalization import _out, _run

_SHA = re.compile(r'[0-9a-f]{40}')


class IntegrationConflict(ContractError):
    """The default branch conflicts with the candidate; it needs rework, not another merge."""


def merge_message(result, tip):
    return (f'merge(execution): integrate default branch into {result.task_id}\n\n'
            f'Execution-Attempt: {result.attempt_id}\n'
            f'Execution-Generation: {result.generation}\n'
            f'Integrates: {tip}\n')


class DefaultBranchIntegration:
    """branch: the repository's default branch, or {canonical repository path: default branch}
    when one controller serves repositories with different names. max_rounds bounds how often a
    moving tip is chased before the attempt is left awaiting verification for a later retry."""

    def __init__(self, author, *, branch='main', max_rounds=3, commit_date=None):
        if (type(author) is not tuple or len(author) != 2
                or not all(type(item) is str and item.strip() and '\n' not in item and '\x00' not in item
                           for item in author)):
            raise ContractError('integration requires an explicit fixed author identity')
        names = list(branch.values()) if type(branch) is dict else [branch]
        if not names or (type(branch) is dict and not all(type(path) is str and path.startswith('/')
                                                           for path in branch)):
            raise ContractError('integration requires the default branch name')
        for name in names:
            if type(name) is not str or not name or name.startswith(('-', 'feat/', 'fix/')) or '..' in name:
                raise ContractError('integration requires the default branch name')
        if type(max_rounds) is not int or max_rounds < 1:
            raise ContractError('integration requires a positive round limit')
        self.author, self.branch, self.max_rounds, self.commit_date = author, branch, max_rounds, commit_date

    def branch_of(self, task):
        if type(self.branch) is str:
            return self.branch
        if task.repository.path not in self.branch:
            raise ContractError('no default branch configured for this repository')
        return self.branch[task.repository.path]

    def tip(self, task):
        tip = _out(task.repository.path, 'rev-parse', '--verify', '--end-of-options',
                   f'refs/heads/{self.branch_of(task)}^{{commit}}').decode().strip()
        if not _SHA.fullmatch(tip):
            raise ContractError('default branch tip is not a commit')
        return tip

    @staticmethod
    def contains(workspace, tip, commit):
        return _run(workspace, 'merge-base', '--is-ancestor', tip, commit, check=False).returncode == 0

    def _worktree(self, workspace):
        path = Path(workspace)
        if not path.is_absolute() or path.resolve() != path or not path.is_dir():
            raise ContractError('integration requires an isolated canonical worktree')
        gitdir = _out(path, 'rev-parse', '--path-format=absolute', '--git-dir').strip()
        common = _out(path, 'rev-parse', '--path-format=absolute', '--git-common-dir').strip()
        if gitdir == common or _out(path, 'rev-parse', '--show-toplevel').decode().strip() != str(path):
            raise ContractError('candidate is not in its own linked worktree')
        if not _out(path, 'branch', '--show-current').decode().strip().startswith(('feat/', 'fix/')):
            raise ContractError('integration requires the attempt feature branch')
        if _out(path, 'status', '--porcelain', '--untracked-files=all'):
            raise ContractError('worktree is not clean; refusing to integrate')
        return path

    def _recovered(self, workspace, candidate):
        """A crash after the fast-forward but before the store recorded it leaves our own merge
        commit at HEAD; adopt it only if its first parent and message prove it is ours."""
        head = _out(workspace, 'rev-parse', 'HEAD').decode().strip()
        parents = _out(workspace, 'rev-list', '--parents', '-n', '1', 'HEAD').decode().split()
        if len(parents) != 3 or parents[1] != candidate.result_commit:
            return None
        if _out(workspace, 'log', '-1', '--format=%B', 'HEAD').decode().strip() \
                != merge_message(candidate, parents[2]).strip():
            return None
        return head

    def merge(self, candidate, tip, *, terminated, policy_check):
        """Return `candidate` with result_commit set to a merge of `tip` into it."""
        if candidate.result_commit is None or candidate.diff_ref is not None:
            raise ContractError('integration requires a committed candidate')
        if not callable(terminated) or terminated() is not True:
            raise ContractError('agent termination is not proven; refusing to integrate')
        try:
            allowed = policy_check()
        except Exception as exc:
            raise ContractError('policy re-check failed; refusing to integrate') from exc
        if allowed is not True:
            raise ContractError('policy no longer permits integration')
        workspace = self._worktree(candidate.workspace)
        head = _out(workspace, 'rev-parse', 'HEAD').decode().strip()
        if head != candidate.result_commit:
            recovered = self._recovered(workspace, candidate)
            if recovered is None:
                raise ContractError('worktree HEAD is not the recorded candidate')
            return replace(candidate, result_commit=recovered)
        merged = _run(workspace, 'merge-tree', '--write-tree', '--no-messages', head, tip, check=False)
        if merged.returncode == 1:
            raise IntegrationConflict('default branch conflicts with the candidate')
        if merged.returncode:
            raise ContractError('integration merge computation failed')
        tree = merged.stdout.decode().split('\n', 1)[0].strip()
        if not _SHA.fullmatch(tree):
            raise ContractError('integration merge produced no tree')
        env = {'GIT_AUTHOR_NAME': self.author[0], 'GIT_AUTHOR_EMAIL': self.author[1],
               'GIT_COMMITTER_NAME': self.author[0], 'GIT_COMMITTER_EMAIL': self.author[1],
               'GIT_CONFIG_NOSYSTEM': '1'}
        if self.commit_date:
            env.update(GIT_AUTHOR_DATE=self.commit_date, GIT_COMMITTER_DATE=self.commit_date)
        commit = _out(workspace, 'commit-tree', tree, '-p', head, '-p', tip, '-F', '-', env=env,
                      data=merge_message(candidate, tip).encode()).decode().strip()
        _run(workspace, '-c', 'core.hooksPath=/dev/null', 'merge', '-q', '--ff-only', commit)
        if _out(workspace, 'rev-parse', 'HEAD').decode().strip() != commit \
                or _out(workspace, 'status', '--porcelain', '--untracked-files=all'):
            raise ContractError('worktree did not fast-forward cleanly to the integration merge')
        return replace(candidate, result_commit=commit)
