"""Actual private socket/hook dispatch and approval replay refusals."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_execution.contracts import Boundary, ContractError
from agent_execution.policy import EffectivePolicy, OperatorApproval, ToolProfile, action_scope
from agent_execution.store import AttemptStore
from agent_execution.tool_authority import (AuthorityServer, SOCKET_PATH_MAX, ToolAuthority, ToolRoute,
                                            authority_socket_path, request_authority, runtime_base)
from agent_execution.tests.fixtures import ARTIFACT, agent, task


class AuthorityFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        workspace = self.root / 'worktree'
        workspace.mkdir()
        self.task = replace(task(), repository=replace(task().repository, path=str(workspace)),
                            execution_policy=replace(task().execution_policy, allowed_actions=('read', 'write', 'shell'),
                                                     approval_actions=('publish',)))
        self.current = self.task
        self.agent = replace(agent(), supported_tools=('Read', 'Write', 'Edit', 'Bash', 'apply_patch',
                                                                 'mcp:tracker:comment'))
        profile = ToolProfile(ref='tools:offline', policy_revision='p1', agent_ids=('local',),
                               tools=self.agent.supported_tools, allowed_actions=('read', 'write', 'shell'),
                               approval_actions=('publish',), max_wall_seconds=60,
                               cost_policy_refs=('budget:no-paid-calls',), local_route_verified=True)
        self.store = AttemptStore(self.root / 'attempts.db', clock=lambda: 1000)
        self.addCleanup(self.store.close)
        self.policy = EffectivePolicy(current_task=lambda _: self.current, profiles=(profile,),
                                       operators=('operator',), store=self.store, clock=lambda: 1000)
        self.run = self.store.claim(self.task, ARTIFACT, agent_id='local', workspace=str(workspace), owner_id='scheduler')
        self.run = self.store.record_process(self.run.attempt_id, 'scheduler', self.run.claim.generation,
                                             pid=12345, start_time='fixture')
        self.dispatch = Mock(return_value={'posted': True})
        self.route = ToolRoute('mcp:tracker:comment', 'publish', lambda arguments: arguments['task'], self.dispatch, Boundary.LOCAL)
        self.authority = ToolAuthority(self.policy, self.task, self.agent, self.run,
                                        routes=(self.route,), sandbox_verified=True)
        self.socket = self.root / 'authority' / 'tools.sock'

    def request(self, tool='Read', arguments=None, **kw):
        return dict(attempt_id=self.run.attempt_id, generation=self.run.claim.generation,
                    tool=tool, arguments=arguments or {'file_path': 'module.py'}, **kw)

    def approve(self, arguments):
        scope = action_scope(self.task, tool=self.route.tool, action=self.route.action,
                             target=arguments['task'], arguments=arguments)
        approval = OperatorApproval(approval_id='approved-comment', actor_id='operator', scope=scope,
                                    expires_at='1970-01-01T00:30:00Z')
        self.policy.register_approval(approval, authenticated_actor='operator')
        return approval.approval_id

    def hook(self, payload):
        hook = Path(__file__).resolve().parents[1] / 'tool_hook.py'
        result = subprocess.run([sys.executable, str(hook), str(self.socket), self.run.attempt_id,
                                 str(self.run.claim.generation)], input=payload, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        return json.loads(result.stdout)['hookSpecificOutput']['permissionDecision']


class NativeToolAuthority(AuthorityFixture):
    def test_socket_checks_fresh_task_before_allowing_builtin_tool(self):
        with AuthorityServer(self.socket, self.authority):
            self.assertTrue(request_authority(self.socket, self.request())['allow'])
            self.current = replace(self.task, task_revision='new')
            with self.assertRaises(ContractError):
                request_authority(self.socket, self.request())

    def test_path_escape_symlink_secret_and_unknown_tools_are_refused(self):
        (Path(self.run.workspace) / 'escape').symlink_to(self.root)
        for path in ('../private', '/etc/passwd', 'escape/private', '.env', 'nested/.env.local',
                     '.git/config', '.claude/settings.json', 'AGENTS.md', 'secret.pem', '.npmrc', 'nested/auth.json'):
            with self.subTest(path=path), self.assertRaises(ContractError):
                self.authority.handle(self.request(arguments={'file_path': path}))
        for tool in ('Agent', 'WebFetch', 'mcp:unknown'):
            with self.subTest(tool=tool), self.assertRaises(ContractError):
                self.authority.handle(self.request(tool))

    def test_request_cannot_supply_action_classification_or_foreign_generation(self):
        request = self.request()
        for changed in (dict(request, action='read'), dict(request, generation=True),
                        dict(request, generation=999), dict(request, attempt_id='other')):
            with self.assertRaises(ContractError):
                self.authority.handle(changed)

    def test_unverified_sandbox_and_shell_escape_flags_are_refused(self):
        authority = ToolAuthority(self.policy, self.task, self.agent, self.run)
        with self.assertRaises(ContractError):
            authority.handle(self.request())
        for flag in ('dangerouslyDisableSandbox', 'run_in_background'):
            with self.assertRaises(ContractError):
                self.authority.handle(self.request('Bash', {'command': 'echo fixture', flag: True}))

    def test_actual_external_dispatch_requires_exact_approval_and_cannot_replay(self):
        arguments = {'task': 'task:123', 'body': 'approved content'}
        approval = self.approve(arguments)
        with AuthorityServer(self.socket, self.authority):
            with self.assertRaises(ContractError):
                request_authority(self.socket, self.request(self.route.tool, dict(arguments, body='changed'), approval_id=approval))
            self.dispatch.assert_not_called()
            result = request_authority(self.socket, self.request(self.route.tool, arguments, approval_id=approval))
            self.assertTrue(result['result']['posted'])
            with self.assertRaises(ContractError):
                request_authority(self.socket, self.request(self.route.tool, arguments, approval_id=approval))
        self.dispatch.assert_called_once_with(arguments)

    def test_uncertain_external_effect_is_not_retried_with_consumed_approval(self):
        arguments = {'task': 'task:123', 'body': 'approved content'}
        approval = self.approve(arguments)
        self.dispatch.side_effect = OSError('remote acknowledgement lost')
        with AuthorityServer(self.socket, self.authority):
            for _ in range(2):
                with self.assertRaises(ContractError):
                    request_authority(self.socket, self.request(self.route.tool, arguments, approval_id=approval))
        self.dispatch.assert_called_once_with(arguments)

    def test_local_only_task_cannot_use_cloud_tool_even_with_operator_approval(self):
        arguments = {'task': 'task:123', 'body': 'approved content'}
        approval = self.approve(arguments)
        cloud = ToolAuthority(self.policy, self.task, self.agent, self.run,
                               routes=(replace(self.route, boundary=Boundary.CLOUD),), sandbox_verified=True)
        with self.assertRaises(ContractError):
            cloud.handle(self.request(self.route.tool, arguments, approval_id=approval))
        self.dispatch.assert_not_called()
        self.authority.handle(self.request(self.route.tool, arguments, approval_id=approval))
        self.dispatch.assert_called_once_with(arguments)

    def test_real_hook_process_denies_malformed_input_dead_authority_and_unmediated_mcp(self):
        self.assertEqual(self.hook('malformed'), 'deny')
        payload = json.dumps({'tool_name': 'Read', 'tool_input': {'file_path': 'module.py'}})
        self.assertEqual(self.hook(payload), 'deny')
        with AuthorityServer(self.socket, self.authority):
            self.assertEqual(self.hook(payload), 'allow')
            self.assertEqual(self.hook(json.dumps({'tool_name': 'mcp__untrusted__send', 'tool_input': {}})), 'deny')
            self.current = replace(self.task, task_revision='changed')
            self.assertEqual(self.hook(payload), 'deny')


class CodexApplyPatch(AuthorityFixture):
    def patch(self, *body):
        return self.request('apply_patch', {'command': '\n'.join(('*** Begin Patch', *body, '*** End Patch')) + '\n'})

    def test_checked_patch_paths_are_authorized_as_one_write(self):
        workspace = Path(self.run.workspace)
        request = self.patch(
            '*** Add File: docs/new.md', '+hello',
            f'*** Update File: {workspace / "src" / "b.py"}', '@@ def f():', '-    return 1', '+    return 2',
            '*** End of File',
            '*** Update File: old.py', '*** Move to: pkg/new.py', '@@', ' kept',
            '*** Delete File: gone.py')
        self.assertEqual(self.authority.handle(request), {'allow': True})
        # Every checked path, Move-to targets included, is the action target an audit names.
        self.assertEqual(self.authority._patch_paths(request['arguments']),
                         [str(workspace / name) for name in
                          ('docs/new.md', 'src/b.py', 'old.py', 'pkg/new.py', 'gone.py')])

    def test_any_unchecked_or_protected_path_refuses_the_whole_patch(self):
        (Path(self.run.workspace) / 'escape').symlink_to(self.root)
        for path in ('../private', '/etc/passwd', 'escape/private', 'AGENTS.md', 'nested/CLAUDE.md',
                     '.env', 'deploy/key.pem', '.git/config', '.codex/auth.json', ''):
            for body in ((f'*** Add File: {path}', '+x'), (f'*** Delete File: {path}',),
                         ('*** Update File: ok.py', f'*** Move to: {path}', '@@', ' x'),
                         ('*** Add File: fine.py', '+x', f'*** Update File: {path}', '@@', '-a', '+b')):
                with self.subTest(path=path, body=body[0]), self.assertRaises(ContractError):
                    self.authority.handle(self.patch(*body))

    def test_anything_codex_could_parse_differently_is_refused(self):
        refused = {
            'heredoc wrapper': "apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: a.py\n+x\n*** End Patch\nEOF\n",
            'no begin': '*** Add File: a.py\n+x\n*** End Patch\n',
            'no end': '*** Begin Patch\n*** Add File: a.py\n+x\n',
            'indented begin': ' *** Begin Patch\n*** Add File: a.py\n+x\n*** End Patch\n',
            'no file': '*** Begin Patch\n*** End Patch\n',
            'empty': '',
            'indented header': '*** Begin Patch\n*** Add File: a.py\n+x\n  *** Add File: AGENTS.md\n+y\n*** End Patch\n',
            'trailing space': '*** Begin Patch\n*** Update File: AGENTS.md \n@@\n-a\n+b\n*** End Patch\n',
            'unknown header': '*** Begin Patch\n*** Add File: a.py\n+x\n*** Rename File: AGENTS.md\n*** End Patch\n',
            'header without space': '*** Begin Patch\n***Add File: AGENTS.md\n+x\n*** End Patch\n',
            'move without update': '*** Begin Patch\n*** Add File: a.py\n*** Move to: b.py\n+x\n*** End Patch\n',
            'carriage return': '*** Begin Patch\n*** Update File: AGENTS.md\r\n@@\n-a\n+b\n*** End Patch\n',
            'only end of file': '*** Begin Patch\n*** End of File\n*** End Patch\n',
            'nested envelope': '*** Begin Patch\n*** Add File: a.py\n+x\n*** End Patch\n*** Begin Patch\n*** End Patch\n',
        }
        for name, command in refused.items():
            with self.subTest(name), self.assertRaises(ContractError):
                self.authority.handle(self.request('apply_patch', {'command': command}))
        for arguments in ({'command': 7}, {}, {'command': '*** Begin Patch', 'input': 'x'}):
            with self.subTest(arguments=arguments), self.assertRaises(ContractError):
                self.authority.handle(self.request('apply_patch', arguments))

    def test_real_hook_process_allows_a_checked_patch_without_rewriting_it(self):
        payload = {'tool_name': 'apply_patch',
                   'tool_input': {'command': '*** Begin Patch\n*** Add File: a.py\n+x\n*** End Patch\n'}}
        with AuthorityServer(self.socket, self.authority):
            hook = Path(__file__).resolve().parents[1] / 'tool_hook.py'
            result = subprocess.run([sys.executable, str(hook), str(self.socket), self.run.attempt_id,
                                     str(self.run.claim.generation)], input=json.dumps(payload),
                                    capture_output=True, text=True, timeout=5)
            output = json.loads(result.stdout)['hookSpecificOutput']
            self.assertEqual((output['permissionDecision'], 'updatedInput' in output), ('allow', False))
            payload['tool_input']['command'] = payload['tool_input']['command'].replace('a.py', 'AGENTS.md')
            self.assertEqual(self.hook(json.dumps(payload)), 'deny')

class AuthoritySocketPathTest(unittest.TestCase):
    """#1497: the socket binds under the runtime dir, so its length does not depend on the root."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.attempt = '0' * 36

    def test_a_long_or_unicode_root_keeps_a_short_socket(self):
        for root in (self.tmp / ('x' * 200), self.tmp / ('é' * 80) / 'execution'):
            path = authority_socket_path(root, self.attempt, self.tmp / 'run')
            self.assertEqual(path.parent.parent, self.tmp / 'run' / 'nomad-exec')
            self.assertLessEqual(len(str(path).encode()), SOCKET_PATH_MAX)

    def test_distinct_roots_get_distinct_directories_and_the_same_root_the_same(self):
        a = authority_socket_path(self.tmp / 'a', self.attempt, self.tmp / 'run')
        b = authority_socket_path(self.tmp / 'b', self.attempt, self.tmp / 'run')
        self.assertNotEqual(a.parent, b.parent)
        self.assertEqual(a, authority_socket_path(self.tmp / 'x' / '..' / 'a', self.attempt, self.tmp / 'run'))

    def test_an_overlong_runtime_dir_is_refused(self):
        with self.assertRaisesRegex(ContractError, 'AF_UNIX allows 107'):
            authority_socket_path(self.tmp / 'a', self.attempt, self.tmp / ('r' * 80))

    def test_runtime_base_takes_the_first_private_owned_dir(self):
        private, shared = self.tmp / 'private', self.tmp / 'shared'
        private.mkdir(mode=0o700)
        shared.mkdir()
        shared.chmod(0o755)
        link = self.tmp / 'link'
        link.symlink_to(private)
        self.assertEqual(runtime_base(('', 'relative/dir', str(self.tmp / 'missing'), str(shared), str(private))),
                         private.resolve())
        self.assertEqual(runtime_base((str(link),)), private.resolve())
        with self.assertRaisesRegex(ContractError, 'no private runtime directory'):
            runtime_base((str(shared), str(self.tmp / 'missing')))
        with patch('agent_execution.tool_authority.os.geteuid', return_value=os.geteuid() + 1):
            with self.assertRaises(ContractError):
                runtime_base((str(private),))

    def test_the_server_refuses_a_socket_directory_owned_by_someone_else(self):
        path = authority_socket_path(self.tmp / 'a', self.attempt, self.tmp / 'run')
        path.parent.mkdir(parents=True, mode=0o700)
        with patch('agent_execution.tool_authority.os.geteuid', return_value=os.geteuid() + 1):
            with self.assertRaisesRegex(ContractError, 'operator owns'):
                AuthorityServer(path, Mock()).__enter__()

    def test_the_server_binds_and_cleans_up_at_the_resolved_path(self):
        path = authority_socket_path(self.tmp / 'a', self.attempt, self.tmp / 'run')
        with AuthorityServer(path, Mock()):
            self.assertTrue(path.is_socket())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)


if __name__ == '__main__':
    unittest.main()
