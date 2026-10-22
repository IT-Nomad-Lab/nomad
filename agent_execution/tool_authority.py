"""Private per-attempt tool authority; model input never selects action classification.

Built-in tool decisions require a separately validated native OS sandbox. External effects
execute here through explicitly registered handlers, after scoped durable authorization.
The socket carries no ambient credentials and is never exposed on a network interface.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from fnmatch import fnmatchcase
import socket
import socketserver
import struct
import threading
import stat

from .contracts import Boundary, ContractError, Privacy
from .policy import action_scope


MAX_REQUEST = 64 * 1024
SOCKET_PATH_MAX = 107  # sun_path is 108 bytes with the NUL
PROTECTED = {'.git', '.ssh', '.aws', '.codex', '.claude', '.agents', 'AGENTS.md',
             'CLAUDE.md', 'Nomad.md', 'secret_scan.py', '.netrc', '.npmrc', '.pypirc',
             '.git-credentials', '.bash_history', 'auth.json', 'credentials.json'}
# Readable at launch, but a candidate may never change them.
CANDIDATE_PROTECTED = frozenset({'AUTONOMY.template.md'})


# Component patterns apply at every depth, including directories with protected names.
PROTECTED_PATTERNS = ('.env*', '*.pem', '*.key', 'id_rsa*', 'id_ed25519*', 'id_ecdsa*')


def is_protected(relpath, *, extra=()):
    """Shared name policy; callers retain their own containment and symlink checks."""
    parts = PurePosixPath(relpath).parts
    return not parts or any(
        part in PROTECTED or part in extra
        or any(fnmatchcase(part, pattern) for pattern in PROTECTED_PATTERNS)
        for part in parts)


@dataclass(frozen=True)
class ToolRoute:
    tool: str
    action: str
    target: object
    dispatch: object
    boundary: Boundary
    # Where the handler's result may be shown. Unclassified results are treated as local-only.
    result_privacy: Privacy = Privacy.LOCAL_ONLY

    def __post_init__(self):
        if not self.tool.startswith('mcp:') or not self.action or not callable(self.target) or not callable(self.dispatch):
            raise ContractError('external tool requires explicit classification, target and handler')
        if type(self.boundary) is not Boundary:
            raise ContractError('external tool requires an explicit verified privacy boundary')
        if type(self.result_privacy) is not Privacy:
            raise ContractError('external tool requires an explicit result privacy class')


class ToolAuthority:
    def __init__(self, policy, task, agent, run, *, routes=(), sandbox_verified=False):
        self.policy, self.task, self.agent, self.run = policy, task, agent, run
        self.routes = {route.tool: route for route in routes}
        if len(self.routes) != len(routes):
            raise ContractError('ambiguous external tool registry')
        self.sandbox_verified = sandbox_verified

    def _path(self, arguments):
        raw = arguments.get('file_path')
        if type(raw) is not str or not raw or '\x00' in raw:
            raise ContractError('file tool requires an explicit file path')
        root = Path(self.run.workspace)
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        try:
            parts = candidate.relative_to(root).parts
        except ValueError as exc:
            raise ContractError('file tool escapes the task worktree') from exc
        if is_protected(PurePosixPath(*parts)):
            raise ContractError('protected or secret path is not authorized')
        return str(candidate)

    def _patch_paths(self, arguments):
        """Every path a Codex apply_patch touches, each held to the Write/Edit rules.

        Strict on purpose: only the exact envelope and file headers are accepted, and any other
        line that could read as a header refuses, so Codex's own parser can never find a path
        this one did not check. Content lines always carry a ' ', '+' or '-' prefix."""
        if set(arguments) != {'command'} or type(arguments['command']) is not str:
            raise ContractError('apply_patch requires exactly one patch text')
        lines = arguments['command'].removesuffix('\n').split('\n')
        if len(lines) < 3 or lines[0] != '*** Begin Patch' or lines[-1] != '*** End Patch':
            raise ContractError('apply_patch envelope is not exact')
        paths, previous = [], ''
        for line in lines[1:-1]:
            if line.lstrip().startswith('***'):
                if line != line.strip():
                    # Codex trims header lines, so 'AGENTS.md ' would reach AGENTS.md.
                    raise ContractError('apply_patch header carries surrounding whitespace')
                for header in ('*** Add File: ', '*** Delete File: ', '*** Update File: ', '*** Move to: '):
                    if line.startswith(header):
                        if header == '*** Move to: ' and not previous.startswith('*** Update File: '):
                            raise ContractError('apply_patch move without its update header')
                        paths.append(self._path({'file_path': line[len(header):]}))
                        break
                else:
                    if line != '*** End of File':
                        raise ContractError('apply_patch header is not recognized')
            previous = line
        if not paths:
            raise ContractError('apply_patch names no file')
        return paths

    def handle(self, request):
        if type(request) is not dict or set(request) - {'attempt_id', 'generation', 'tool', 'arguments', 'approval_id'}:
            raise ContractError('invalid tool request fields')
        if (request.get('attempt_id'), request.get('generation')) != (self.run.attempt_id, self.run.claim.generation):
            raise ContractError('tool request belongs to another attempt/generation')
        if type(request.get('generation')) is not int:
            raise ContractError('invalid tool generation')
        tool, arguments = request.get('tool'), request.get('arguments')
        if type(tool) is not str or type(arguments) is not dict:
            raise ContractError('invalid tool request')
        approval = request.get('approval_id')
        if approval is not None and type(approval) is not str:
            raise ContractError('invalid approval identity')
        route = self.routes.get(tool)
        if route is not None:
            if self.task.execution_policy.privacy == Privacy.LOCAL_ONLY and route.boundary != Boundary.LOCAL:
                raise ContractError('local-only task cannot dispatch to a cloud tool')
            if self.agent.boundary != Boundary.LOCAL and route.result_privacy == Privacy.LOCAL_ONLY:
                raise ContractError('local-only tool result cannot be returned to a cloud agent')
            scope = action_scope(self.task, tool=tool, action=route.action,
                                 target=route.target(arguments), arguments=arguments)
            store, run = self.policy.store, self.run
            # Checked before the approval is consumed; begin_external_effect re-checks atomically.
            if any(effect['state'] == 'uncertain' for effect in store.unreconciled_effects(run.attempt_id)):
                raise ContractError('unreconciled uncertain external effect blocks further effects')
            self.policy.authorize_effect(self.task, self.agent, run, scope, approval_id=approval)
            effect = store.begin_external_effect(run.attempt_id, run.claim.owner_id, run.claim.generation,
                                                 tool=tool, scope_digest=scope.digest, approval_id=approval)
            # Never retry here. A handler exception may represent an uncertain remote effect;
            # the approval is already consumed, so operator reconciliation/new authority is required.
            try:
                result = route.dispatch(arguments)
            except BaseException:
                store.finish_external_effect(effect, certain=False)
                raise
            store.finish_external_effect(effect, certain=True)
            return {'result': result}
        if tool.startswith('mcp:'):
            raise ContractError('external tool is not registered')
        if not self.sandbox_verified:
            raise ContractError('native tool sandbox has not been validated')
        if tool in ('Read', 'Write', 'Edit'):
            target = self._path(arguments)
            action = 'read' if tool == 'Read' else 'write'
        elif tool == 'apply_patch':
            target, action = '\n'.join(self._patch_paths(arguments)), 'write'
        elif tool == 'Bash':
            if type(arguments.get('command')) is not str or not arguments['command'].strip():
                raise ContractError('shell requires an explicit command')
            if arguments.get('dangerouslyDisableSandbox') or arguments.get('run_in_background'):
                raise ContractError('unsandboxed or background shell execution refused')
            target, action = self.run.workspace, 'shell'
        else:
            raise ContractError('unknown native tool refused')
        scope = action_scope(self.task, tool=tool, action=action, target=target, arguments=arguments)
        self.policy.authorize_effect(self.task, self.agent, self.run, scope, approval_id=approval)
        return {'allow': True}


def runtime_base(candidates=None):
    """The user's private runtime directory: $XDG_RUNTIME_DIR, else /run/user/<uid>; the first
    candidate that is absolute, a directory, owned by the effective user and closed to others."""
    if candidates is None:
        candidates = (os.environ.get('XDG_RUNTIME_DIR', ''), f'/run/user/{os.geteuid()}')
    for candidate in candidates:
        if not candidate or not os.path.isabs(candidate):
            continue
        path = Path(candidate).resolve()
        try:
            info = path.stat()
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077:
            return path
    raise ContractError('no private runtime directory for tool authority sockets')


def authority_socket_path(root, attempt_id, runtime=None):
    """Where the attempt's authority socket binds: <runtime>/nomad-exec/<digest of root>/<attempt>.sock.

    Durable state stays under the execution root; only the socket moves, so its length no longer
    depends on where the root lives (about 84 bytes for a 36-character attempt id)."""
    base = Path(runtime).resolve() if runtime is not None else runtime_base()
    digest = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]
    path = base / 'nomad-exec' / digest / (attempt_id + '.sock')
    if len(str(path).encode()) > SOCKET_PATH_MAX:
        raise ContractError(f'tool authority socket path is {len(str(path).encode())} bytes, '
                            f'AF_UNIX allows {SOCKET_PATH_MAX}')
    return path


class AuthorityServer:
    def __init__(self, path, authority):
        self.path, self.authority = Path(path), authority
        self.server, self.thread = None, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise ContractError('tool authority directory must be a directory the operator owns')
        if self.path.parent.stat().st_mode & 0o077:
            raise ContractError('tool authority directory must be private')
        if self.path.parent.resolve() != self.path.parent:
            raise ContractError('tool authority directory must be canonical')
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                raise ContractError('unexpected tool authority path')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                try:
                    probe.connect(str(self.path))
                except ConnectionRefusedError:
                    self.path.unlink()
                else:
                    raise ContractError('tool authority socket is already active')
        authority = self.authority
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(5)
                try:
                    _, uid, _ = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != os.geteuid():
                        raise ContractError('foreign socket peer refused')
                    raw = self.rfile.readline(MAX_REQUEST + 1)
                    if len(raw) > MAX_REQUEST or not raw.endswith(b'\n'):
                        raise ContractError('tool request exceeds bounded frame')
                    response = authority.handle(json.loads(raw))
                except Exception as exc:
                    # Do not echo tool arguments, credentials or raw service exception text.
                    response = {'error': type(exc).__name__, 'allow': False}
                try:
                    self.wfile.write(json.dumps(response, allow_nan=False).encode() + b'\n')
                except OSError:
                    pass  # A disconnected peer does not authorize retrying a consumed effect.
        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
            block_on_close = False
        self.server = Server(str(self.path), Handler)
        os.chmod(self.path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
            self.path.unlink(missing_ok=True)


def request_authority(path, request):
    payload = json.dumps(request, allow_nan=False).encode() + b'\n'
    if len(payload) > MAX_REQUEST:
        raise ContractError('tool request exceeds bounded frame')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(path))
        connection.sendall(payload)
        with connection.makefile('rb') as stream:
            raw = stream.readline(MAX_REQUEST + 1)
    if len(raw) > MAX_REQUEST or not raw.endswith(b'\n'):
        raise ContractError('invalid authority response')
    result = json.loads(raw)
    if type(result) is not dict or 'error' in result or result.get('allow') is False:
        raise ContractError('tool authority refused dispatch')
    return result
