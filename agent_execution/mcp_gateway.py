"""Per-attempt stdio MCP gateway: the only MCP server a governed native agent may reach.

Every tools/call is forwarded to the attempt's private ToolAuthority socket, which applies the
exact-scope approval, privacy boundary and attempt liveness checks before running a registered
handler. The gateway holds no credentials and never retries: a lost or refused response is
reported to the agent as an error and needs reconciliation or new authority.

Invoked as an absolute standalone script so repository PYTHONPATH cannot replace imports.
The controller supplies socket, attempt, generation and route names as arguments, never the agent.
"""
import json
import re
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_execution.tool_authority import MAX_REQUEST, request_authority

SERVER = 'nomad_effects'
APPROVAL_FIELD = 'approval_id'
_PART = re.compile(r'[A-Za-z0-9-]+(?:_[A-Za-z0-9-]+)*')


def exposed_name(route):
    """'mcp:<server>:<tool>' -> '<server>__<tool>'; refuses names that would not map back uniquely."""
    parts = route.split(':') if isinstance(route, str) else []
    if len(parts) != 3 or parts[0] != 'mcp' or not all(_PART.fullmatch(part) for part in parts[1:]):
        raise ValueError('route cannot be exposed through the gateway')
    name = parts[1] + '__' + parts[2]
    if len(f'mcp__{SERVER}__{name}') > 64:
        raise ValueError('route name too long for native tool naming')
    return name


def routes_by_name(routes):
    mapping = {exposed_name(route): route for route in routes}
    if len(mapping) != len(routes):
        raise ValueError('ambiguous gateway route names')
    return mapping


def _tool(name, route):
    return {'name': name, 'description': f'Mediated {route}. Pass an operator approval_id when the effect needs one.',
            'inputSchema': {'type': 'object', 'properties': {APPROVAL_FIELD: {'type': 'string'}},
                            'additionalProperties': True}}


def _call(socket, attempt, generation, route, arguments):
    if type(arguments) is not dict:
        raise ValueError('tool arguments must be an object')
    arguments = dict(arguments)
    approval = arguments.pop(APPROVAL_FIELD, None)
    request = {'attempt_id': attempt, 'generation': generation, 'tool': route, 'arguments': arguments}
    if approval is not None:
        request['approval_id'] = approval
    response = request_authority(socket, request)
    if 'result' not in response:
        raise ValueError('authority returned no handler result')
    return response['result']


def respond(message, socket, attempt, generation, routes):
    """One JSON-RPC message in, the reply out (None for notifications)."""
    if type(message) is not dict or message.get('jsonrpc') != '2.0' or type(message.get('method')) is not str:
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'invalid request'}}
    method, ident = message['method'], message.get('id')
    if 'id' not in message:
        return None
    params = message.get('params')
    params = params if isinstance(params, dict) else {}
    if method == 'initialize':
        version = params.get('protocolVersion')
        result = {'protocolVersion': version if type(version) is str else '2025-06-18',
                  'capabilities': {'tools': {'listChanged': False}},
                  'serverInfo': {'name': SERVER, 'version': '1'}}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': [_tool(name, route) for name, route in sorted(routes.items())]}
    elif method == 'tools/call':
        route = routes.get(params.get('name'))
        try:
            if route is None:
                raise ValueError('unknown gateway tool')
            text, failed = json.dumps(_call(socket, attempt, generation, route, params.get('arguments', {})),
                                      allow_nan=False), False
        except Exception:
            # Do not echo arguments, approvals or handler exception text back into the model context.
            text, failed = 'refused or failed under task authority; not retried', True
        result = {'content': [{'type': 'text', 'text': text}], 'isError': failed}
    else:
        return {'jsonrpc': '2.0', 'id': ident, 'error': {'code': -32601, 'message': 'method not found'}}
    return {'jsonrpc': '2.0', 'id': ident, 'result': result}


def main():
    socket, attempt, generation, *names = sys.argv[1:]
    if not attempt:
        raise SystemExit('gateway requires an attempt identity')
    routes = routes_by_name(names)
    generation = int(generation)
    while True:
        raw = sys.stdin.buffer.readline(MAX_REQUEST + 1)
        if not raw:
            return 0
        if len(raw) > MAX_REQUEST or not raw.endswith(b'\n'):
            return 1  # An unbounded frame cannot be resynchronised safely.
        try:
            message = json.loads(raw)
        except ValueError:
            reply = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}}
        else:
            reply = respond(message, socket, attempt, generation, routes)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + '\n')
            sys.stdout.flush()


if __name__ == '__main__':
    sys.exit(main())
