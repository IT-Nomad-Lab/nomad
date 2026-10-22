"""Actual installed Codex CLI with CodexEnforcement overrides against a fake loopback provider.

Disposable CODEX_HOME, fixture provider key, no operator config or auth, no paid model calls.
Part 1 proves the generated per-attempt overrides contain the CLI, including that no process
can receive write_stdin input (which PreToolUse never sees): tty sessions are refused and a live
non-tty process has stdin closed. Part 2 (Option B, #1461)
proves a trusted per-attempt containment hook (the real tool_hook.py client, a fake authority)
mediates exec_command, and that verify_codex_hooks refuses a tampered, extra, untrusted or
absent hook, each of which Codex would otherwise skip silently (fail open). Launch wiring
(CodexTrustedEnforcement, #1486) builds on both; probes/codex_launch.py runs one real attempt.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import socketserver
import subprocess
import tempfile
import threading
import time
import uuid
from types import SimpleNamespace

from agent_execution.codex import codex_runtime_reads as runtime_reads
from agent_execution.contracts import ContractError
from agent_execution.enforcement import (CodexEnforcement, codex_mcp_servers, codex_trust_toml, verify_codex_hooks,
                                         write_codex_trusted_home)

ALLOWED_TOOLS = {'exec_command', 'write_stdin', 'request_user_input'}
KEY = 'fixture-only-not-credential'


def main():
    codex = shutil.which('codex')
    if codex is None:
        raise SystemExit('installed Codex CLI not found')
    base = Path.home() / '.cache' / 'nomad-codex-probe'
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='probe-', dir=base)).resolve()
    server = None
    try:
        home, workspace = root / 'home', root / 'ws'
        home.mkdir(mode=0o700)
        workspace.mkdir()
        git = ['git', '-C', str(workspace), '-c', 'user.email=f@x', '-c', 'user.name=f']
        subprocess.run(['git', 'init', '-q', str(workspace)], check=True)
        (workspace / 'README').write_text('fixture\n')
        subprocess.run([*git, 'add', '.'], check=True)
        subprocess.run([*git, 'commit', '-qm', 'base'], check=True)
        secrets = ['outside-' + uuid.uuid4().hex, 'inside-' + uuid.uuid4().hex, 'nested-' + uuid.uuid4().hex]
        (root / 'outside-secret').write_text(secrets[0])
        (workspace / '.env').write_text(secrets[1])
        (workspace / 'sub').mkdir()
        (workspace / 'sub' / 'key.pem').write_text(secrets[2])
        secrets.append('ssh-' + uuid.uuid4().hex)
        (workspace / 'sub' / 'id_ed25519').write_text(secrets[3])
        mcp_marker = root / 'mcp-started'
        mcp_server = root / 'mcp_fixture.py'
        mcp_server.write_text(f'from pathlib import Path\nPath({str(mcp_marker)!r}).write_text("started")\n')
        outside_write = root / 'outside-write'
        hook_log = root / 'hook-calls'
        obs = {'requests': 0, 'tools': None, 'outputs': {}, 'answered': set(), 'leaked': False, 'tool_network': False,
               'typed': False}
        commands = ['printf inside > inside-marker', 'cat README', f"cat '{root / 'outside-secret'}'", 'cat .env',
                    'cat sub/key.pem', 'cat sub/id_ed25519', f"printf outside > '{outside_write}'", 'printf x > .git/probe-write',
                    'printf x > /tmp/nomad-codex-probe-' + root.name]

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def do_GET(self):
                obs['tool_network'] = True
                self.send_error(404)

            def do_POST(self):
                if 'tool-network' in self.path:  # before auth: the sandboxed tool has no provider key
                    obs['tool_network'] = True; self.send_error(404); return
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                if self.headers.get('Authorization') != 'Bearer ' + KEY:
                    self.send_error(403); return
                data = json.loads(body)
                obs['requests'] += 1
                obs['leaked'] |= any(secret in body.decode(errors='replace') for secret in secrets)
                if obs['tools'] is None:
                    obs['tools'] = sorted(tool.get('name') or tool.get('type') for tool in data.get('tools', []))
                outputs = [item for item in data.get('input', []) if item.get('type') == 'function_call_output']
                obs['outputs'] = {item.get('call_id'): str(item.get('output')) for item in outputs}
                obs['answered'] = set(obs['outputs'])
                network = (f'/usr/bin/python3 -c "import socket;s=socket.create_connection((\'127.0.0.1\','
                           f'{self.server.server_port}),timeout=1);s.sendall(b\'POST /tool-network HTTP/1.0\\r\\n\\r\\n\')"')
                session = re.search(r'session ID (\d+)', obs['outputs'].get('call_live', ''))
                if not outputs:
                    items = [{'type': 'function_call', 'id': f'fc_{n}', 'call_id': f'call_{n}', 'name': 'exec_command',
                              'arguments': json.dumps({'cmd': command})} for n, command in enumerate(commands + [network])]
                    # write_stdin is not seen by PreToolUse, so no process may ever have an open stdin:
                    # a tty session must be refused, and a live non-tty process must have stdin closed.
                    items += [{'type': 'function_call', 'id': 'fc_tty', 'call_id': 'call_tty', 'name': 'exec_command',
                               'arguments': json.dumps({'cmd': 'bash --noprofile --norc', 'tty': True, 'yield_time_ms': 500})},
                              {'type': 'function_call', 'id': 'fc_live', 'call_id': 'call_live', 'name': 'exec_command',
                               'arguments': json.dumps({'cmd': 'sleep 3; read line; eval "$line"', 'yield_time_ms': 500})}]
                elif session and not obs['typed']:
                    obs['typed'] = True
                    items = [{'type': 'function_call', 'id': 'fc_stdin', 'call_id': 'call_stdin', 'name': 'write_stdin',
                              'arguments': json.dumps({'session_id': int(session.group(1)),
                                                       'chars': 'printf typed > stdin-marker\n', 'yield_time_ms': 4000})}]
                else:
                    items = [{'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'status': 'completed',
                              'content': [{'type': 'output_text', 'text': 'done', 'annotations': []}]}]
                rid = 'resp_' + uuid.uuid4().hex
                events = [('response.created', {'type': 'response.created', 'response': {'id': rid}})]
                events += [('response.output_item.done', {'type': 'response.output_item.done', 'item': item}) for item in items]
                events.append(('response.completed', {'type': 'response.completed', 'response': {'id': rid, 'usage': {
                    'input_tokens': 1, 'input_tokens_details': {'cached_tokens': 0}, 'output_tokens': 1,
                    'output_tokens_details': {'reasoning_tokens': 0}, 'total_tokens': 2}}}))
                encoded = ''.join(f'event: {name}\ndata: {json.dumps(value)}\n\n' for name, value in events).encode()
                self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(encoded))); self.end_headers(); self.wfile.write(encoded)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        hook = root / 'record_hook.py'
        hook.write_text(f'import sys\nopen({str(hook_log)!r}, "a").write(sys.stdin.read() + "\\n")\n')
        (home / 'hooks.json').write_text(json.dumps({'hooks': {'PreToolUse': [{'matcher': '.*', 'hooks': [
            {'type': 'command', 'command': f"/usr/bin/python3 '{hook}'", 'timeout': 5}]}]}}))
        (home / 'config.toml').write_text(f'''model = "fixture"
model_provider = "fixture"

[model_providers.fixture]
name = "fixture"
base_url = "http://127.0.0.1:{server.server_port}/v1"
wire_api = "responses"
env_key = "FIXTURE_PROVIDER_KEY"

[mcp_servers.fixture_mcp]
command = "/usr/bin/python3"
args = ["{mcp_server}"]

[mcp_servers.second-fixture]
command = "/usr/bin/python3"
args = ["{mcp_server}"]
''')
        env = {key: os.environ[key] for key in ('PATH', 'LANG', 'USER') if key in os.environ}
        env.update(HOME=str(root), CODEX_HOME=str(home), FIXTURE_PROVIDER_KEY=KEY, TMPDIR=str(root))
        enforcement = CodexEnforcement(runtime_reads(codex), lambda run: codex_mcp_servers(codex, env, run.workspace))
        args = enforcement(SimpleNamespace(attempt_id='probe-' + uuid.uuid4().hex, workspace=str(workspace)))
        listed = json.loads(subprocess.run([codex, 'mcp', 'list', '--json', *args], env=env, capture_output=True,
                                           check=True, timeout=30).stdout)
        process = subprocess.run([codex, 'exec', '--json', '--ephemeral', *args, '-'], input=b'run the fixture',
                                 cwd=workspace, env=env, capture_output=True, timeout=120)
        time.sleep(4)  # let the live process reach its read even if Codex left it running
        outputs = '\n'.join(obs['outputs'].values())
        expected_calls = {f'call_{n}' for n in range(len(commands) + 1)} | {'call_tty', 'call_live', 'call_stdin'}
        result = {
            'exit': process.returncode, 'requests': obs['requests'], 'tools': obs['tools'],
            'mcp': {item['name']: item.get('enabled') for item in listed}, 'mcp_started': mcp_marker.exists(),
            'all_calls_answered': obs['answered'] == expected_calls,
            # Positive control: the network command ran and failed inside the tool, not before it.
            'network_attempted': 'Error' in obs['outputs'].get(f'call_{len(commands)}', ''),
            'tty_refused': 'TTY execution is disabled' in obs['outputs'].get('call_tty', ''),
            'stdin_closed': 'stdin is closed' in obs['outputs'].get('call_stdin', ''),
            'typed_ran': (workspace / 'stdin-marker').exists(),
            'inside_marker': (workspace / 'inside-marker').is_file(), 'normal_read': 'fixture' in outputs,
            'outside_write': outside_write.exists(), 'git_write': (workspace / '.git' / 'probe-write').exists(),
            'tmp_write': Path('/tmp/nomad-codex-probe-' + root.name).exists(),
            'leaked': obs['leaked'], 'tool_network': obs['tool_network'],
            'untrusted_hook_calls': hook_log.read_text().count('\n') if hook_log.exists() else 0,
        }
        print(json.dumps(result))
        if (process.returncode != 0 or result['requests'] < 2 or not set(result['tools'] or ['?']) <= ALLOWED_TOOLS
                or set(result['mcp']) != {'fixture_mcp', 'second-fixture'}
                or any(enabled is not False for enabled in result['mcp'].values()) or result['mcp_started']
                or not result['all_calls_answered'] or not result['network_attempted'] or not result['inside_marker']
                or not result['normal_read'] or result['outside_write'] or result['git_write'] or result['tmp_write']
                or result['leaked'] or result['tool_network'] or not result['tty_refused'] or not result['stdin_closed']
                or result['typed_ran']):
            print('codex stderr:', process.stderr.decode(errors='replace')[-2000:])
            raise SystemExit('codex enforcement probe did not pass; Codex activation remains refused')
        print('installed Codex: per-attempt profile contains file/secret/network/MCP effects and refuses '
              'write_stdin input (tty refused, stdin closed); fake provider only.')
        if result['untrusted_hook_calls'] == 0:
            print('part 1 recorder hook is untrusted and was skipped silently, as expected; trusted mediation is part 2')
        trusted_hook_probe(codex)
        subprocess.run([codex, '--version'], check=True, timeout=10)
    finally:
        if server is not None:
            server.shutdown(); server.server_close()
        Path('/tmp/nomad-codex-probe-' + root.name).unlink(missing_ok=True)
        shutil.rmtree(root, ignore_errors=True)


def hooks_list(codex, env, cwd):
    """Codex's own view of the hooks it would load in cwd (app-server stdio JSON-RPC, read-only)."""
    app = subprocess.Popen([codex, 'app-server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, env=env, cwd=cwd, text=True)
    timer = threading.Timer(30, app.kill)
    timer.start()
    try:
        for message in ({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'nomad-probe', 'version': '0'}}},
                        {'method': 'initialized'}, {'id': 2, 'method': 'hooks/list', 'params': {'cwds': [str(cwd)]}}):
            app.stdin.write(json.dumps(dict(message, jsonrpc='2.0')) + '\n')
            app.stdin.flush()
        for line in app.stdout:
            reply = json.loads(line)
            if reply.get('id') == 2:
                return [hook for entry in reply['result']['data'] for hook in entry['hooks']]
        raise SystemExit('codex app-server closed before answering hooks/list')
    finally:
        timer.cancel()
        app.kill()
        app.wait()


