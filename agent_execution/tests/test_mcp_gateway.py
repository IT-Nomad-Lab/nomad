"""The per-attempt MCP gateway process, its hook namespace and its enforcement wiring."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import unittest

from agent_execution.contracts import ContractError, Privacy
from agent_execution.enforcement import ClaudeEnforcement, claude_settings, gateway_mcp_config
from agent_execution.mcp_gateway import exposed_name, routes_by_name
from agent_execution.policy import EffectivePolicy, ToolProfile
from agent_execution.store import AttemptStore
from agent_execution.tool_authority import AuthorityServer, ToolAuthority
from agent_execution.tests.fixtures import ARTIFACT, agent
from agent_execution.tests.test_tool_authority import AuthorityFixture

GATEWAY = Path(__file__).resolve().parents[1] / 'mcp_gateway.py'


class GatewayNames(unittest.TestCase):
    def test_routes_map_to_unique_reversible_names(self):
        self.assertEqual(exposed_name('mcp:tracker:comment'), 'tracker__comment')
        self.assertEqual(exposed_name('mcp:nomad-pm:add_comment'), 'nomad-pm__add_comment')
        for bad in ('tracker:comment', 'mcp:tracker', 'mcp:a:b:c', 'mcp:a__b:c', 'mcp:a:_b', 'mcp:a:b_',
                    'mcp:a b:c', 'mcp::c', 'mcp:a:' + 'x' * 60, None):
            with self.subTest(route=bad), self.assertRaises(ValueError):
                exposed_name(bad)
        with self.assertRaises(ValueError):
            routes_by_name(['mcp:tracker:comment', 'mcp:tracker:comment'])


class GatewayProcess(AuthorityFixture):
    def gateway(self, *messages, generation=None, routes=('mcp:tracker:comment',)):
        generation = self.run.claim.generation if generation is None else generation
        lines = ''.join(json.dumps(dict(message, jsonrpc='2.0')) + '\n' for message in messages)
        result = subprocess.run([sys.executable, str(GATEWAY), str(self.socket), self.run.attempt_id, str(generation),
                                 *routes], input=lines, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return {reply['id']: reply for reply in map(json.loads, result.stdout.splitlines())}

    def call(self, ident, arguments, name='tracker__comment'):
        return {'id': ident, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}

    def text(self, reply):
        result = reply['result']
        return result['isError'], result['content'][0]['text']

    def test_lists_only_registered_routes_and_answers_protocol(self):
        with AuthorityServer(self.socket, self.authority):
            replies = self.gateway({'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
                                   {'method': 'notifications/initialized'}, {'id': 2, 'method': 'tools/list'},
                                   {'id': 3, 'method': 'resources/list'}, {'id': 4, 'method': 'ping'})
        self.assertEqual(replies[1]['result']['protocolVersion'], '2025-06-18')
        self.assertEqual([tool['name'] for tool in replies[2]['result']['tools']], ['tracker__comment'])
        self.assertEqual(replies[3]['error']['code'], -32601)
        self.assertEqual(replies[4]['result'], {})
        self.assertEqual(set(replies), {1, 2, 3, 4})

    def test_effect_runs_once_with_exact_approval_and_refusals_do_not_echo(self):
        arguments = {'task': 'task:123', 'body': 'approved content'}
        approval = self.approve(arguments)
        with AuthorityServer(self.socket, self.authority):
            replies = self.gateway(
                self.call(1, dict(arguments, body='changed secret-ish text', approval_id=approval)),
                self.call(2, arguments),
                self.call(5, dict(arguments, approval_id=approval), name='tracker__delete'),
                self.call(6, 'not an object'),
                self.call(3, dict(arguments, approval_id=approval)),
                self.call(4, dict(arguments, approval_id=approval)))
        self.assertEqual(self.text(replies[3]), (False, json.dumps({'posted': True})))
        for ident in (1, 2, 4, 5, 6):
            failed, text = self.text(replies[ident])
            self.assertTrue(failed, ident)
            self.assertNotIn('secret-ish', text)
            self.assertNotIn(approval, text)
        self.dispatch.assert_called_once_with(arguments)

    def test_dead_authority_and_foreign_generation_refuse(self):
        arguments = {'task': 'task:123', 'body': 'approved content'}
        approval = self.approve(arguments)
        self.assertTrue(self.text(self.gateway(self.call(1, dict(arguments, approval_id=approval)))[1])[0])
        with AuthorityServer(self.socket, self.authority):
            replies = self.gateway(self.call(1, dict(arguments, approval_id=approval)),
                                   generation=self.run.claim.generation + 1)
        self.assertTrue(self.text(replies[1])[0])
        self.dispatch.assert_not_called()

    def test_hook_admits_only_the_gateway_namespace(self):
        self.assertEqual(self.hook(json.dumps({'tool_name': 'mcp__nomad_effects__tracker__comment', 'tool_input': {}})),
                         'allow')
        for tool in ('mcp__nomad_effects_evil__x', 'mcp__other__nomad_effects__x', 'mcp__nomad_effects', 'mcp__x__y'):
            with self.subTest(tool=tool):
                self.assertEqual(self.hook(json.dumps({'tool_name': tool, 'tool_input': {}})), 'deny')


class ResultPrivacy(AuthorityFixture):
    def cloud_authority(self, result_privacy):
        cloud = replace(agent('claude'), supported_tools=('mcp:tracker:comment',))
        self.task = replace(self.task, execution_policy=replace(self.task.execution_policy, privacy=Privacy.CLOUD_ALLOWED))
        self.current = self.task
        profile = ToolProfile(ref='tools:offline', policy_revision='p1', agent_ids=(cloud.agent_id,),
                              tools=cloud.supported_tools, allowed_actions=('read', 'write', 'shell'),
                              approval_actions=('publish',), max_wall_seconds=60, cost_policy_refs=('budget:no-paid-calls',))
        store = AttemptStore(self.root / 'cloud.db', clock=lambda: 1000)
        self.addCleanup(store.close)
        self.policy = EffectivePolicy(current_task=lambda _: self.current, profiles=(profile,), operators=('operator',),
                                      store=store, clock=lambda: 1000)
        self.run = store.claim(self.task, ARTIFACT, agent_id=cloud.agent_id, workspace=self.run.workspace,
                               owner_id='scheduler')
        self.run = store.record_process(self.run.attempt_id, 'scheduler', self.run.claim.generation, pid=1, start_time='f')
        return ToolAuthority(self.policy, self.task, cloud, self.run,
                             routes=(replace(self.route, result_privacy=result_privacy),), sandbox_verified=True)

    def test_local_only_result_never_reaches_a_cloud_agent_and_keeps_the_approval(self):
        arguments = {'task': 'task:123', 'body': 'approved content'}
        authority = self.cloud_authority(Privacy.LOCAL_ONLY)
        approval = self.approve(arguments)
        with self.assertRaises(ContractError):
            authority.handle(self.request(self.route.tool, arguments, approval_id=approval))
        self.dispatch.assert_not_called()
        released = ToolAuthority(self.policy, self.task, authority.agent, self.run,
                                 routes=(replace(self.route, result_privacy=Privacy.CLOUD_ALLOWED),), sandbox_verified=True)
        self.assertEqual(released.handle(self.request(self.route.tool, arguments, approval_id=approval)),
                         {'result': {'posted': True}})

    def test_result_privacy_must_be_explicit_class(self):
        with self.assertRaises(ContractError):
            replace(self.route, result_privacy='cloud_allowed')
        self.assertEqual(self.route.result_privacy, Privacy.LOCAL_ONLY)


class GatewayEnforcement(AuthorityFixture):
    def test_strict_config_holds_only_the_attempt_gateway(self):
        self.assertEqual(gateway_mcp_config(self.run, self.socket, ()), {'mcpServers': {}})
        self.assertNotIn('mcp__nomad_effects', claude_settings(self.run, self.socket)['permissions']['allow'])
        config = gateway_mcp_config(self.run, self.socket, ('mcp:tracker:comment',))
        self.assertEqual(list(config['mcpServers']), ['nomad_effects'])
        server = config['mcpServers']['nomad_effects']
        self.assertEqual(server['args'], [str(GATEWAY), str(self.socket), self.run.attempt_id,
                                          str(self.run.claim.generation), 'mcp:tracker:comment'])
        self.assertEqual(server['env'], {})
        with self.assertRaises(ContractError):
            gateway_mcp_config(self.run, self.socket, ('mcp:a__b:c',))

    def test_enforcement_passes_gateway_config_and_allows_its_namespace(self):
        enforcement = ClaudeEnforcement(self.root / 'permissions', lambda run: self.socket,
                                        lambda run: ('mcp:tracker:comment',))
        args = list(enforcement(self.run))
        self.assertIn('--strict-mcp-config', args)
        config = json.loads(args[args.index('--mcp-config') + 1])
        self.assertEqual(list(config['mcpServers']), ['nomad_effects'])
        settings = json.loads(Path(args[args.index('--settings') + 1]).read_text())
        self.assertIn('mcp__nomad_effects', settings['permissions']['allow'])
        plain = ClaudeEnforcement(self.root / 'plain', lambda run: self.socket)
        args = list(plain(self.run))
        self.assertEqual(json.loads(args[args.index('--mcp-config') + 1]), {'mcpServers': {}})
        with self.assertRaises(ContractError):
            ClaudeEnforcement(self.root / 'x', lambda run: self.socket, ('mcp:tracker:comment',))


if __name__ == '__main__':
    unittest.main()
