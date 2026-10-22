"""Actual installed Claude CLI against a fake loopback provider; no real model/API spend."""
from contextlib import nullcontext
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import uuid

from agent_execution.controller import ExecutionController
from agent_execution.enforcement import ClaudeEnforcement
from agent_execution.native import NativeAdapter, NativeProfile
from agent_execution.policy import EffectivePolicy, ToolProfile
from agent_execution.tool_authority import AuthorityServer, ToolAuthority
from agent_execution.tests.fixtures import agent, task
from agent_execution.tests.test_native import NativeExecution

def main():
    fixture = NativeExecution('test_codex_consumes_full_context_and_normalizes_unknown_cost')
    fixture.setUp()
    controller = None
    server = None
    try:
        outside_secret = fixture.root / 'outside-secret'
        outside_write = fixture.root / 'outside-write'
        secrets = ['outside-' + uuid.uuid4().hex, 'inside-' + uuid.uuid4().hex]
        outside_secret.write_text(secrets[0])
        (fixture.worktree / '.env').write_text(secrets[1])
        marker = fixture.worktree / 'sandbox-marker'
        observations = {'requests': 0, 'tool_results': 0, 'leaked': False, 'commands_issued': False,
                        'tool_network': False, 'network_probe_ran': False, 'hook_requests': 0, 'hook_denials': 0,
                        'hook_tools': [], 'normal_read_succeeded': False}
        command = f"printf inside > sandbox-marker; cat '{outside_secret}'; cat .env; printf outside > '{outside_write}'"
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                observations['tool_network'] = True
                self.send_response(200); self.send_header('Content-Length', '0'); self.end_headers()
            def do_POST(self):
                size = int(self.headers.get('Content-Length', '0'))
                if size > 8 * 1024 * 1024 or self.headers.get('x-api-key') != 'fixture-only-not-credential':
                    self.send_error(403)
                    return
                data = json.loads(self.rfile.read(size))
                if self.path.endswith('/count_tokens'):
                    encoded = b'{"input_tokens":1}'
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(encoded)))
                    self.end_headers(); self.wfile.write(encoded)
                    return
                observations['requests'] += 1
                encoded_request = json.dumps(data)
                observations['leaked'] |= any(secret in encoded_request for secret in secrets)
                results = [block for message in data.get('messages', [])
                           for block in (message.get('content') if isinstance(message.get('content'), list) else [])
                           if block.get('type') == 'tool_result']
                observations['tool_results'] = max(observations['tool_results'], len(results))
                observations['normal_read_succeeded'] |= any(
                    item.get('tool_use_id') == 'tool_read_normal' and not item.get('is_error')
                    and 'fixture' in json.dumps(item.get('content')) for item in results)
                if not observations['commands_issued']:
                    observations['commands_issued'] = True
                    content = [
                        {'type': 'tool_use', 'id': 'tool_read_normal', 'name': 'Read', 'input': {'file_path': str(fixture.worktree / 'README')}},
                        {'type': 'tool_use', 'id': 'tool_read', 'name': 'Read', 'input': {'file_path': str(fixture.worktree / '.env')}},
                        {'type': 'tool_use', 'id': 'tool_bash', 'name': 'Bash', 'input': {'command': command}},
                        {'type': 'tool_use', 'id': 'tool_write', 'name': 'Write',
                         'input': {'file_path': str(outside_write), 'content': 'outside'}},
                    ]
                    stop = 'tool_use'
                else:
                    content = [{'type': 'text', 'text': 'Local policy fixture completed.'}]
                    stop = 'end_turn'
                message = {'id': 'msg_fixture_' + uuid.uuid4().hex, 'type': 'message', 'role': 'assistant',
                           'model': 'fixture', 'content': content, 'stop_reason': stop,
                           'stop_sequence': None, 'usage': {'input_tokens': 1, 'output_tokens': 1}}
                if not data.get('stream'):
                    encoded = json.dumps(message).encode()
                    self.send_response(200); self.send_header('Content-Type', 'application/json')
                else:
                    events = [('message_start', {'type': 'message_start', 'message': dict(message, content=[], stop_reason=None)})]
                    for index, block in enumerate(content):
                        initial = dict(block, input={}) if block['type'] == 'tool_use' else dict(block, text='')
                        events.append(('content_block_start', {'type': 'content_block_start', 'index': index, 'content_block': initial}))
                        delta = ({'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])}
                                 if block['type'] == 'tool_use' else {'type': 'text_delta', 'text': block['text']})
                        events.append(('content_block_delta', {'type': 'content_block_delta', 'index': index, 'delta': delta}))
                        events.append(('content_block_stop', {'type': 'content_block_stop', 'index': index}))
                    events.extend([('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': stop, 'stop_sequence': None},
                                                      'usage': {'output_tokens': 1}}), ('message_stop', {'type': 'message_stop'})])
                    encoded = ''.join('event: ' + name + '\ndata: ' + json.dumps(value) + '\n\n' for name, value in events).encode()
                    self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers(); self.wfile.write(encoded)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        if not Path('/usr/bin/python3').is_file():
            raise SystemExit('network probe requires the system Python executable')
        network_code = ('import socket; from pathlib import Path; Path("network-attempted").write_text("attempted"); '
                        f's=socket.create_connection(("127.0.0.1",{server.server_port}),timeout=1); '
                        's.sendall(b"GET /tool-network-probe HTTP/1.0\\r\\n\\r\\n"); s.recv(100)')
        import shlex
        command += '; ' + shlex.join(['/usr/bin/python3', '-c', network_code])
        dependency_prefix = Path(os.environ.get('NOMAD_SANDBOX_DEP_PREFIX', Path.home() / '.local/share/nomad-native-deps'))
        spec = replace(task(), repository=replace(task().repository, path=str(fixture.repo), base_commit=fixture.base),
                       execution_policy=replace(task().execution_policy, allowed_actions=('read', 'write', 'shell')),
                       budget=replace(task().budget, wall_seconds=20))
        native_agent = replace(agent(), supported_tools=('Read', 'Write', 'Edit', 'Bash'))
        tool_profile = ToolProfile(ref='tools:offline', policy_revision='p1', agent_ids=('local',), tools=native_agent.supported_tools,
                                   allowed_actions=('read', 'write', 'shell'), approval_actions=(), max_wall_seconds=20,
                                   cost_policy_refs=('budget:no-paid-calls',), local_route_verified=True)
        policy = EffectivePolicy(current_task=lambda _: spec, profiles=(tool_profile,), operators=('operator',), store=fixture.store)
        socket_for = lambda run: fixture.root / 'authority' / (run.attempt_id + '.sock')
        profile = NativeProfile(backend='local', agent=native_agent, command_prefix=(shutil.which('claude'),), settings_args=(),
                                policy_revision='p1', tool_profile_ref='tools:offline', environment={
                                    'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{server.server_port}',
                                    'ANTHROPIC_API_KEY': 'fixture-only-not-credential',
                                    'PATH': str(dependency_prefix / 'usr/bin') + ':' + os.environ['PATH'],
                                    'LD_LIBRARY_PATH': str(dependency_prefix / 'usr/lib/x86_64-linux-gnu'),
                                    'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1', 'DISABLE_AUTOUPDATER': '1'})
        adapter = NativeAdapter(profile, fixture.artifacts, fixture.store, fixture.root / 'native-canary', kill_grace=0.2,
                                 enforcement=ClaudeEnforcement(fixture.root / 'permissions', socket_for))
        class ProbeAuthority(ToolAuthority):
            def handle(self, request):
                observations['hook_requests'] += 1
                observations['hook_tools'].append(request.get('tool'))
                try:
                    return super().handle(request)
                except Exception:
                    observations['hook_denials'] += 1
                    raise
        authority_for = lambda task, agent, run: AuthorityServer(socket_for(run), ProbeAuthority(policy, task, agent, run, sandbox_verified=True))
        controller = ExecutionController(fixture.store, fixture.artifacts, {'local': adapter}, authorize=policy,
                                           resource_for=lambda *_: nullcontext(), tool_authority_for=authority_for)
        run = controller.start(spec, 'local', fixture.instructions, workspace=str(fixture.worktree))
        fixture.adapters.append((adapter, run))
        until = time.monotonic() + 25
        while time.monotonic() < until and adapter.recover(run).status != 'exited':
            time.sleep(0.05)
        state = adapter.recover(run)
        observations['network_probe_ran'] = (fixture.worktree / 'network-attempted').is_file()
        print(json.dumps({'state': state.status, 'observations': observations, 'inside_marker': marker.exists(),
                          'outside_write': outside_write.exists()}))
        if (state.status != 'exited' or observations['tool_results'] < 4 or not marker.exists()
                or outside_write.exists() or observations['leaked'] or observations['tool_network']
                or not observations['network_probe_ran'] or not observations['normal_read_succeeded']
                or 'Bash' not in observations['hook_tools']
                or observations['hook_denials'] < 1):
            _, _, output, errors = adapter._paths(run)
            # Show only non-secret startup diagnostics, never prompts or request bodies.
            print('native stderr:', errors.read_text()[-2000:])
            raise SystemExit('native enforcement canary did not pass; activation remains blocked')
        print('installed Claude/local harness: hook refusals + sandbox secret/read/write/network containment pass; fake provider only')
        subprocess.run([shutil.which('claude'), '--version'], check=True, timeout=10)
    finally:
        if controller is not None:
            controller.close()
        if server is not None:
            server.shutdown(); server.server_close()
        fixture.doCleanups()


if __name__ == "__main__":
    main()
