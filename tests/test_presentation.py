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
    actor = "codex-a@newton"
    details = {"cwd": "/work"}
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
                   "-c", "/work", "fleet actor-view codex-a@newton"], check=True),
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


def test_close_kills_the_exact_actor_presentation():
    present = __import__("subprocess").CompletedProcess([], 0)
    with mock.patch.object(presentation.alan, "runtime_name", return_value="hash"), \
         mock.patch.object(presentation.subprocess, "run", return_value=present) as run:
        presentation.close("codex-a@newton")
    run.assert_called_once_with(
        ["tmux", "kill-session", "-t", "=fleet@alan-hash"], text=True,
        stdout=presentation.subprocess.DEVNULL, stderr=presentation.subprocess.PIPE)


def test_close_accepts_an_absent_actor_presentation():
    absent = __import__("subprocess").CompletedProcess(
        [], 1, stderr="can't find session: fleet@alan-hash\n")
    with mock.patch.object(presentation.alan, "runtime_name", return_value="hash"), \
         mock.patch.object(presentation.subprocess, "run", return_value=absent) as run:
        presentation.close("codex-a@newton")
    run.assert_called_once()


def test_close_propagates_other_tmux_failures():
    failure = __import__("subprocess").CompletedProcess(
        [], 1, stderr="no server running\n")
    with mock.patch.object(presentation.alan, "runtime_name", return_value="hash"), \
         mock.patch.object(presentation.subprocess, "run", return_value=failure):
        try:
            presentation.close("codex-a@newton")
        except __import__("subprocess").CalledProcessError:
            pass
        else:
            raise AssertionError("close hid a tmux failure")


def test_refresh_retires_closes_and_resumes_the_same_waiting_actor():
    actor = "codex-a@newton"
    details = {"addr": actor, "kind": "codex", "state": "waiting",
               "native": {"id": "thread-1"}}
    calls = []
    with mock.patch.object(presentation.alan, "actors", return_value=[details]), \
         mock.patch.object(presentation.alan, "retire",
                           side_effect=lambda value: calls.append(("retire", value))), \
         mock.patch.object(presentation, "close",
                           side_effect=lambda value: calls.append(("close", value))), \
         mock.patch.object(presentation.alan, "resume",
                           side_effect=lambda value: calls.append(("resume", value))):
        presentation.refresh(actor)
    assert calls == [("retire", actor), ("close", actor), ("resume", actor)]


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


def test_actor_view_dispatches_codex_to_native_console():
    actor = "codex-a@newton"
    details = {"addr": actor, "kind": "codex", "state": "waiting"}
    with mock.patch.object(presentation.alan, "actors", return_value=[details]), \
         mock.patch.object(presentation, "codex_console") as console:
        presentation.run(actor)
    console.assert_called_once_with(actor, details)


def test_full_codex_console_resumes_the_actor_thread(tmp_path):
    actor = "codex-a@newton"
    native = tmp_path / "native"
    native.mkdir()
    (native / "thread_id").write_text("thread-1")
    details = {"cwd": "/work", "capabilities": "full"}
    socket = Path("/runtime/alan/codex/hash/codex.sock")

    with mock.patch.object(presentation.alan, "native_dir", return_value=native), \
         mock.patch.object(presentation.alan, "codex_socket", return_value=socket), \
         mock.patch.object(presentation.os, "chdir") as chdir, \
         mock.patch.object(presentation.os, "execvp") as execute:
        presentation.codex_console(actor, details)

    chdir.assert_called_once_with("/work")
    execute.assert_called_once_with(
        "codex",
        ["codex", "resume", "--remote", "unix:///runtime/alan/codex/hash/codex.sock",
         "thread-1", "--no-alt-screen"],
    )


def test_read_codex_console_enters_the_existing_actor_cage(tmp_path):
    actor = "codex-a@newton"
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    native = state / "actors" / actor / "native"
    native.mkdir(parents=True)
    (native / "thread_id").write_text("thread-1")
    details = {"cwd": "/work", "capabilities": "read"}

    with mock.patch.dict(os.environ, {
             "LOOP_STORE_DIR": str(state),
             "XDG_RUNTIME_DIR": str(runtime),
             "LOOP_CODEX_CAGE": "/cage",
             "LOOP_CAGE_RUNTIME_DIR": "/configured-cages",
         }), mock.patch.object(presentation.os, "chdir") as chdir, \
         mock.patch.object(presentation.os, "execve") as execute:
        presentation.codex_console(actor, details)

    name = presentation.alan.runtime_name(actor)
    socket = runtime / "alan" / "codex" / name / "codex.sock"
    chdir.assert_called_once_with("/work")
    argv, environment = execute.call_args.args[1:]
    assert argv == [
        "/cage", "--client", actor, str(native), str(socket.parent),
        "/configured-cages/" + name + ".sock", str(socket), "thread-1",
    ]
    assert environment["LOOP_SOCKET"] == str(state / "actors" / (name + ".sock"))
    assert environment["LOOP_CAPABILITIES"] == '"read"'
    assert environment["LOOP_CWD"] == "/work"
