import pytest

import app as app_module


@pytest.fixture
def client(monkeypatch):
    """Flask test client with every outbound DataForSEO call blocked."""

    def blocked(*args, **kwargs):
        raise AssertionError("outbound DataForSEO call attempted in tests")

    monkeypatch.setattr(app_module, "_dfs_post", blocked)
    monkeypatch.setattr(app_module, "_dfs_get", blocked)
    monkeypatch.setattr(app_module, "dfs_post", blocked)
    monkeypatch.setattr(app_module, "dfs_get", blocked)
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture
def captured_post(monkeypatch):
    """Replace dfs_post with a recorder that returns an empty successful task."""
    calls = []

    def fake_post(endpoint, payload, login, password, timeout=None):
        calls.append({"endpoint": endpoint, "payload": payload})
        return {"cost": 0.0, "tasks": [{"status_code": 20000, "cost": 0.0, "result": []}]}

    monkeypatch.setattr(app_module, "dfs_post", fake_post)
    return calls