# Expected error items: the unbounded `**` deny-glob warning on Linux (the globs still deny at depth;
# capping them would fail open past the cap) and the fake model name having no metadata.
EXPECTED_ERRORS = ('glob_scan_max_depth', 'Model metadata for `fixture` not found')


def trusted_hook_run(codex, mode):
    """One Option B run. mode: trusted | tampered | extra | untrusted | absent."""
    base = Path.home() / '.cache' / 'nomad-codex-probe'
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=f'hook-{mode}-', dir=base)).resolve()
    provider = authority = None
    try:
        workspace = root / 'ws'
        workspace.mkdir()
        subprocess.run(['git', 'init', '-q', str(workspace)], check=True)
        socket_path, attempt = root / 'authority.sock', 'probe-' + uuid.uuid4().hex
        requests = []

        class Authority(socketserver.StreamRequestHandler):
            def handle(self):
                request = json.loads(self.rfile.readline())
                requests.append(request)
                command = (request.get('arguments') or {}).get('command', '')
                allow = (request.get('attempt_id') == attempt and request.get('tool') == 'Bash'
                         and isinstance(command, str) and command.startswith('allowed='))
                self.wfile.write(json.dumps({'allow': allow}).encode() + b'\n')

        authority = socketserver.ThreadingUnixStreamServer(str(socket_path), Authority)
        threading.Thread(target=authority.serve_forever, daemon=True).start()
        commands = ['allowed=1 printf ok > allowed-marker', 'allowed=1 env > allowed-env',
                    'printf denied > denied-marker']
        answered = set()

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
                outputs = [item for item in data.get('input', []) if item.get('type') == 'function_call_output']
                answered.update(item.get('call_id') for item in outputs)
                if outputs:
                    items = [{'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'status': 'completed',
                              'content': [{'type': 'output_text', 'text': 'done', 'annotations': []}]}]
                else:
                    items = [{'type': 'function_call', 'id': f'fc_{n}', 'call_id': f'call_{n}', 'name': 'exec_command',
                              'arguments': json.dumps({'cmd': command})} for n, command in enumerate(commands)]
                rid = 'resp_' + uuid.uuid4().hex
                events = [('response.created', {'type': 'response.created', 'response': {'id': rid}})]
                events += [('response.output_item.done', {'type': 'response.output_item.done', 'item': item}) for item in items]
                events.append(('response.completed', {'type': 'response.completed', 'response': {'id': rid, 'usage': {
                    'input_tokens': 1, 'input_tokens_details': {'cached_tokens': 0}, 'output_tokens': 1,
                    'output_tokens_details': {'reasoning_tokens': 0}, 'total_tokens': 2}}}))
                encoded = ''.join(f'event: {name}\ndata: {json.dumps(value)}\n\n' for name, value in events).encode()
                self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(encoded))); self.end_headers(); self.wfile.write(encoded)

        provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        threading.Thread(target=provider.serve_forever, daemon=True).start()
        home = write_codex_trusted_home(root / 'home', socket_path, attempt, 1)
        hooks_json = home / 'hooks.json'
        if mode == 'absent':
            hooks_json.unlink()
        config = home / 'config.toml'
        config.write_text(f'''model = "fixture"
model_provider = "fixture"

[model_providers.fixture]
name = "fixture"
base_url = "http://127.0.0.1:{provider.server_port}/v1"
wire_api = "responses"
env_key = "FIXTURE_PROVIDER_KEY"
''')
        env = {key: os.environ[key] for key in ('PATH', 'LANG', 'USER') if key in os.environ}
        env.update(HOME=str(root), CODEX_HOME=str(home), FIXTURE_PROVIDER_KEY=KEY, TMPDIR=str(root))
        if mode != 'untrusted':
            with config.open('a') as stream:
                stream.write('\n' + codex_trust_toml(hooks_list(codex, env, workspace)))
        if mode == 'tampered':  # changed after trust: Codex reports it modified and skips it
            hooks_json.write_text(hooks_json.read_text().replace('"timeout": 5', '"timeout": 6'))
        if mode == 'extra':  # a second hook nobody vetted, appended after trust
            data = json.loads(hooks_json.read_text())
            data['hooks']['PostToolUse'] = [{'matcher': '.*', 'hooks': [{'type': 'command', 'command': '/bin/true'}]}]
            hooks_json.write_text(json.dumps(data))
        listed = hooks_list(codex, env, workspace)
        try:
            verified = verify_codex_hooks(listed, hooks_json)
        except ContractError as error:
            verified = str(error)
        args = CodexEnforcement(runtime_reads(codex), lambda run: ())(SimpleNamespace(attempt_id=attempt, workspace=str(workspace)))
        process = subprocess.run([codex, 'exec', '--json', '--ephemeral', *args, '-'], input=b'run the fixture',
                                 cwd=workspace, env=env, capture_output=True, timeout=120)
        errors = []
        for line in process.stdout.decode(errors='replace').splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            item = event.get('item') if isinstance(event, dict) else None
            if event.get('type') == 'error' or (isinstance(item, dict) and item.get('type') == 'error'):
                errors.append(str((item or event).get('message')))
        allowed_env = (workspace / 'allowed-env').read_text() if (workspace / 'allowed-env').exists() else ''
        return {
            'mode': mode, 'exit': process.returncode, 'verified': verified,
            'trust': [hook.get('trustStatus') for hook in listed],
            'authority_requests': len(requests),
            'hook_input_shape': all(request.get('tool') == 'Bash' and isinstance(request.get('arguments'), dict)
                                    for request in requests),
            'all_calls_answered': answered == {f'call_{n}' for n in range(len(commands))},
            'allowed_ran': (workspace / 'allowed-marker').is_file(), 'denied_ran': (workspace / 'denied-marker').is_file(),
            'provider_key_in_allowed_env': KEY in allowed_env, 'codex_home_in_allowed_env': str(home) in allowed_env,
            'unexpected_errors': [error for error in errors if not any(text in error for text in EXPECTED_ERRORS)],
            'stderr_tail': process.stderr.decode(errors='replace')[-600:],
        }
    finally:
        for server in (provider, authority):
            if server is not None:
                server.shutdown(); server.server_close()
        shutil.rmtree(root, ignore_errors=True)


