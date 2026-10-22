"""Regression: the existing local bridge path must not drop supplied handoff context."""
import os
from pathlib import Path
import sys
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault('AGENT_BRIDGE_MCP_SYNC', 'off')

import backends
import runner
from store import Store


def test_local_native_session_still_receives_supplied_handoff():
    store = Store(':memory:')
    store.set_session('fixture', 'local', 'native-session', '/tmp/fixture')
    backend = backends.Backends('/bin/true')
    backend.mcp_sync = Mock(return_value='')
    backend.mcp_config = Mock(return_value='')
    backend.env = Mock(return_value={})
    manager = runner.RunManager(store, claude='/bin/true', be=backend)
    handoff = 'Required checkpoint: preserve decisions and known failures.\n'
    with patch.object(runner.Run, 'start') as start:
        manager.start('fixture', '/tmp/fixture', 'fixture', 'Continue task', backend='local', handoff=handoff)
    assert start.call_args.args[2] == handoff + 'Continue task'
    assert '--resume' in start.call_args.args[0]
