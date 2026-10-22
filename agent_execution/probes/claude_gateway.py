"""Installed Claude CLI reaching an external effect only through the per-attempt MCP gateway.

Fake loopback provider, fixture key, disposable worktree and store; no real model or API spend.
The provider asks for one approved effect, a changed-content call, a replay and an unapproved
call. Exactly the approved one may reach the handler, once, and the CLI must be offered the
gateway's tool and no other MCP tool. A call to the CLI's own deferred-tool placeholder must fail.
"""
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid

from agent_execution.contracts import Boundary
from agent_execution.controller import ExecutionController
from agent_execution.enforcement import ClaudeEnforcement
from agent_execution.native import NativeAdapter, NativeProfile
from agent_execution.policy import EffectivePolicy, OperatorApproval, ToolProfile, action_scope
from agent_execution.tool_authority import AuthorityServer, ToolAuthority, ToolRoute
from agent_execution.tests.fixtures import agent, task
from agent_execution.tests.test_native import NativeExecution

ROUTE = 'mcp:tracker:comment'
TOOL = 'mcp__nomad_effects__tracker__comment'
PLACEHOLDER = 'DeferredToolPlaceholder'


def sse(message):
    events = [('message_start', {'type': 'message_start', 'message': dict(message, content=[], stop_reason=None)})]
    for index, block in enumerate(message['content']):
        initial = dict(block, input={}) if block['type'] == 'tool_use' else dict(block, text='')
        delta = ({'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])}
                 if block['type'] == 'tool_use' else {'type': 'text_delta', 'text': block['text']})
        events += [('content_block_start', {'type': 'content_block_start', 'index': index, 'content_block': initial}),
                   ('content_block_delta', {'type': 'content_block_delta', 'index': index, 'delta': delta}),
                   ('content_block_stop', {'type': 'content_block_stop', 'index': index})]
    events += [('message_delta', {'type': 'message_delta', 'usage': {'output_tokens': 1},
                                  'delta': {'stop_reason': message['stop_reason'], 'stop_sequence': None}}),
               ('message_stop', {'type': 'message_stop'})]
    return ''.join(f'event: {name}\ndata: {json.dumps(value)}\n\n' for name, value in events).encode()


def main():
    fixture = NativeExecution('test_codex_consumes_full_context_and_normalizes_unknown_cost')
    fixture.setUp()
    controller = server = None
    try:
        approved = {'task': 'task:123', 'body': 'approved ' + uuid.uuid4().hex}
        obs = {'requests': 0, 'mcp_tools': None, 'builtin_tools': None, 'results': {}, 'dispatched': []}
        calls = None

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                size = int(self.headers.get('Content-Length', '0'))
                if size > 8 * 1024 * 1024 or self.headers.get('x-api-key') != 'fixture-only-not-credential':
                    self.send_error(403)
                    return
                data = json.loads(self.rfile.read(size))
                if self.path.endswith('/count_tokens'):
                    body, kind = b'{"input_tokens":1}', 'application/json'
                else:
                    obs['requests'] += 1
                    names = [tool.get('name') for tool in data.get('tools', [])]
                    if obs['mcp_tools'] is None and names:
                        obs['mcp_tools'] = sorted(n for n in names if n.startswith('mcp__'))
                        obs['builtin_tools'] = sorted(n for n in names if not n.startswith('mcp__'))
                    for message in data.get('messages', []):
                        for block in message.get('content') if isinstance(message.get('content'), list) else []:
                            if block.get('type') == 'tool_result':
                                obs['results'][block['tool_use_id']] = (bool(block.get('is_error')), json.dumps(block.get('content')))
                    if obs['requests'] == 1:
                        content, stop = [{'type': 'tool_use', 'id': ident, 'name': name, 'input': arguments}
                                         for ident, name, arguments in calls], 'tool_use'
                    else:
                        content, stop = [{'type': 'text', 'text': 'gateway fixture completed.'}], 'end_turn'
                    body, kind = sse({'id': 'msg_' + uuid.uuid4().hex, 'type': 'message', 'role': 'assistant',
                                      'model': 'fixture', 'content': content, 'stop_reason': stop, 'stop_sequence': None,
                                      'usage': {'input_tokens': 1, 'output_tokens': 1}}), 'text/event-stream'
                self.send_response(200)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        spec = replace(task(), repository=replace(task().repository, path=str(fixture.repo), base_commit=fixture.base),
                       execution_policy=replace(task().execution_policy, allowed_actions=('read', 'write', 'shell'),
                                                approval_actions=('publish',)),
                       budget=replace(task().budget, wall_seconds=20))
        native_agent = replace(agent(), supported_tools=('Read', 'Write', 'Edit', 'Bash', ROUTE))
        profile_tools = ToolProfile(ref='tools:offline', policy_revision='p1', agent_ids=('local',),
                                    tools=native_agent.supported_tools, allowed_actions=('read', 'write', 'shell'),
                                    approval_actions=('publish',), max_wall_seconds=20,
                                    cost_policy_refs=('budget:no-paid-calls',), local_route_verified=True)
        policy = EffectivePolicy(current_task=lambda _: spec, profiles=(profile_tools,), operators=('operator',),
                                 store=fixture.store)
        scope = action_scope(spec, tool=ROUTE, action='publish', target=approved['task'], arguments=approved)
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        policy.register_approval(OperatorApproval(approval_id='probe-approval', actor_id='operator', scope=scope,
                                                  expires_at=expires), authenticated_actor='operator')
        # The CLI always offers its own deferred-loading placeholder; calling it must still be refused.
        calls = [('use_approved', TOOL, dict(approved, approval_id='probe-approval')),
                 ('use_changed', TOOL, dict(approved, body='changed', approval_id='probe-approval')),
                 ('use_replay', TOOL, dict(approved, approval_id='probe-approval')),
                 ('use_unapproved', TOOL, dict(approved)),
                 ('use_placeholder', PLACEHOLDER, {})]
        route = ToolRoute(ROUTE, 'publish', lambda arguments: arguments['task'],
                          lambda arguments: obs['dispatched'].append(arguments) or {'posted': True}, Boundary.LOCAL)
        socket_for = lambda run: fixture.root / 'authority' / (run.attempt_id + '.sock')
        prefix = Path(os.environ.get('NOMAD_SANDBOX_DEP_PREFIX', Path.home() / '.local/share/nomad-native-deps'))
        profile = NativeProfile(backend='local', agent=native_agent, command_prefix=(shutil.which('claude'),),
                                settings_args=(), policy_revision='p1', tool_profile_ref='tools:offline', environment={
                                    'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{server.server_port}',
                                    'ANTHROPIC_API_KEY': 'fixture-only-not-credential',
                                    'PATH': str(prefix / 'usr/bin') + ':' + os.environ['PATH'],
                                    'LD_LIBRARY_PATH': str(prefix / 'usr/lib/x86_64-linux-gnu'),
                                    'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1', 'DISABLE_AUTOUPDATER': '1'})
        enforcement = ClaudeEnforcement(fixture.root / 'permissions', socket_for, lambda run: (ROUTE,))
        adapter = NativeAdapter(profile, fixture.artifacts, fixture.store, fixture.root / 'native-canary',
                                kill_grace=0.2, enforcement=enforcement)
        authority_for = lambda task_, agent_, run: AuthorityServer(
            socket_for(run), ToolAuthority(policy, task_, agent_, run, routes=(route,), sandbox_verified=True))
        controller = ExecutionController(fixture.store, fixture.artifacts, {'local': adapter}, authorize=policy,
                                         resource_for=lambda *_: nullcontext(), tool_authority_for=authority_for)
        run = controller.start(spec, 'local', fixture.instructions, workspace=str(fixture.worktree))
        fixture.adapters.append((adapter, run))
        until = time.monotonic() + 40
        while time.monotonic() < until and adapter.recover(run).status != 'exited':
            time.sleep(0.05)
        state = adapter.recover(run)
        results = obs['results']
        succeeded = sorted(ident for ident, (error, _) in results.items() if not error)
        print(json.dumps({'state': state.status, 'requests': obs['requests'], 'mcp_tools': obs['mcp_tools'],
                          'builtin_tools': obs['builtin_tools'], 'succeeded': succeeded,
                          'dispatched': len(obs['dispatched']), 'results': results}, indent=1))
        ok = (state.status == 'exited' and obs['mcp_tools'] == [TOOL]
              and set(obs['builtin_tools'] or ()) <= {'Read', 'Write', 'Edit', 'Bash', PLACEHOLDER}
              and set(results) == {ident for ident, _, _ in calls}
              and obs['dispatched'] == [approved] and len(succeeded) == 1
              and succeeded[0] in ('use_approved', 'use_replay') and '"posted": true' in results[succeeded[0]][1].replace('\\', ''))
        if not ok:
            _, _, _, errors = adapter._paths(run)
            print('native stderr:', errors.read_text()[-2000:])
            raise SystemExit('gateway canary did not pass; MCP effects stay disabled')
        print('installed Claude CLI: external effect reached only via the gateway, once, with exact approval; fake provider only')
        subprocess.run([shutil.which('claude'), '--version'], check=True, timeout=10)
    finally:
        if controller is not None:
            controller.close()
        if server is not None:
            server.shutdown()
            server.server_close()
        fixture.doCleanups()


if __name__ == '__main__':
    main()
