"""Windows paths in hook payloads, translated to the WSL paths the hooks run against.

A Claude Code session on Windows sends hook payloads with Windows paths in them (`cwd`, file
paths, `cd` targets in commands). The hooks themselves run in WSL through bin/nomad-run, so every
such path goes through to_wsl() first. This is the one place that knows the mapping; bash
(nomad-run) passes paths through untouched.

  C:\\Users\\josia\\x  and  C:/Users/josia/x       ->  /mnt/c/Users/josia/x
  \\\\wsl.localhost\\Ubuntu-24.04\\home\\x (or wsl$)   ->  /home/x
  /c/Users/josia/x  (Git Bash form, Windows host only) ->  /mnt/c/Users/josia/x
  anything else                                    ->  unchanged

The drive mapping is what `wslpath -u` gives with the default automount root; NOMAD_WSL_MOUNT
overrides the root if /etc/wsl.conf moves it.
"""
import os
import re

DRIVE = re.compile(r"^([A-Za-z]):(?:[\\/]|$)")
UNC = re.compile(r"^[\\/]{2}(?:wsl\.localhost|wsl\$)[\\/][^\\/]+(?=[\\/]|$)", re.I)
MSYS = re.compile(r"^/([A-Za-z])(?=/|$)")


def on_windows():
    return os.environ.get("NOMAD_HOST") == "windows"


def to_wsl(path, windows=None):
    """The WSL path for `path`. Linux paths come back unchanged.

    `windows` says whether the payload came from a Windows session; it defaults to NOMAD_HOST.
    Only then is a Git Bash path like /c/Users read as a drive, because in WSL /c is just a
    directory."""
    if not isinstance(path, str) or not path:
        return path
    if windows is None:
        windows = on_windows()
    mount = os.environ.get("NOMAD_WSL_MOUNT", "/mnt").rstrip("/")
    m = UNC.match(path)
    if m:
        rest = path[m.end():].replace("\\", "/")
        return rest or "/"
    m = DRIVE.match(path)
    if m:
        rest = path[2:].replace("\\", "/").lstrip("/")
        return f"{mount}/{m.group(1).lower()}" + (f"/{rest}" if rest else "")
    if windows:
        m = MSYS.match(path)
        if m:
            return f"{mount}/{m.group(1).lower()}{path[2:]}"
    return path


if __name__ == "__main__":
    # bin/nomad-run's one call: `winpath.py <path>` prints the WSL path for a Windows session's.
    import sys
    print(to_wsl(sys.argv[1], windows=True) if len(sys.argv) > 1 else "")
