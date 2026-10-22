"""Controller-generated per-attempt Claude/local permission settings and Codex overrides.

Operator approved this scoped enforcement in #1461. Global settings are never edited.
Activation requires native version/sandbox compatibility checks; generated JSON alone
does not prove that an installed CLI supports or enforces these settings.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import threading

from .contracts import ContractError
from .mcp_gateway import SERVER as GATEWAY, routes_by_name
from .tool_authority import PROTECTED, PROTECTED_PATTERNS


def gateway_mcp_config(run, socket_path, effect_tools):
    """Strict MCP config: no server, or only the per-attempt gateway over the registered routes."""
    routes = tuple(effect_tools)
    if not routes:
        return {'mcpServers': {}}
    try:
        routes_by_name(routes)
    except ValueError as error:
        raise ContractError('effect route cannot be exposed through the gateway') from error
    script = str(Path(__file__).with_name('mcp_gateway.py').resolve())
    return {'mcpServers': {GATEWAY: {'type': 'stdio', 'command': sys.executable,
                                     'args': [script, str(socket_path), run.attempt_id, str(run.claim.generation), *routes],
                                     'env': {}}}}


def claude_settings(run, socket_path, effect_tools=()):
    workspace = Path(run.workspace)
    if workspace.resolve() != workspace:
        raise ContractError('enforcement workspace must be canonical')
    blocked = [str(workspace / name) for name in sorted(PROTECTED)]
    blocked += [str(workspace / '**' / name) for name in sorted(PROTECTED)]
    blocked += [str(workspace / '**' / pattern) for pattern in PROTECTED_PATTERNS]
    # The OS sandbox denies all ambient filesystem reads and reopens only runtime
    # binaries and this worktree. File tools additionally pass the authority hook.
    reads = ['/usr', '/bin', '/lib', '/lib64', '/etc/ld.so.cache', '/etc/alternatives',
             '/dev/null', '/dev/urandom', '/dev/random', str(workspace)]
    return {
        'autoMemoryEnabled': False,
        'permissions': {'defaultMode': 'dontAsk', 'blockReadsOutsideWorkingDirectories': True,
                        'allow': ['Read', 'Write', 'Edit', 'Bash', *([f'mcp__{GATEWAY}'] if effect_tools else [])],
                        'deny': ['Read(**/.env*)', 'Read(**/*.pem)', 'Read(**/*.key)']},
        'sandbox': {'enabled': True, 'failIfUnavailable': True, 'allowUnsandboxedCommands': False,
                    'autoAllowBashIfSandboxed': False,
                    'filesystem': {'denyRead': ['/', *blocked], 'allowRead': reads,
                                   'denyWrite': blocked},
                    'network': {'allowedDomains': [], 'allowLocalBinding': False}},
        'hooks': {'PreToolUse': [{'matcher': '.*', 'hooks': [{
            'type': 'command', 'command': sys.executable,
            'args': [str(Path(__file__).with_name('tool_hook.py').resolve()), str(socket_path),
                     run.attempt_id, str(run.claim.generation)],
            'timeout': 5, 'onFailure': 'block',
        }]}]},
    }


class ClaudeEnforcement:
    def __init__(self, runtime_root, socket_for, effect_tools_for=None):
        if not callable(socket_for) or (effect_tools_for is not None and not callable(effect_tools_for)):
            raise ContractError('explicit authority socket resolver required')
        self.runtime_root, self.socket_for = Path(runtime_root), socket_for
        self.effect_tools_for = effect_tools_for or (lambda run: ())

    def __call__(self, run):
        directory = self.runtime_root / hashlib.sha256(run.attempt_id.encode()).hexdigest()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.resolve() != directory or directory.stat().st_mode & 0o077:
            raise ContractError('permission settings directory must be private and canonical')
        path = directory / 'permissions.json'
        socket_path, effect_tools = self.socket_for(run), tuple(self.effect_tools_for(run))
        mcp = json.dumps(gateway_mcp_config(run, socket_path, effect_tools), sort_keys=True)
        data = json.dumps(claude_settings(run, socket_path, effect_tools), sort_keys=True).encode()
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            if (path.is_symlink() or path.stat().st_nlink != 1 or path.stat().st_mode & 0o077
                    or path.read_bytes() != data):
                raise ContractError('attempt permission settings changed; refuse reuse')
        else:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        return ('--restricted', '--setting-sources', '', '--settings', str(path),
                '--strict-mcp-config', '--mcp-config', mcp,
                '--permission-mode', 'dontAsk', '--tools', 'Read,Write,Edit,Bash',
                '--no-chrome', '--disable-slash-commands')


# Codex 0.163.0 findings (#1461, disposable CODEX_HOME + fake provider): hooks without a
# persisted trust hash are skipped silently, so a controller hook cannot be the containment
# layer for Codex. Containment comes from a per-attempt named permission profile instead,
# plus feature switches that remove tools which do not run inside that sandbox.
# write_stdin never reaches PreToolUse, so no process may hold an open stdin: unified_exec_tty
# off refuses tty sessions, and non-tty sessions start with stdin closed (write_stdin then fails
# "stdin is closed"). Codex still offers write_stdin, but only to poll output.
CODEX_DISABLED_FEATURES = ('apps', 'browser_use', 'computer_use', 'goals', 'image_generation',
                           'multi_agent', 'multi_agent_v2', 'plugins', 'remote_plugin',
                           'tool_suggest', 'view_image', 'memories', 'unified_exec_tty')
_CODEX_NAME = re.compile(r'[A-Za-z0-9_-]{1,64}')


def _toml_string(value):
    value = str(value)
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in value):
        raise ContractError('Codex override path contains control characters')
    return json.dumps(value)


def codex_mcp_servers(codex, environment, cwd, timeout=20):
    """Names of every MCP server Codex would load in cwd (project config included); refuses on any doubt.

    Enumeration and launch are separate processes: config edited in between is not covered."""
    try:
        result = subprocess.run([codex, 'mcp', 'list', '--json'], env=dict(environment), cwd=cwd, capture_output=True,
                                stdin=subprocess.DEVNULL, timeout=timeout)
        servers = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise ContractError('cannot enumerate Codex MCP servers') from error
    if not isinstance(servers, list) or not all(isinstance(item, dict) and isinstance(item.get('name'), str)
                                                for item in servers):
        raise ContractError('cannot enumerate Codex MCP servers')
    return tuple(item['name'] for item in servers)


# Codex loads AGENTS.md itself under the profile, and the worker is told to follow these files: each
# existing copy is readable, never writable. Codex 0.163.0 takes only 'deny' on a glob, and a deny glob
# beats an exact 'read', so they get exact entries. A copy that is not a plain single-link file stays
# denied, as in the verifier. New copies are not covered by any rule; finalization refuses them.
INSTRUCTION_FILES = ('AGENTS.md', 'CLAUDE.md', 'Nomad.md')


def _instruction_files(workspace, limit=100000):
    found, scanned = {}, 0
    for parent, folders, files in os.walk(workspace, followlinks=False):
        if '.git' in folders:
            folders.remove('.git')
        for name in [*folders, *files]:
            scanned += 1
            if scanned > limit:
                raise ContractError('workspace exceeds bounded instruction file scan')
            if name not in INSTRUCTION_FILES:
                continue
            path = Path(parent) / name
            info = path.lstat()
            found[str(path)] = 'read' if stat.S_ISREG(info.st_mode) and info.st_nlink == 1 else 'deny'
            if name in folders:
                folders.remove(name)
    return found


def codex_overrides(run, runtime_reads, mcp_servers):
    workspace = Path(run.workspace)
    if workspace.resolve() != workspace:
        raise ContractError('enforcement workspace must be canonical')
    reads = [Path(path) for path in runtime_reads]
    if any(not path.is_absolute() or path.resolve() != path for path in reads):
        raise ContractError('Codex runtime read roots must be canonical absolute paths')
    if any(path == workspace or workspace.is_relative_to(path) for path in reads):
        raise ContractError('Codex runtime read roots must not contain the workspace')
    names = tuple(mcp_servers)
    if any(not isinstance(name, str) or not _CODEX_NAME.fullmatch(name) for name in names):
        raise ContractError('Codex MCP server name cannot be disabled safely')
    profile = 'nomad-' + hashlib.sha256(run.attempt_id.encode()).hexdigest()[:20]
    filesystem = {':minimal': 'read', **{str(path): 'read' for path in reads}, str(workspace): 'write'}
    filesystem[str(workspace / '.git')] = 'read'
    # Only globs: a literal deny on a missing path makes bwrap fail to build the sandbox.
    # '**/' also matches at the workspace root (verified against the installed CLI).
    for name in sorted(PROTECTED - {'.git'} - set(INSTRUCTION_FILES)) + list(PROTECTED_PATTERNS):
        filesystem[str(workspace / '**' / name)] = 'deny'
    filesystem.update(_instruction_files(workspace))
    table = '{' + ', '.join(f'{_toml_string(key)}={_toml_string(value)}' for key, value in filesystem.items()) + '}'
    args = ['-c', f'default_permissions="{profile}"',
            '-c', f'permissions.{profile}.filesystem={table}',
            '-c', f'permissions.{profile}.network={{enabled=false}}',
            '-c', 'approval_policy="never"', '-c', 'web_search="disabled"']
    for feature in CODEX_DISABLED_FEATURES:
        args += ['-c', f'features.{feature}=false']
    for name in names:
        args += ['-c', f'mcp_servers.{name}.enabled=false']
    return tuple(args)


class CodexEnforcement:
    """Per-attempt Codex overrides only. NativeAdapter refuses to launch Codex with this alone:
    launch needs CodexTrustedEnforcement, which adds the verified trusted CODEX_HOME (#1486)."""

    def __init__(self, runtime_reads, mcp_servers):
        # mcp_servers(run) must enumerate from run.workspace, where project config applies.
        if not callable(mcp_servers):
            raise ContractError('explicit Codex MCP server enumerator required')
        self.runtime_reads, self.mcp_servers = tuple(runtime_reads), mcp_servers

    def __call__(self, run):
        return codex_overrides(run, self.runtime_reads, self.mcp_servers(run))


# Option B (operator decision 2026-10-21, #1461): a private per-attempt CODEX_HOME holding a
# containment hook that Codex has recorded as trusted. Codex 0.163.0 silently skips an untrusted
# or modified hook, so a hook is only a containment layer once its currentHash (reported by the
# app-server `hooks/list` method) is persisted as its trusted_hash. The private home sits outside
# every writable root, so the attempt cannot edit hooks.json or config.toml to defeat the hook.
# The helpers below are pure/offline; CodexTrustedEnforcement (#1486) drives the installed CLI to
# record and re-read trust at launch. probes/codex_policy.py holds the installed-CLI proof and
# its negative controls (tampered, extra, untrusted, absent).
def codex_hook_command(socket_path, attempt_id, generation):
    """The shell command string for the per-attempt Codex containment hook.

    tool_hook.py takes exactly socket/attempt/generation after its own path, so the hash Codex
    trusts is bound to this attempt and its claim generation."""
    if not isinstance(attempt_id, str) or not attempt_id:
        raise ContractError('Codex hook requires an attempt id')
    return shlex.join([sys.executable, str(Path(__file__).with_name('tool_hook.py').resolve()),
                       str(socket_path), attempt_id, str(int(generation))])


def codex_hooks_json(socket_path, attempt_id, generation):
    """hooks.json body: one PreToolUse command hook, our containment client, nothing else.

    Only PreToolUse is registered: with approval_policy="never" no PermissionRequest fires, and a
    single hook keeps the trust verification exact (any extra listed hook is then a refusal)."""
    hook = {'type': 'command', 'command': codex_hook_command(socket_path, attempt_id, generation), 'timeout': 5}
    return {'hooks': {'PreToolUse': [{'matcher': '.*', 'hooks': [hook]}]}}


def write_codex_trusted_home(directory, socket_path, attempt_id, generation):
    """Materialize the private CODEX_HOME (hooks.json only) in a fresh, private, canonical dir.

    config.toml (provider/model, and the trust records appended after `hooks/list`) is the
    caller's: enforcement does not bake in a provider or auth. Returns the home path."""
    home = Path(directory)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if home.resolve() != home or home.stat().st_mode & 0o077:
        raise ContractError('Codex trusted home must be private and canonical')
    path = home / 'hooks.json'
    data = json.dumps(codex_hooks_json(socket_path, attempt_id, generation), sort_keys=True).encode()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if (path.is_symlink() or path.stat().st_nlink != 1 or path.stat().st_mode & 0o077
                or path.read_bytes() != data):
            raise ContractError('Codex trusted home hooks.json changed; refuse reuse')
    else:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    return home


def codex_trust_toml(listed_hooks):
    """TOML trust records pinning exactly the hooks Codex listed, by their reported currentHash."""
    lines = []
    for hook in listed_hooks:
        key, current = hook.get('key'), hook.get('currentHash')
        if not isinstance(key, str) or not isinstance(current, str):
            raise ContractError('Codex hook listing lacks a key/currentHash to trust')
        lines.append(f'[hooks.state.{json.dumps(key)}]')
        lines.append(f'trusted_hash = {json.dumps(current)}')
    return '\n'.join(lines) + ('\n' if lines else '')


def verify_codex_hooks(listed_hooks, hooks_json_path, expected_count=1):
    """Fail-closed pre-launch check: Codex reports exactly our hooks, all trusted and user-owned.

    Refuses on any surprise: wrong count, a non-trusted/modified status, a managed or plugin hook,
    or a key that does not belong to our hooks.json. Returns True only when every hook is ours."""
    if not isinstance(listed_hooks, list) or len(listed_hooks) != expected_count:
        raise ContractError('Codex hook set does not match the attempt containment hook')
    prefix = str(Path(hooks_json_path)) + ':'
    for hook in listed_hooks:
        if not isinstance(hook, dict):
            raise ContractError('Codex hook listing is malformed')
        if hook.get('trustStatus') != 'trusted':
            raise ContractError('Codex containment hook is not trusted; refuse launch')
        if hook.get('source') != 'user' or hook.get('isManaged') or hook.get('pluginId'):
            raise ContractError('Codex hook is managed or plugin-provided; refuse launch')
        key = hook.get('key')
        if not isinstance(key, str) or not key.startswith(prefix):
            raise ContractError('Codex hook does not come from the attempt hooks.json; refuse launch')
    return True


def codex_hooks_list(codex, environment, cwd, timeout=30):
    """Codex's own view of the hooks it would load in cwd (app-server `hooks/list`, read-only).

    Refuses on any doubt: a launch failure, a timeout, an error reply or a malformed listing."""
    try:
        app = subprocess.Popen([codex, 'app-server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env=dict(environment), cwd=cwd, text=True)
    except OSError as error:
        raise ContractError('cannot list Codex hooks') from error
    timer = threading.Timer(timeout, app.kill)
    timer.start()
    try:
        for message in ({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'nomad', 'version': '1'}}},
                        {'method': 'initialized'}, {'id': 2, 'method': 'hooks/list', 'params': {'cwds': [str(cwd)]}}):
            app.stdin.write(json.dumps(dict(message, jsonrpc='2.0')) + '\n')
            app.stdin.flush()
        for line in app.stdout:
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            if isinstance(reply, dict) and reply.get('id') == 2:
                entries = reply['result']['data']
                hooks = [hook for entry in entries for hook in entry['hooks']]
                if not isinstance(entries, list) or not all(isinstance(hook, dict) for hook in hooks):
                    raise TypeError('malformed hook listing')
                return hooks
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ContractError('cannot list Codex hooks') from error
    finally:
        timer.cancel()
        app.kill()
        app.wait()
        for stream in (app.stdin, app.stdout):
            try:
                stream.close()
            except OSError:
                pass
    raise ContractError('cannot list Codex hooks')


def _write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


_AUTH_DIGEST = '.auth-source.sha256'


class CleanupOutcome(dict):
    """Structured cleanup evidence; truthiness retains the legacy sync-success API."""
    def __bool__(self):
        return self['auth'] == 'persisted'


class CodexTrustedEnforcement:
    """Option B launch path (#1486): a private per-attempt CODEX_HOME holding one containment
    hook that Codex itself reports as trusted, plus the per-attempt profile overrides.

    `__call__(run, environment)` builds the home, records trust for our hook only from Codex's own
    `hooks/list`, derives the overrides and, as its last step, re-checks hooks.json and re-lists
    and verifies the hook set. `environment(run)` then hands NativeAdapter the CODEX_HOME to launch
    with. Any failure removes the home and refuses. `release(run)` removes the home once the attempt
    has exited. Codex rotates refresh tokens, so a login refreshed inside the home makes the copied
    source stale; with `auth_writeback=True` (operator opt-in: it writes a credential file) release
    first writes the refreshed login back, and only over the exact bytes it copied.

    `base_config` is trusted controller TOML (provider/model); it must not carry hook trust."""

    def __init__(self, codex, runtime_root, runtime_reads, socket_for, *, base_config='', auth_source=None,
                 auth_writeback=False, timeout=30):
        if not isinstance(codex, str) or not Path(codex).is_absolute():
            raise ContractError('Codex enforcement requires an absolute CLI path')
        root = Path(runtime_root)
        if not root.is_absolute() or root.resolve() != root:
            raise ContractError('Codex trusted home root must be canonical')
        if not callable(socket_for):
            raise ContractError('explicit authority socket resolver required')
        if (not isinstance(base_config, str) or 'trusted_hash' in base_config
                or re.search(r'^\s*(\[+\s*)?["\']?hooks\b', base_config, re.MULTILINE)):
            raise ContractError('Codex base config must not carry hooks or hook trust')
        if auth_source is not None and not Path(auth_source).is_absolute():
            raise ContractError('Codex auth source must be an absolute path')
        self.codex, self.runtime_root, self.runtime_reads = codex, root, tuple(runtime_reads)
        self.socket_for, self.base_config, self.timeout = socket_for, base_config, timeout
        if auth_writeback and auth_source is None:
            raise ContractError('Codex auth write-back requires an auth source')
        self.auth_source = None if auth_source is None else Path(auth_source)
        self.auth_writeback, self.homes = auth_writeback is True, {}

    def _home(self, run):
        workspace = Path(run.workspace)
        if workspace.resolve() != workspace:
            raise ContractError('enforcement workspace must be canonical')
        home = self.runtime_root / hashlib.sha256(run.attempt_id.encode()).hexdigest()
        for root in (workspace, *(Path(path) for path in self.runtime_reads)):
            if home.is_relative_to(root) or root.is_relative_to(home):
                raise ContractError('Codex trusted home must lie outside every sandbox read and write root')
        return home

    def _copy_auth(self, home):
        if self.auth_source is None:
            return
        try:
            fd = os.open(self.auth_source, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as error:
            raise ContractError('Codex auth source is unreadable') from error
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ContractError('Codex auth source must be a regular file')
        with os.fdopen(fd, 'rb') as stream:
            data = stream.read()
        _write_private(home / 'auth.json', data)
        _write_private(home / _AUTH_DIGEST, hashlib.sha256(data).hexdigest().encode())

    def __call__(self, run, environment):
        home = self._home(run)
        self.runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.runtime_root.stat().st_mode & 0o077:
            raise ContractError('Codex trusted home root must be private')
        try:
            home.mkdir(mode=0o700)
        except FileExistsError:
            raise ContractError('Codex trusted home already exists; reconcile it first') from None
        home.with_suffix('.cleanup.json').unlink(missing_ok=True)
        try:
            socket_path = self.socket_for(run)
            hooks = write_codex_trusted_home(home, socket_path, run.attempt_id, run.claim.generation) / 'hooks.json'
            self._copy_auth(home)
            _write_private(home / 'config.toml', self.base_config.encode())
            listing = {**environment, 'CODEX_HOME': str(home)}
            # Trust only our own hook; anything else Codex lists stays untrusted and is refused below.
            ours = [hook for hook in codex_hooks_list(self.codex, listing, run.workspace, self.timeout)
                    if str(hook.get('key', '')).startswith(str(hooks) + ':')]
            with (home / 'config.toml').open('a') as stream:
                stream.write('\n' + codex_trust_toml(ours))
            for name in ('hooks.json', 'config.toml'):
                os.chmod(home / name, 0o400)
            args = codex_overrides(run, self.runtime_reads,
                                   codex_mcp_servers(self.codex, listing, run.workspace, self.timeout))
            # Last, immediately before launch: our hooks.json is unchanged and Codex reports exactly it, trusted.
            write_codex_trusted_home(home, socket_path, run.attempt_id, run.claim.generation)
            verify_codex_hooks(codex_hooks_list(self.codex, listing, run.workspace, self.timeout), hooks)
        except BaseException:
            shutil.rmtree(home, ignore_errors=True)
            raise
        self.homes[run.attempt_id] = home
        return args

    def environment(self, run):
        home = self.homes.get(run.attempt_id)
        if home is None:
            raise ContractError('Codex trusted home was not prepared for this attempt')
        return {'CODEX_HOME': str(home)}

    def _auth_outcome(self, home):
        """Whether Codex refreshed its login in the private home, and what became of it. Digests only.
        Writes Codex makes outside the workspace go to a per-command tmpfs that is gone by now, so
        they can't be reported here; the README says workspace-only persistence."""
        try:
            copied = (home / _AUTH_DIGEST).read_text().strip()
            current = hashlib.sha256((home / 'auth.json').read_bytes()).hexdigest()
        except OSError:
            return 'unchanged'
        if current == copied:
            return 'unchanged'
        if not self.auth_writeback:
            return 'changed_not_persisted'
        try:
            source = hashlib.sha256(self.auth_source.read_bytes()).hexdigest()
        except OSError:
            source = None
        if source != copied:
            return 'conflict'
        return 'persisted' if self._sync_auth(home) else 'changed_not_persisted'

    def _sync_auth(self, home):
        """Write a refreshed login back to the source, only over the exact bytes we copied."""
        try:
            copied = (home / _AUTH_DIGEST).read_text().strip()
            current = (home / 'auth.json').read_bytes()
            source = self.auth_source.read_bytes()
        except OSError:
            return False
        if hashlib.sha256(current).hexdigest() == copied or hashlib.sha256(source).hexdigest() != copied:
            return False
        try:
            if not isinstance(json.loads(current), dict):
                return False
        except ValueError:
            return False
        temporary = self.auth_source.with_name('.auth.json.' + os.urandom(8).hex())
        _write_private(temporary, current)
        os.replace(temporary, self.auth_source)
        return True

    def release(self, run):
        """Record the login outcome (digests only) before discarding the private home.

        The sidecar permits recovery even if the controller stops between cleanup and
        copying the outcome into the native attempt record.
        """
        self.homes.pop(run.attempt_id, None)
        home = self._home(run)
        record = home.with_suffix('.cleanup.json')
        if record.exists():
            outcome = CleanupOutcome(json.loads(record.read_text()))
            if home.exists():
                shutil.rmtree(home)
            return outcome
        if not home.exists():
            return CleanupOutcome(auth='unchanged')
        outcome = CleanupOutcome(auth=self._auth_outcome(home))
        temporary = record.with_name('.cleanup-' + os.urandom(16).hex())
        _write_private(temporary, json.dumps(dict(outcome), sort_keys=True).encode())
        os.replace(temporary, record)
        shutil.rmtree(home)
        return outcome
