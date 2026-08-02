import os
from pathlib import Path
from unittest import mock
from types import SimpleNamespace

from agent_fleet import presentation


def descriptor(state="waiting"):
    return {
        "addr": "codex-a@newton",
        "kind": "llm",
        "state": state,
    }


def test_presentation_sends_input_and_renders_its_output(capsys):
    with mock.patch.object(presentation.alan, "actors",
                           return_value=[descriptor()]), \
         mock.patch("builtins.input", side_effect=["inspect", EOFError]), \
         mock.patch.object(presentation.loop, "send",
                           return_value={"input": "codex-a@newton#1"}) as send, \
         mock.patch.object(presentation.alan, "wait_output",
                           return_value={"status": "ok", "value": "done"}):
        presentation.run("codex-a@newton")

    send.assert_called_once_with(
        "codex-a@newton", {"kind": "message", "text": "inspect"})
    assert capsys.readouterr().out == "done\n\n"


def test_interrupt_controls_the_active_actor_then_observes_its_output(capsys):
    with mock.patch.object(presentation.alan, "actors",
                           return_value=[descriptor()]), \
         mock.patch("builtins.input", side_effect=["inspect", EOFError]), \
         mock.patch.object(presentation.loop, "send",
                           return_value={"input": "codex-a@newton#1"}), \
         mock.patch.object(presentation.alan, "wait_output",
                           side_effect=[KeyboardInterrupt, {
                               "status": "interrupted", "error": "interrupted",
                           }]), \
         mock.patch.object(presentation.loop, "control") as control:
        presentation.run("codex-a@newton")

    control.assert_called_once_with("codex-a@newton", "interrupt")
    assert capsys.readouterr().out == "interrupted\n\n"


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
        mock.call(["tmux", "has-session", "-t", "=fleet@alan-hash"],
                  stdout=presentation.subprocess.DEVNULL,
                  stderr=presentation.subprocess.DEVNULL),
        mock.call(["tmux", "new-session", "-d", "-s", "fleet@alan-hash",
                   "-c", "/work",
                   "/usr/bin/python -c 'import sys; from agent_fleet.presentation import run; run(sys.argv[1])' llm-a@newton"],
                  check=True),
        mock.call(["tmux", "set-option", "-t", "fleet@alan-hash", "status", "off"],
                  check=True),
        mock.call(["tmux", "set-option", "-t", "fleet@alan-hash", "mouse", "on"],
                  check=True),
    ]
    execute.assert_called_once_with(
        "tmux", ["tmux", "attach-session", "-t", "=fleet@alan-hash"])


def test_existing_actor_presentation_is_reused():
    present = __import__("subprocess").CompletedProcess([], 0)
    with mock.patch.object(presentation.alan, "runtime_name", return_value="hash"), \
         mock.patch.object(presentation.subprocess, "run", return_value=present) as run, \
         mock.patch.object(presentation.os, "execvp") as execute:
        presentation.attach("python-a@newton", {"cwd": "/work"})
    run.assert_called_once()
    execute.assert_called_once_with(
        "tmux", ["tmux", "attach-session", "-t", "=fleet@alan-hash"])


def test_claude_attaches_only_to_its_existing_native_terminal():
    present = __import__("subprocess").CompletedProcess([], 0)
    with mock.patch.object(presentation.alan, "runtime_name", return_value="hash"), \
         mock.patch.object(presentation.subprocess, "run", return_value=present) as run, \
         mock.patch.object(presentation.os, "execvp") as execute:
        presentation.attach("claude-a@newton", {"kind": "claude", "cwd": "/work"})
    run.assert_called_once()
    execute.assert_called_once_with(
        "tmux", ["tmux", "attach-session", "-t", "=fleet@alan-hash"])


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
                assert "bare-model" in str(error)
            else:
                raise AssertionError("Fleet closed a non-bare-model presentation")
        run.assert_not_called()


def test_close_kills_the_exact_bare_model_presentation():
    present = __import__("subprocess").CompletedProcess([], 0)
    with mock.patch.object(presentation.alan, "runtime_name", return_value="hash"), \
         mock.patch.object(presentation.subprocess, "run", return_value=present) as run:
        presentation.close("llm-a@newton")
    run.assert_called_once_with(
        ["tmux", "kill-session", "-t", "=fleet@alan-hash"], text=True,
        stdout=presentation.subprocess.DEVNULL, stderr=presentation.subprocess.PIPE)


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


def test_refresh_leaves_codex_terminal_lifecycle_to_alan():
    actor = "codex-a@newton"
    details = {"addr": actor, "kind": "codex", "state": "waiting",
               "native": {"id": "thread-1"}}
    calls = []
    with mock.patch.object(presentation.alan, "actors", return_value=[details]), \
         mock.patch.object(presentation.alan, "retire",
                           side_effect=lambda value: calls.append(("retire", value))), \
         mock.patch.object(presentation.alan, "resume",
                           side_effect=lambda value: calls.append(("resume", value))):
        presentation.refresh(actor)
    assert calls == [("retire", actor), ("resume", actor)]


def test_refresh_leaves_claude_terminal_lifecycle_to_alan():
    actor = "claude-a@newton"
    details = {"addr": actor, "kind": "claude", "state": "waiting",
               "native": {"id": "session-1"}}
    calls = []
    with mock.patch.object(presentation.alan, "actors", return_value=[details]), \
         mock.patch.object(presentation.alan, "retire",
                           side_effect=lambda value: calls.append(("retire", value))), \
         mock.patch.object(presentation, "close",
                           side_effect=lambda value: calls.append(("close", value))), \
         mock.patch.object(presentation.alan, "resume",
                           side_effect=lambda value: calls.append(("resume", value))):
        presentation.refresh(actor)
    assert calls == [("retire", actor), ("resume", actor)]


def test_refresh_rejects_a_working_actor_before_lifecycle_changes():
    actor = "codex-a@newton"
    details = {"addr": actor, "kind": "codex", "state": "working",
               "native": {"id": "thread-1"}}
    with mock.patch.object(presentation.alan, "actors", return_value=[details]), \
         mock.patch.object(presentation.alan, "retire") as retire:
        try:
            presentation.refresh(actor)
        except RuntimeError as error:
            assert "waiting actor" in str(error)
        else:
            raise AssertionError("refresh accepted a working actor")
    retire.assert_not_called()


def test_actor_view_dispatches_python_to_jupyter_console():
    actor = "python-a@newton"
    details = {"addr": actor, "kind": "python", "state": "waiting"}
    connection = Path("/state/actors") / actor / "native/kernel.json"
    with mock.patch.object(presentation.alan, "actors", return_value=[details]), \
         mock.patch.object(presentation.alan, "native_dir",
                           return_value=connection.parent), \
         mock.patch.object(presentation, "python_console") as console:
        presentation.run(actor)
    console.assert_called_once_with(actor, connection)
