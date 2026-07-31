from unittest import mock
from types import SimpleNamespace

from agent_fleet import presentation


def descriptor(state="waiting"):
    return {
        "addr": "codex-a@newton",
        "kind": "codex",
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


def test_python_console_uses_jupyter_existing_mode():
    console = mock.Mock()
    with mock.patch.object(presentation.PythonConsole, "instance", return_value=console):
        presentation.python_console("python-a@newton", "/native/kernel.json")
    assert console.actor == "python-a@newton"
    console.initialize.assert_called_once_with(["--existing", "/native/kernel.json"])
    console.start.assert_called_once_with()
