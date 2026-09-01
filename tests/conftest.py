import pytest

from agent_fleet import actions, config, daemon, viewer


@pytest.fixture(autouse=True)
def configured_runtime_sources(monkeypatch):
    source = config.RuntimeSource(
        "lovelace", "will", "/home/will/.local/state/alan/loop.sock",
        "/tmp/tmux-1000/default")
    viewer_sources = [config.RuntimeSource(
        host, "will", "/home/will/.local/state/alan/loop.sock",
        "/tmp/tmux-1000/default")
        for host in ("lovelace", "turing", "newton", "noether", "boltzmann")]
    monkeypatch.setattr(actions, "runtime_sources", lambda: viewer_sources)
    monkeypatch.setattr(daemon, "runtime_sources", lambda: viewer_sources)
    monkeypatch.setattr(viewer, "runtime_sources", lambda: viewer_sources)
