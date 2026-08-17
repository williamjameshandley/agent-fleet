import os
from pathlib import Path
from unittest import mock
from types import SimpleNamespace
import networkx as nx

from agent_fleet import presentation


def descriptor():
    return {"kind": "antigravity", "cwd": "/home/will"}


class Observation:
    def __init__(self, details):
        self.graph = nx.MultiDiGraph()
        self.graph.graph["actors"] = [details]

    def __iter__(self):
        return self

    def __next__(self):
        graph, self.graph = self.graph, None
        if graph is None:
            raise StopIteration
        return graph

    def close(self):
        pass


def test_presentation_availability_is_derived_from_exact_native_evidence(tmp_path):
    codex = "codex-a@newton"
    claude = "claude-a@newton"
    python = "python-a@newton"
    llm = "llm-a@newton"
    names = [
        "fleet@alan-" + presentation.alan.runtime_name(codex),
        "fleet@alan-" + presentation.alan.runtime_name(claude),
    ]
    native = tmp_path / "native"
    native.mkdir()
    (native / "kernel.json").touch()
    with mock.patch.object(presentation.alan, "native_dir", return_value=native):
        assert presentation.available(codex, {"kind": "codex"}, names)
        assert presentation.available(claude, {"kind": "claude"}, names)
        assert not presentation.available(
            "codex-read-reviewer@newton", {"kind": "codex"}, names)
        assert presentation.available(
            python, {"kind": "python", "cwd": str(tmp_path)}, names)
        (native / "kernel.json").unlink()
        assert not presentation.available(
            python, {"kind": "python", "cwd": str(tmp_path)}, names)
        (native / "kernel.json").touch()
        assert presentation.available(
            llm, {"kind": "llm", "cwd": str(tmp_path)}, names)
        assert presentation.available(
            "antigravity-a@newton", {"kind": "antigravity", "cwd": str(tmp_path)},
            names)
        assert not presentation.available(
            "python-gone@newton",
            {"kind": "python", "cwd": str(tmp_path / "gone")}, names)
        assert not presentation.available(
            "llm-no-cwd@newton", {"kind": "llm", "cwd": ""}, names)
        assert not presentation.available(codex, {"kind": "codex"}, names * 2)
        assert not presentation.available("principal@newton", {
            "kind": "principal", "cwd": str(tmp_path),
        }, names)


def test_disappeared_native_presentation_immediately_removes_language_eligibility():
    actor = "codex-a@newton"
    descriptor = {"kind": "codex"}
    name = "fleet@alan-" + presentation.alan.runtime_name(actor)
    assert presentation.available(actor, descriptor, [name])
    assert not presentation.available(actor, descriptor, [])


def test_presentation_sends_input_and_renders_its_output(capsys):
    observations = mock.Mock()
    with mock.patch.object(presentation.loop, "observe",
                           return_value=observations) as observe, \
         mock.patch("builtins.input", side_effect=["inspect", EOFError]), \
         mock.patch.object(presentation.loop, "send",
                           return_value={"input": "antigravity-a@newton#1"}) as send, \
         mock.patch.object(presentation.alan, "wait_output",
                           return_value={"status": "ok", "value": "done"}) as wait:
        presentation.run("antigravity-a@newton", descriptor())

    observe.assert_called_once_with(stream=True, actor="antigravity-a@newton")
    wait.assert_called_once_with("antigravity-a@newton#1", observations)
    observations.close.assert_called_once_with()
    send.assert_called_once_with(
        "antigravity-a@newton", {"kind": "prompt", "text": "inspect"})
    assert capsys.readouterr().out == "done\n\n"


def test_interrupt_controls_the_active_actor_then_observes_its_output(capsys):
    observations = mock.Mock()
    with mock.patch.object(presentation.loop, "observe",
                           return_value=observations), \
         mock.patch("builtins.input", side_effect=["inspect", EOFError]), \
         mock.patch.object(presentation.loop, "send",
                           return_value={"input": "antigravity-a@newton#1"}), \
         mock.patch.object(presentation.alan, "wait_output",
                           side_effect=[KeyboardInterrupt, {
                               "status": "interrupted", "error": "interrupted",
                           }]) as wait, \
         mock.patch.object(presentation.loop, "control") as control:
        presentation.run("antigravity-a@newton", descriptor())

    control.assert_called_once_with("antigravity-a@newton", "interrupt")
    assert wait.call_args_list == [
        mock.call("antigravity-a@newton#1", observations),
        mock.call("antigravity-a@newton#1", observations),
    ]
    observations.close.assert_called_once_with()
    assert capsys.readouterr().out == "interrupted\n\n"


def test_run_owns_only_the_python_presentation():
    for kind in ("llm", "codex", "shell"):
        try:
            presentation.run("actor@newton", {"kind": kind})
        except SystemExit as error:
            assert "no Fleet-owned presentation" in str(error)
        else:
            raise AssertionError("Fleet ran a non-python presentation")


