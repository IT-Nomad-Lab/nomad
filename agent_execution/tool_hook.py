"""Claude/local PreToolUse client. Authority loss, malformed input and unknown tools deny.

Invoked as an absolute standalone script so repository PYTHONPATH cannot replace imports.
The private controller supplies socket/attempt identity as arguments, never tool input.
"""
import json
import shlex
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_execution.mcp_gateway import SERVER as GATEWAY
from agent_execution.tool_authority import request_authority


def main():
    verdict = 'deny'
    try:
        path, attempt, generation = sys.argv[1:]
        raw = sys.stdin.buffer.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ValueError('oversized hook input')
        data = json.loads(raw)
        tool = data['tool_name']
        # External effects must use the separately mediated MCP gateway; a native
        # connection to any other MCP server is never authorized by this hook. Gateway
        # calls are authorized per call by the authority behind the gateway, so asking it
        # here too would consume an approval before the effect runs.
        if tool.startswith(f'mcp__{GATEWAY}__'):
            print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'allow',
                                                     'permissionDecisionReason': 'mediated by the task gateway'}}))
            return 0
        if tool.startswith('mcp__'):
            raise ValueError('unmediated MCP tool refused')
        response = request_authority(path, {'attempt_id': attempt, 'generation': int(generation),
                                           'tool': tool, 'arguments': data['tool_input']})
        if response.get('allow') is True:
            verdict = 'allow'
    except Exception:
        pass
    output = {'hookEventName': 'PreToolUse', 'permissionDecision': verdict,
              'permissionDecisionReason': 'task-scoped controller authority'}
    if verdict == 'allow' and tool == 'Bash':
        # Drop harness/provider credentials from the tool environment. Filesystem and
        # network containment still come from the mandatory native sandbox.
        updated = dict(data['tool_input'])
        updated['command'] = shlex.join(['/usr/bin/env', '-i', 'PATH=/usr/bin:/bin',
                                         '/bin/bash', '--noprofile', '--norc', '-c', updated['command']])
        output['updatedInput'] = updated
    print(json.dumps({'hookSpecificOutput': output}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
