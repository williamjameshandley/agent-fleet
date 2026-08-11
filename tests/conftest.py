import os
import tempfile

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolated_tmux_servers():
    names = ("TMUX_TMPDIR", "TMUX", "TMUX_PANE")
    previous = {name: os.environ.get(name) for name in names}
    with tempfile.TemporaryDirectory() as directory:
        os.environ["TMUX_TMPDIR"] = directory
        os.environ.pop("TMUX", None)
        os.environ.pop("TMUX_PANE", None)
        yield
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