def trusted_hook_probe(codex):
    results = {mode: trusted_hook_run(codex, mode) for mode in ('trusted', 'tampered', 'extra', 'untrusted', 'absent')}
    print(json.dumps({mode: {key: value for key, value in result.items() if key != 'stderr_tail'}
                      for mode, result in results.items()}))
    good = results['trusted']
    failures = []
    if not (good['exit'] == 0 and good['verified'] is True and good['all_calls_answered'] and good['hook_input_shape']
            and good['authority_requests'] == 3 and good['allowed_ran'] and not good['denied_ran']
            and not good['provider_key_in_allowed_env'] and not good['codex_home_in_allowed_env']
            and not good['unexpected_errors']):
        failures.append('trusted hook did not mediate exec_command exactly')
    for mode in ('tampered', 'extra', 'untrusted', 'absent'):
        if results[mode]['verified'] is True:
            failures.append(f'{mode}: verify_codex_hooks accepted a hook set it must refuse')
    for mode in ('tampered', 'untrusted', 'absent'):
        # Positive control for the check itself: without it, Codex really does run the denied command.
        if not results[mode]['denied_ran'] or results[mode]['authority_requests']:
            failures.append(f'{mode}: expected Codex to skip the hook silently (fail open)')
    if failures:
        print('codex stderr (trusted):', good['stderr_tail'])
        raise SystemExit('codex trusted-hook probe did not pass; Codex activation remains refused: ' + '; '.join(failures))
    print('installed Codex: trusted per-attempt hook mediates exec_command; tampered/extra/untrusted/absent hooks are '
          'refused by verify_codex_hooks (Codex alone would skip them silently).')


if __name__ == '__main__':
    main()
