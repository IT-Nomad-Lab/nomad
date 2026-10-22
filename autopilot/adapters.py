"""Native worker execution seam, initially preserving the existing Claude invocation.

The controller keeps workspaces, generated policy/settings, grants and integration authority.
Adapters own native commands, streams and process handling. This low-level seam is separate
from the portable AgentAdapter protocol until task/context/attempt integration in #1457/#1458.
"""
import subprocess
from typing import Protocol


class WorkerAdapter(Protocol):
    name: str

    def command(self, executable, prompt, settings_args, cap_args): ...
    def launch(self, command, *, cwd, stdout, stderr, env): ...
    def parse(self, path): ...
    def terminate(self, proc): ...
    def adopt(self, pid, start_time): ...


class ClaudeAdapter:
    name = "claude"

    def __init__(self, *, parse_stream, kill_group, adopt, popen=None):
        self.parse_stream = parse_stream
        self.kill_group = kill_group
        self.adopt_process = adopt
        self.popen = popen or subprocess.Popen

    def command(self, executable, prompt, settings_args, cap_args):
        return [executable, "-p", prompt, *settings_args, *cap_args,
                "--verbose", "--output-format", "stream-json"]

    def launch(self, command, *, cwd, stdout, stderr, env):
        return self.popen(command, cwd=cwd, stdout=stdout, stderr=stderr,
                          stdin=subprocess.DEVNULL, start_new_session=True, env=env)

    def parse(self, path):
        return self.parse_stream(path)

    def terminate(self, proc):
        return self.kill_group(proc)

    def adopt(self, pid, start_time):
        return self.adopt_process(pid, start_time)
