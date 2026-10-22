import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_execution.contracts import ContractError
from agent_execution.enforcement import (CODEX_DISABLED_FEATURES, CodexEnforcement, CodexTrustedEnforcement, codex_hook_command, codex_hooks_json,
                                         codex_mcp_servers, codex_overrides, codex_trust_toml, verify_codex_hooks,
                                         write_codex_trusted_home)


def overrides(args):
    assert all(flag == '-c' for flag in args[::2])
    return list(args[1::2])


class CodexOverrides(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: __import__('shutil').rmtree(self.root))
        self.workspace = self.root / 'ws'
        self.workspace.mkdir()
        self.run_a = SimpleNamespace(attempt_id='attempt-a', workspace=str(self.workspace))

    def test_profile_scopes_writes_to_workspace_and_denies_secrets_network_and_mcp(self):
        values = overrides(codex_overrides(self.run_a, ['/usr'], ['fixture_mcp', 'other-server']))
        profile = values[0].split('=', 1)[1].strip('"')
        self.assertTrue(profile.startswith('nomad-'))
        filesystem = next(value for value in values if value.startswith(f'permissions.{profile}.filesystem='))
        ws = str(self.workspace)
        self.assertIn(f'"{ws}"="write"', filesystem)
        self.assertIn(f'"{ws}/.git"="read"', filesystem)
        self.assertIn('":minimal"="read"', filesystem)
        self.assertIn('"/usr"="read"', filesystem)
        for denied in ('**/.codex', '**/.claude', '**/auth.json', '**/.env*', '**/*.pem', '**/*.key', '**/.ssh',
                       '**/id_rsa*', '**/id_ed25519*'):
            self.assertIn(f'"{ws}/{denied}"="deny"', filesystem)
        # A deny glob would beat the exact read rules below (installed CLI, 0.163.0).
        for instructions in ('AGENTS.md', 'CLAUDE.md', 'Nomad.md'):
            self.assertNotIn(f'"{ws}/**/{instructions}"', filesystem)
        self.assertNotIn('"write"', filesystem.replace(f'"{ws}"="write"', ''))
        self.assertIn(f'permissions.{profile}.network={{enabled=false}}', values)
        self.assertIn('approval_policy="never"', values)
        self.assertIn('web_search="disabled"', values)
        for feature in CODEX_DISABLED_FEATURES:
            self.assertIn(f'features.{feature}=false', values)
        # write_stdin bypasses PreToolUse: tty sessions (the only ones with open stdin) must be off.
        self.assertIn('features.unified_exec_tty=false', values)
        self.assertIn('mcp_servers.fixture_mcp.enabled=false', values)
        self.assertIn('mcp_servers.other-server.enabled=false', values)
        self.assertFalse(any('bypass' in value or 'danger' in value for value in values))

    def test_instruction_files_are_read_only_and_odd_copies_stay_denied(self):
        ws = self.workspace
        (ws / 'pkg' / 'CLAUDE.md').mkdir(parents=True)                  # a directory, not a file
        (ws / 'svc').mkdir()
        (ws / 'AGENTS.md').write_text('rules')
        (ws / 'svc' / 'Nomad.md').write_text('meta')
        (ws / 'svc' / 'AGENTS.md').symlink_to(ws / 'AGENTS.md')
        (ws / 'secret.txt').write_text('x')
        os.link(ws / 'secret.txt', ws / 'pkg' / 'AGENTS.md')            # a second link to another file
        (ws / '.git').mkdir()
        (ws / '.git' / 'AGENTS.md').write_text('not scanned')
        values = overrides(codex_overrides(self.run_a, [], []))
        table = next(value for value in values if '.filesystem=' in value)
        self.assertIn(f'"{ws}/AGENTS.md"="read"', table)
        self.assertIn(f'"{ws}/svc/Nomad.md"="read"', table)
        for odd in ('svc/AGENTS.md', 'pkg/AGENTS.md', 'pkg/CLAUDE.md'):
            self.assertIn(f'"{ws}/{odd}"="deny"', table)
        self.assertNotIn(f'"{ws}/.git/AGENTS.md"', table)

    def test_instruction_file_scan_is_bounded(self):
        for index in range(5):
            (self.workspace / f'f{index}').write_text('x')
        with patch('agent_execution.enforcement._instruction_files.__defaults__', (3,)), \
                self.assertRaises(ContractError):
            codex_overrides(self.run_a, [], [])

    def test_profile_name_is_unique_per_attempt_and_stable_on_retry(self):
        run_b = SimpleNamespace(attempt_id='attempt-b', workspace=str(self.workspace))
        first = codex_overrides(self.run_a, [], [])
        self.assertEqual(first, codex_overrides(self.run_a, [], []))
        self.assertNotEqual(first[1], codex_overrides(run_b, [], [])[1])

    def test_unsafe_inputs_are_refused(self):
        link = self.root / 'link'
        link.symlink_to(self.workspace)
        cases = [
            (SimpleNamespace(attempt_id='a', workspace=str(link)), [], []),
            (self.run_a, ['relative/path'], []),
            (self.run_a, [str(self.root)], []),
            (self.run_a, [str(self.workspace)], []),
            (self.run_a, [], ['bad.name']),
            (self.run_a, [], ['x" = {enabled=true}']),
            (self.run_a, [], [7]),
        ]
        for run, reads, servers in cases:
            with self.subTest(reads=reads, servers=servers), self.assertRaises(ContractError):
                codex_overrides(run, reads, servers)
        control = self.root / 'bad\nname'
        control.mkdir()
        with self.assertRaises(ContractError):
            codex_overrides(SimpleNamespace(attempt_id='a', workspace=str(control)), [], [])

    def test_enforcement_requires_an_enumerator_and_uses_it_per_attempt(self):
        with self.assertRaises(ContractError):
            CodexEnforcement([], ['fixture_mcp'])
        calls = []
        enforcement = CodexEnforcement(['/usr'], lambda run: calls.append(run.workspace) or ('late_server',))
        self.assertIn('mcp_servers.late_server.enabled=false', overrides(enforcement(self.run_a)))
        enforcement(self.run_a)
        self.assertEqual(calls, [str(self.workspace)] * 2)

    def fake_codex(self, body, code=0):
        path = self.root / 'codex'
        path.write_text(f'#!/bin/sh\ncat <<\'EOF\'\n{body}\nEOF\nexit {code}\n')
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def test_mcp_enumeration_fails_closed(self):
        ok = self.fake_codex(json.dumps([{'name': 'one', 'enabled': True}, {'name': 'two', 'enabled': False}]))
        self.assertEqual(codex_mcp_servers(ok, os.environ, self.root), ('one', 'two'))
        for body, code in (('[]', 1), ('not json', 0), ('{"name": "one"}', 0), ('[{"enabled": true}]', 0)):
            with self.subTest(body=body, code=code), self.assertRaises(ContractError):
                codex_mcp_servers(self.fake_codex(body, code), os.environ, self.root)
        with self.assertRaises(ContractError):
            codex_mcp_servers(str(self.root / 'missing'), os.environ, self.root)

    def test_mcp_enumeration_runs_in_the_attempt_workspace(self):
        # Project-scoped Codex config only applies when listing from the workspace.
        path = self.root / 'codex-cwd'
        path.write_text('#!/bin/sh\nprintf \'[{"name": "%s"}]\' "$(basename "$PWD")"\n')
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.assertEqual(codex_mcp_servers(str(path), os.environ, self.workspace), ('ws',))


