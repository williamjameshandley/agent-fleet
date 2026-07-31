from unittest import mock

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


def test_python_presentation_sends_exec_payload(capsys):
    python = {**descriptor(), "addr": "python-a@newton", "kind": "python"}
    with mock.patch.object(presentation.alan, "actors", return_value=[python]), \
         mock.patch("builtins.input", side_effect=["1 + 1", EOFError]), \
         mock.patch.object(presentation.loop, "send",
                           return_value={"input": "python-a@newton#1"}) as send, \
         mock.patch.object(presentation.alan, "wait_output",
                           return_value={"status": "ok", "value": "2"}):
        presentation.run("python-a@newton")

    send.assert_called_once_with(
        "python-a@newton", {"kind": "exec", "code": "1 + 1"})
