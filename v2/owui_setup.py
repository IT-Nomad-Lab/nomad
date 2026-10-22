"""+ New project from an Open WebUI chat: the setup steps after `approve PRD` (#1153).

The logic lives in nomad_projects/create.py (#1372), the one creation module every entry point
uses. This name stays so the engine and its tests keep importing it. In the engine image,
nomad_projects sits on PYTHONPATH (/app/lib); from a checkout, it is the repo root.
"""
import os
import sys

try:
    import nomad_projects.create  # noqa: F401
except ModuleNotFoundError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nomad_projects.create import *  # noqa: E402,F401,F403
