"""The governed Codex NativeAdapter (#1486), built the way autopilot/hybrid.py registers extra
adapters: `build_execution(adapters_extra={'codex': codex_adapter_factory(
policy_revision=POLICY_REVISION, tool_profile_ref=TOOL_PROFILE)})` calls
`make(agent, artifacts, store, root)` once per controller.

Each attempt gets a private trusted CODEX_HOME under `root/codex-homes`, its hook trust recorded
and re-verified by CodexTrustedEnforcement immediately before launch; the hook asks the same
per-attempt authority socket the controller serves at `authority_socket_path(root, attempt)`.
"""
from pathlib import Path
import shutil

from .contracts import ContractError
from .enforcement import CodexTrustedEnforcement
from .native import NativeAdapter, NativeProfile
from .tool_authority import authority_socket_path

LINUXBREW = Path('/home/linuxbrew/.linuxbrew')


def codex_runtime_reads(codex):
    """Read roots the installed CLI needs inside its sandbox: /usr and the CLI's install prefix."""
    binary = Path(codex).resolve()
    prefix = next((parent for parent in binary.parents if parent == LINUXBREW), binary.parent.parent)
    return ('/usr', str(prefix))


def codex_adapter_factory(*, policy_revision, tool_profile_ref, codex=None, auth_source=None,
                          auth_writeback=False, base_config='', kill_grace=5.0, runtime=None):
    """Return make(agent, artifacts, store, root) -> NativeAdapter for backend 'codex'.

    auth_source defaults to the operator's ~/.codex/auth.json (copied per attempt at 0600, never
    written back unless auth_writeback is set). policy_revision/tool_profile_ref must match the
    controller's ToolProfile. runtime overrides the runtime directory the authority socket binds
    under (see tool_authority.authority_socket_path); the controller must use the same."""
    codex = codex or shutil.which('codex')
    if codex is None:
        raise ContractError('installed Codex CLI not found')
    if auth_source is None:
        auth_source = str(Path.home() / '.codex' / 'auth.json')

    def make(agent, artifacts, store, root):
        root = Path(root)
        socket_for = lambda run: authority_socket_path(root, run.attempt_id, runtime)
        enforcement = CodexTrustedEnforcement(codex, root / 'codex-homes', codex_runtime_reads(codex), socket_for,
                                              base_config=base_config, auth_source=auth_source,
                                              auth_writeback=auth_writeback)
        profile = NativeProfile(backend='codex', agent=agent, command_prefix=(codex,), settings_args=(),
                                policy_revision=policy_revision, tool_profile_ref=tool_profile_ref,
                                environment={})
        native = root / 'native'
        native.mkdir(mode=0o700, parents=True, exist_ok=True)
        return NativeAdapter(profile, artifacts, store, native, kill_grace=kill_grace, enforcement=enforcement)

    return make