def test_python_console_uses_jupyter_existing_mode():
    console = mock.Mock()
    with mock.patch.object(presentation.PythonConsole, "instance", return_value=console):
        presentation.python_console("python-a@newton", "/native/kernel.json")
    assert console.actor == "python-a@newton"
    console.initialize.assert_called_once_with(["--existing", "/native/kernel.json"])
    console.start.assert_called_once_with()


def test_python_console_interrupts_only_while_jupyter_is_executing():
    console = presentation.PythonConsole()
    console.actor = "python-a@newton"
    console.shell = SimpleNamespace(_executing=True)
    with mock.patch.object(presentation.loop, "control") as control:
        console.handle_sigint()
    control.assert_called_once_with("python-a@newton", "interrupt")

    console.shell._executing = False
    with mock.patch.object(presentation.loop, "control") as control, \
         mock.patch.object(
             presentation.ZMQTerminalIPythonApp, "handle_sigint"
         ) as upstream:
        console.handle_sigint("signal", "frame")
    control.assert_not_called()
    upstream.assert_called_once_with("signal", "frame")


def test_actor_presentation_is_a_nested_tmux_session():
    actor = "llm-a@newton"
    details = {"kind": "llm", "cwd": "/work"}
    missing = __import__("subprocess").CompletedProcess([], 1)
    with mock.patch.object(presentation.alan, "runtime_name", return_value="hash"), \
         mock.patch.object(presentation.subprocess, "run",
                           side_effect=[missing, mock.DEFAULT, mock.DEFAULT,
                                        mock.DEFAULT]) as run, \
         mock.patch.object(presentation.os, "execvp") as execute:
        presentation.attach(actor, details)

    assert run.call_args_list == [
        mock.call(["/usr/bin/tmux", "-N", "has-session", "-t", "=fleet@alan-hash"],
                  stdout=presentation.subprocess.DEVNULL,
                  stderr=presentation.subprocess.DEVNULL),
        mock.call(["/usr/bin/tmux", "-N", "new-session", "-d", "-s", "fleet@alan-hash",
                   "-c", "/work", "/usr/bin/alan llm-a@newton"],
                  check=True),
        mock.call(["/usr/bin/tmux", "-N", "set-option", "-t", "fleet@alan-hash", "mouse", "on"],
                  check=True),
        mock.call(["/usr/bin/tmux", "-N", "set-option", "-t", "fleet@alan-hash", "status", "on"],
                  check=True),
    ]
    execute.assert_called_once_with(
        "/usr/bin/tmux", ["/usr/bin/tmux", "-N", "attach-session", "-t", "=fleet@alan-hash"])


def test_python_presentation_is_a_nested_jupyter_session():
    actor = "python-a@newton"
    details = {"kind": "python", "cwd": "/work"}
    missing = __import__("subprocess").CompletedProcess([], 1)
    with mock.patch.object(presentation.alan, "runtime_name", return_value="hash"), \
         mock.patch.object(presentation.subprocess, "run",
                           side_effect=[missing, mock.DEFAULT, mock.DEFAULT,
                                        mock.DEFAULT]) as run, \
         mock.patch.object(presentation.os, "execvp"):
        presentation.attach(actor, details)

    assert run.call_args_list[1] == mock.call(
        ["/usr/bin/tmux", "-N", "new-session", "-d", "-s", "fleet@alan-hash",
         "-c", "/work",
         "/usr/bin/python -c 'import json,sys; from agent_fleet.presentation import run; run(sys.argv[1], json.loads(sys.argv[2]))' python-a@newton '{\"kind\":\"python\",\"cwd\":\"/work\"}'"],
        check=True)


def test_existing_actor_presentation_is_reused():
    present = __import__("subprocess").CompletedProcess([], 0)
    with mock.patch.object(presentation.alan, "runtime_name", return_value="hash"), \
         mock.patch.object(presentation.subprocess, "run", return_value=present) as run, \
         mock.patch.object(presentation.os, "execvp") as execute:
        presentation.attach("python-a@newton", {"cwd": "/work"})
    assert run.call_args_list == [
        mock.call(["/usr/bin/tmux", "-N", "has-session", "-t", "=fleet@alan-hash"],
                  stdout=presentation.subprocess.DEVNULL,
                  stderr=presentation.subprocess.DEVNULL),
        mock.call(["/usr/bin/tmux", "-N", "set-option", "-t", "fleet@alan-hash", "status", "on"],
                  check=True),
    ]
    execute.assert_called_once_with(
        "/usr/bin/tmux", ["/usr/bin/tmux", "-N", "attach-session", "-t", "=fleet@alan-hash"])