class CodexTrustedHook(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: __import__('shutil').rmtree(self.root))
        self.home = self.root / 'home'
        self.hooks_path = self.home / 'hooks.json'

    def listed(self, **changes):
        hook = {'key': f'{self.hooks_path}:pre_tool_use:0:0', 'currentHash': 'sha256:abc',
                'trustStatus': 'trusted', 'source': 'user', 'isManaged': False, 'pluginId': None}
        hook.update(changes)
        return [hook]

    def test_hook_is_bound_to_attempt_and_generation(self):
        command = codex_hook_command('/run/a.sock', 'attempt-a', 3)
        self.assertTrue(command.endswith("tool_hook.py /run/a.sock attempt-a 3"))
        self.assertNotEqual(command, codex_hook_command('/run/a.sock', 'attempt-a', 4))
        hooks = codex_hooks_json('/run/a.sock', 'attempt-a', 3)['hooks']
        self.assertEqual(list(hooks), ['PreToolUse'])
        self.assertEqual(hooks['PreToolUse'][0]['hooks'][0]['command'], command)
        with self.assertRaises(ContractError):
            codex_hook_command('/run/a.sock', '', 3)

    def test_home_is_private_and_refuses_changed_reuse(self):
        home = write_codex_trusted_home(self.home, '/run/a.sock', 'attempt-a', 1)
        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.hooks_path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.hooks_path.read_text()), codex_hooks_json('/run/a.sock', 'attempt-a', 1))
        write_codex_trusted_home(self.home, '/run/a.sock', 'attempt-a', 1)  # identical retry is fine
        with self.assertRaises(ContractError):
            write_codex_trusted_home(self.home, '/run/a.sock', 'attempt-a', 2)
        self.hooks_path.write_text('{}')
        with self.assertRaises(ContractError):
            write_codex_trusted_home(self.home, '/run/a.sock', 'attempt-a', 1)

    def test_trust_records_pin_listed_hashes(self):
        text = codex_trust_toml(self.listed())
        self.assertIn(f'[hooks.state.{json.dumps(str(self.hooks_path) + ":pre_tool_use:0:0")}]', text)
        self.assertIn('trusted_hash = "sha256:abc"', text)
        with self.assertRaises(ContractError):
            codex_trust_toml([{'key': 'k'}])

    def test_verification_accepts_only_exactly_our_trusted_hook(self):
        self.assertTrue(verify_codex_hooks(self.listed(), self.hooks_path))
        refused = [[], self.listed() * 2, self.listed(trustStatus='modified'), self.listed(trustStatus='untrusted'),
                   self.listed(source='project'), self.listed(isManaged=True), self.listed(pluginId='p'),
                   self.listed(key=f'{self.root}/other/hooks.json:pre_tool_use:0:0'),
                   self.listed(key=f'{self.hooks_path}.evil:pre_tool_use:0:0'), [None]]
        for listed in refused:
            with self.subTest(listed=listed), self.assertRaises(ContractError):
                verify_codex_hooks(listed, self.hooks_path)


