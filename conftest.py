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


@pytest.fixture(autouse=True)
def _no_local_backend_for_agent(monkeypatch):
    """The application agent asks a running local backend first. Tests must never reach a
    real one, so it is disabled unless a test points the agent at a fake."""
    monkeypatch.setenv("APPLYPILOT_BACKEND_URL", "")
    try:
        from application_agent.agent import answer_generator
    except ImportError:
        yield
        return
    answer_generator.reset_backend_cache()
    yield
    answer_generator.reset_backend_cache()


@pytest.fixture(autouse=True)
def _isolated_backend_ledger(tmp_path, monkeypatch):
    """The backend's Track endpoint writes the one application ledger; point it (and the
    old store it imports from) at temp files so tests never touch the real ones."""
    server = sys.modules.get("autofill_extension.backend.server")
    if server is not None:
        monkeypatch.setattr(server, "LEDGER_PATH", tmp_path / "ledger.sqlite3", raising=False)
        monkeypatch.setattr(server, "DB_PATH", tmp_path / "legacy_applications.sqlite3", raising=False)
    yield