def test_claude_attaches_only_to_its_existing_native_terminal():
    present = __import__("subprocess").CompletedProcess([], 0)
    with mock.patch.object(presentation.alan, "runtime_name", return_value="hash"), \
         mock.patch.object(presentation.subprocess, "run", return_value=present) as run, \
         mock.patch.object(presentation.os, "execvp") as execute:
        presentation.attach("claude-a@newton", {"kind": "claude", "cwd": "/work"})
    assert run.call_args_list == [
        mock.call(["/usr/bin/tmux", "-N", "has-session", "-t", "=fleet@alan-hash"],
                  stdout=presentation.subprocess.DEVNULL,
                  stderr=presentation.subprocess.DEVNULL),
        mock.call(["/usr/bin/tmux", "-N", "set-option", "-t", "fleet@alan-hash", "status", "on"],
                  check=True),
    ]
    execute.assert_called_once_with(
        "/usr/bin/tmux", ["/usr/bin/tmux", "-N", "attach-session", "-t", "=fleet@alan-hash"])


def test_claude_does_not_get_a_fleet_owned_fallback_terminal():
    missing = __import__("subprocess").CompletedProcess([], 1)
    with mock.patch.object(presentation.alan, "runtime_name", return_value="hash"), \
         mock.patch.object(presentation.subprocess, "run", return_value=missing) as run:
        try:
            presentation.attach(
                "claude-a@newton", {"kind": "claude", "cwd": "/work"})
        except RuntimeError as error:
            assert "evaluator terminal is unavailable" in str(error)
        else:
            raise AssertionError("Fleet created a second Claude presentation")
    run.assert_called_once()


def test_native_actor_does_not_get_a_fleet_owned_fallback_terminal():
    missing = __import__("subprocess").CompletedProcess([], 1)
    with mock.patch.object(presentation.alan, "runtime_name", return_value="hash"), \
         mock.patch.object(presentation.subprocess, "run", return_value=missing) as run:
        try:
            presentation.attach(
                "codex-a@newton", {"kind": "codex", "cwd": "/work"})
        except RuntimeError as error:
            assert "evaluator terminal is unavailable" in str(error)
        else:
            raise AssertionError("Fleet created a second Codex presentation")
    run.assert_called_once()


def test_close_rejects_non_bare_model_terminals():
    for actor in (
        "python-a@newton",
        "claude-a@newton",
        "codex-a@newton",
        "external-a@newton",
    ):
        with mock.patch.object(presentation.subprocess, "run") as run:
            try:
                presentation.close(actor)
            except RuntimeError as error:
                assert "conversational" in str(error)
            else:
                raise AssertionError("Fleet closed a non-conversational presentation")
        run.assert_not_called()


def test_close_kills_the_exact_bare_model_presentation():
    present = __import__("subprocess").CompletedProcess([], 0)
    with mock.patch.object(presentation.alan, "runtime_name", return_value="hash"), \
         mock.patch.object(presentation.subprocess, "run", return_value=present) as run:
        presentation.close("llm-a@newton")
    run.assert_called_once_with(
        ["/usr/bin/tmux", "-N", "kill-session", "-t", "=fleet@alan-hash"], text=True,
        stdout=presentation.subprocess.DEVNULL, stderr=presentation.subprocess.PIPE)
    with mock.patch.object(presentation.alan, "runtime_name", return_value="hash"), \
         mock.patch.object(presentation.subprocess, "run", return_value=present) as run:
        presentation.close("antigravity-a@newton")
    run.assert_called_once()


def test_close_accepts_an_absent_bare_model_presentation():
    absent = __import__("subprocess").CompletedProcess(
        [], 1, stderr="can't find session: fleet@alan-hash\n")
    with mock.patch.object(presentation.alan, "runtime_name", return_value="hash"), \
         mock.patch.object(presentation.subprocess, "run", return_value=absent) as run:
        presentation.close("llm-a@newton")
    run.assert_called_once()


def test_close_propagates_other_bare_model_tmux_failures():
    failure = __import__("subprocess").CompletedProcess(
        [], 1, stderr="no server running\n")
    with mock.patch.object(presentation.alan, "runtime_name", return_value="hash"), \
         mock.patch.object(presentation.subprocess, "run", return_value=failure):
        try:
            presentation.close("llm-a@newton")
        except __import__("subprocess").CalledProcessError:
            pass
        else:
            raise AssertionError("close hid a tmux failure")


def test_actor_view_dispatches_python_to_jupyter_console():
    actor = "python-a@newton"
    details = {"addr": actor, "kind": "python", "state": "waiting"}
    connection = Path("/state/actors") / actor / "native/kernel.json"
    with mock.patch.object(presentation.alan, "actors", return_value=[details]), \
         mock.patch.object(presentation.alan, "native_dir",
                           return_value=connection.parent), \
         mock.patch.object(presentation, "python_console") as console:
        presentation.run(actor, details)
    console.assert_called_once_with(actor, connection)