class CodexTrustedHome(unittest.TestCase):
    """Credential handling and fail-closed listing of CodexTrustedEnforcement, with the fake CLI."""

    def setUp(self):
        from agent_execution.tests import test_native
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: __import__('shutil').rmtree(self.root))
        self.workspace = self.root / 'ws'
        self.workspace.mkdir()
        self.cli = self.root / 'fake_codex'
        self.cli.write_text(f'#!{__import__("sys").executable}\n' + test_native.FAKE_CODEX)
        self.cli.chmod(0o700)
        self.source = self.root / 'source-auth.json'
        self.source.write_bytes(b'{"tokens": "fixture-original"}')
        self.run_a = SimpleNamespace(attempt_id='attempt-a', workspace=str(self.workspace),
                                     claim=SimpleNamespace(generation=1))
        self.environment = {'PATH': os.environ['PATH']}

    def enforcement(self, **options):
        return CodexTrustedEnforcement(str(self.cli), self.root / 'homes', ('/usr',), lambda run: '/run/a.sock',
                                       auth_source=str(self.source), **options)

    def test_auth_is_copied_private_and_never_written_back_by_default(self):
        enforcement = self.enforcement()
        enforcement(self.run_a, self.environment)
        home = Path(enforcement.environment(self.run_a)['CODEX_HOME'])
        self.assertEqual(stat.S_IMODE((home / 'auth.json').stat().st_mode), 0o600)
        self.assertEqual((home / 'auth.json').read_bytes(), self.source.read_bytes())
        (home / 'auth.json').write_bytes(b'{"tokens": "fixture-refreshed"}')
        self.assertFalse(enforcement.release(self.run_a))
        self.assertEqual(self.source.read_bytes(), b'{"tokens": "fixture-original"}')
        self.assertFalse(home.exists())
        with self.assertRaisesRegex(ContractError, 'not prepared'):
            enforcement.environment(self.run_a)

    def test_opt_in_write_back_replaces_only_the_exact_copied_source(self):
        enforcement = self.enforcement(auth_writeback=True)
        enforcement(self.run_a, self.environment)
        home = Path(enforcement.environment(self.run_a)['CODEX_HOME'])
        self.assertFalse(enforcement.release(self.run_a))  # unchanged login: nothing to write
        self.assertEqual(self.source.read_bytes(), b'{"tokens": "fixture-original"}')
        enforcement(self.run_a, self.environment)
        (home / 'auth.json').write_bytes(b'{"tokens": "fixture-refreshed"}')
        self.assertTrue(enforcement.release(self.run_a))
        self.assertEqual(self.source.read_bytes(), b'{"tokens": "fixture-refreshed"}')
        self.assertEqual(stat.S_IMODE(self.source.stat().st_mode), 0o600)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ['fake_codex', 'homes', 'source-auth.json', 'ws'])
        for refreshed in (b'{"tokens": "fixture-again"}', b'not json', b'[]'):
            with self.subTest(refreshed=refreshed):
                enforcement(self.run_a, self.environment)
                (home / 'auth.json').write_bytes(refreshed)
                self.source.write_bytes(b'{"tokens": "fixture-other-session"}')
                self.assertFalse(enforcement.release(self.run_a))
                self.assertEqual(self.source.read_bytes(), b'{"tokens": "fixture-other-session"}')
        enforcement(self.run_a, self.environment)
        (home / 'auth.json').write_bytes(b'not json')
        self.assertFalse(enforcement.release(self.run_a))
        self.assertEqual(self.source.read_bytes(), b'{"tokens": "fixture-other-session"}')

    def test_cleanup_outcomes_and_recovery_are_digest_only(self):
        for writeback, refresh, conflict, expected in (
            (False, False, False, 'unchanged'),
            (False, True, False, 'changed_not_persisted'),
            (True, True, False, 'persisted'),
            (True, True, True, 'conflict'),
        ):
            with self.subTest(expected=expected):
                self.source.write_text('{"fixture": "seed"}')
                enforcement = self.enforcement(auth_writeback=writeback)
                enforcement(self.run_a, self.environment)
                home = Path(enforcement.environment(self.run_a)['CODEX_HOME'])
                if refresh:
                    (home / 'auth.json').write_text('{"fixture": "rotated"}')
                if conflict:
                    self.source.write_text('{"fixture": "other"}')
                result = enforcement.release(self.run_a)
                self.assertEqual(dict(result), {'auth': expected})
                recovered = self.enforcement(auth_writeback=writeback).release(self.run_a)
                self.assertEqual(recovered, result)
                self.assertNotIn('fixture', json.dumps(result))
                self.assertEqual(self.source.read_text(), '{"fixture": "' + (
                    'other' if conflict else 'rotated' if writeback and refresh else 'seed') + '"}')

    def test_codex_own_state_in_its_home_is_not_reported(self):
        # Codex writes sessions, logs and history into CODEX_HOME on every run: not a finding.
        enforcement = self.enforcement()
        enforcement(self.run_a, self.environment)
        home = Path(enforcement.environment(self.run_a)['CODEX_HOME'])
        (home / 'sessions').mkdir()
        (home / 'sessions' / 'rollout.jsonl').write_text('{}')
        (home / 'history.jsonl').write_text('{}')
        (home / 'link').symlink_to(self.source)
        outcome = enforcement.release(self.run_a)
        self.assertEqual(dict(outcome), {'auth': 'unchanged'})
        self.assertFalse(home.exists())
        self.assertEqual(self.source.read_bytes(), b'{"tokens": "fixture-original"}')

    def test_auth_source_must_be_a_regular_file_not_a_link(self):
        link = self.root / 'link-auth.json'
        link.symlink_to(self.source)
        for source in (link, self.root / 'missing.json', self.root):
            with self.subTest(source=source):
                enforcement = CodexTrustedEnforcement(str(self.cli), self.root / 'homes', ('/usr',),
                                                      lambda run: '/run/a.sock', auth_source=str(source))
                with self.assertRaisesRegex(ContractError, 'auth source'):
                    enforcement(self.run_a, self.environment)
                self.assertEqual(list((self.root / 'homes').iterdir()), [])

    def test_configuration_is_validated(self):
        cases = (
            (dict(codex='codex'), 'absolute CLI path'),
            (dict(runtime_root='relative'), 'canonical'),
            (dict(socket_for=None), 'socket resolver'),
            (dict(base_config='[hooks.state."x"]\ntrusted_hash = "y"\n'), 'must not carry hooks'),
            (dict(base_config='[hooks]\n'), 'must not carry hooks'),
            (dict(base_config='[[hooks.PreToolUse]]\n'), 'must not carry hooks'),
            (dict(base_config='model = "x"\nhooks.state."k".trusted_hash = "y"\n'), 'must not carry hooks'),
            (dict(base_config='hooks = { PreToolUse = [] }\n'), 'must not carry hooks'),
            (dict(base_config="[ 'hooks' ]\n"), 'must not carry hooks'),
            (dict(base_config='[profiles.x]\ntrusted_hash = "y"\n'), 'must not carry hooks'),
            (dict(base_config=None), 'must not carry hooks'),
            (dict(auth_source='auth.json'), 'absolute path'),
            (dict(auth_writeback=True), 'requires an auth source'),
        )
        for changes, match in cases:
            with self.subTest(changes=changes):
                arguments = dict(codex=str(self.cli), runtime_root=self.root / 'homes', runtime_reads=('/usr',),
                                 socket_for=lambda run: 'sock')
                arguments.update(changes)
                with self.assertRaisesRegex(ContractError, match):
                    CodexTrustedEnforcement(**arguments)
        self.assertEqual(CodexTrustedEnforcement(str(self.cli), self.root / 'homes', (), lambda run: 's',
                                                 base_config='model = "x"\n[profiles.hooks_like]\n').homes, {})

    def test_home_root_must_be_private(self):
        (self.root / 'homes').mkdir(mode=0o755)
        (self.root / 'homes').chmod(0o755)
        with self.assertRaisesRegex(ContractError, 'must be private'):
            self.enforcement()(self.run_a, self.environment)
        self.assertEqual(list((self.root / 'homes').iterdir()), [])

    def test_listing_refuses_unusable_cli_output(self):
        from agent_execution.enforcement import codex_hooks_list
        scripts = {
            'missing': None,
            'silent': '#!/bin/sh\nexit 0\n',
            'garbage': '#!/bin/sh\nread a; echo not-json; echo \'{"id": 2, "result": {"data": [{"hooks": [1]}]}}\'\n',
            'shape': '#!/bin/sh\necho \'{"id": 2, "result": {"data": {"hooks": []}}}\'\n',
            'hang': '#!/bin/sh\nexec sleep 30\n',
        }
        for name, body in scripts.items():
            with self.subTest(name=name):
                path = self.root / name
                if body is not None:
                    path.write_text(body)
                    path.chmod(0o700)
                started = __import__('time').monotonic()
                with self.assertRaisesRegex(ContractError, 'cannot list Codex hooks'):
                    codex_hooks_list(str(path), self.environment, self.workspace, timeout=1)
                self.assertLess(__import__('time').monotonic() - started, 10)
        # A CLI that is gone before we write leaves unflushed requests; refusing must not leak a pipe error.
        real = __import__('subprocess').Popen

        def exited_first(*args, **kwargs):
            app = real(*args, **kwargs)
            app.wait()
            return app

        with patch('agent_execution.enforcement.subprocess.Popen', side_effect=exited_first), \
                self.assertRaisesRegex(ContractError, 'cannot list Codex hooks'):
            codex_hooks_list(str(self.root / 'silent'), self.environment, self.workspace, timeout=1)
        good = self.root / 'good'
        # Read the three requests first, as the real app-server does; exiting early breaks our pipe (#1500).
        good.write_text('#!/bin/sh\nhead -n 3 >/dev/null; echo \'{"id": 1}\'; echo \'{"id": 2, "result": {"data": [{"hooks": [{"key": "k"}]}, {"hooks": []}]}}\'\n')
        good.chmod(0o700)
        self.assertEqual(codex_hooks_list(str(good), self.environment, self.workspace), [{'key': 'k'}])

    def test_release_without_a_home_is_a_no_op(self):
        self.assertFalse(self.enforcement(auth_writeback=True).release(self.run_a))
        with self.assertRaisesRegex(ContractError, 'not prepared'):
            self.enforcement().environment(self.run_a)


if __name__ == '__main__':
    unittest.main()
