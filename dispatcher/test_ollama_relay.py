"""ollama-relay's tests, collected here too so the dispatcher test run covers them (#383).

The tests live in ollama-relay/test_relay.py, beside the relay.
"""
import importlib.util
import os

_spec = importlib.util.spec_from_file_location("ollama_relay_tests", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ollama-relay", "test_relay.py"))
_tests = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tests)
globals().update({k: v for k, v in vars(_tests).items() if k.startswith("test_")})
