import os
import subprocess
from unittest import mock

from agent_fleet import quota


def result(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def test_provider_failure_does_not_prevent_other_provider_update(tmp_path):
    readers = [result(1, stderr="claude failed"), result(stdout="codex current\n")]
    with mock.patch.object(quota, "hosts", return_value=[os.uname().nodename]), \
         mock.patch.object(quota, "tmux", return_value=result()), \
         mock.patch.object(quota, "tmux_check") as tmux_check, \
         mock.patch.object(quota.subprocess, "run", side_effect=readers), \
         mock.patch.object(quota, "RUNTIME", tmp_path):
        quota.update()
    assert mock.call("set-option", "-g", "@fleet_claude_usage", "unavailable") in \
        tmux_check.call_args_list
    assert mock.call("set-option", "-g", "@fleet_codex_usage", "codex current") in \
        tmux_check.call_args_list
    assert (tmp_path / "quota.changed").exists()
