import sys
import os

import pytest

# Ensure the project root is on sys.path so `import extract_jobs` and
# `import app` work regardless of where pytest is invoked from.
sys.path.insert(0, os.path.dirname(__file__))


@pytest.fixture(autouse=True)
def _isolated_apply_queue(tmp_path, monkeypatch):
    """Point the Flask app's ApplyQueue at a temp DB so tests never touch
    the real application_agent/data/applications.sqlite3."""
    try:
        import app as app_module
        from application_agent.agent.apply_queue import ApplyQueue
    except ImportError:
        yield
        return
    monkeypatch.setattr(
        app_module, "apply_queue", ApplyQueue(tmp_path / "applications.sqlite3"), raising=False
    )
    yield
