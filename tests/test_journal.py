from pathlib import Path
import asyncio
import socket
import subprocess
import threading
import time
from unittest import mock

import pytest

from agent_fleet import daemon, journal, viewer


def test_event_supplies_fixed_native_fields(monkeypatch):
    sent = []
    monkeypatch.setattr(journal, "_send", lambda **fields: sent.append(fields))

    assert journal.record(
        "projection_completed", slot="main", host="newton", source="source-key",
        path="cross_host", selection_ack_seconds=.01, transport_reply_seconds=.002,
        revalidate_switch_seconds=.001)

    assert sent == [{
        "MESSAGE": "Fleet projection completed",
        "PRIORITY": journal.INFO,
        "SYSLOG_IDENTIFIER": "agent-fleet",
        "FLEET_COMPONENT": "viewer",
        "FLEET_EVENT": "projection_completed",
        "FLEET_SLOT": "main",
        "FLEET_HOST": "newton",
        "FLEET_SOURCE": "source-key",
        "FLEET_PATH": "cross_host",
        "FLEET_SELECTION_ACK_SECONDS": "0.01",
        "FLEET_TRANSPORT_REPLY_SECONDS": "0.002",
        "FLEET_REVALIDATE_SWITCH_SECONDS": "0.001",
    }]


def test_schema_rejects_unknown_events_fields_values_and_content_tokens(monkeypatch):
    monkeypatch.setattr(journal, "_send", lambda **fields: None)
    with pytest.raises(ValueError, match="unknown"):
        journal.record("other")
    with pytest.raises(ValueError, match="invalid fields"):
        journal.record("viewer_ready", slot="main", tty="/dev/pts/8", prompt="secret")
    with pytest.raises(TypeError, match="invalid journal value"):
        journal.record("viewer_ready", slot="main", tty=["/dev/pts/8"])
    with pytest.raises(ValueError, match="invalid journal value"):
        journal.record(
            "viewer_operation_failed", slot="main", operation="transcript",
            host="newton", source="source", stage="switch", cause="client",
            error_type="RuntimeError")
    with pytest.raises(ValueError, match="invalid journal value"):
        journal.record(
            "viewer_operation_failed", slot="main", operation="PROJECT",
            host="newton", source="source", stage="prompt", cause="secret",
            error_type="RuntimeError")


@pytest.mark.parametrize("field", journal.VALUES)
def test_every_controlled_field_rejects_an_unknown_token(field):
    with pytest.raises(ValueError, match="invalid journal value"):
        journal._value(field, "secret")


def test_transport_failure_cannot_change_the_callers_result(monkeypatch):
    def fail(**fields):
        raise OSError("journal unavailable")

    monkeypatch.setattr(journal, "_send", fail)
    assert not journal.record("viewer_ready", slot="main", tty="/dev/pts/8")


def test_service_declares_its_journal_streams_and_identifier():
    service = (Path(__file__).parents[1] / "fleet.service").read_text()
    assert "StandardOutput=journal" in service
    assert "StandardError=journal" in service
    assert "SyslogIdentifier=agent-fleet-daemon" in service


def test_collector_diagnostics_preserve_stdout_for_protocol():
    source = (Path(__file__).parents[1] / "agent_fleet/daemon.py").read_text()
    assert 'print(f"{host}: {errors[-1]}", file=sys.stderr, flush=True)' in source


def test_host_events_follow_availability_transitions(monkeypatch):
    fleet = daemon.Fleet()
    host = "newton"
    fleet.processes[host] = mock.Mock(pid=42)
    graph = mock.Mock()
    records = []
    monkeypatch.setattr(daemon, "decode_observation",
                        lambda raw: ([], {}, set(), graph))
    monkeypatch.setattr(daemon.journal, "record",
                        lambda event, **fields: records.append((event, fields)))

    fleet.update_host(host, b"observation")
    fleet.update_host(host, b"observation")
    asyncio.run(fleet.host_disconnected(host, 42, 1))
    asyncio.run(fleet.host_disconnected(host, 43, 2))

    assert records == [
        ("host_connected", {"host": host, "pid": 42}),
        ("host_disconnected", {"host": host, "pid": 42, "status": 1}),
    ]


def test_refresh_task_failure_is_retrieved_and_recorded_once(monkeypatch):
    records = []
    monkeypatch.setattr(daemon.journal, "record",
                        lambda event, **fields: records.append((event, fields)))

    async def exercise():
        fleet = daemon.Fleet()
        fleet.refresh_muster = mock.AsyncMock(
            side_effect=RuntimeError("content must not enter the event"))
        fleet.schedule_refresh()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not fleet.background_tasks
        assert not fleet.task_names

    asyncio.run(exercise())
    assert records == [("daemon_task_failed", {
        "task": "refresh_muster", "error_type": "RuntimeError"})]


def test_archive_task_failure_is_retrieved_and_recorded_once(monkeypatch):
    records = []
    monkeypatch.setattr(daemon.journal, "record",
                        lambda event, **fields: records.append((event, fields)))

    async def exercise():
        fleet = daemon.Fleet()
        fleet.muster_generation = ("socket", 1, 1)
        session = mock.Mock(ref=mock.Mock(key="source"))
        fleet.view = mock.Mock(return_value=([mock.Mock(session=session)], None, None))
        fleet.archive_authority = mock.Mock(return_value=(session, "newton", "authority"))
        fleet.publish_view = mock.Mock(return_value=("action", []))
        fleet.complete_archive = mock.AsyncMock(
            side_effect=RuntimeError("content must not enter the event"))

        assert await fleet.mutate_action("archive\tsource\t0\t100") == "action"
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not fleet.background_tasks
        assert not fleet.task_names

    asyncio.run(exercise())
    assert records == [("daemon_task_failed", {
        "task": "archive", "error_type": "RuntimeError"})]


def test_connected_host_requires_owned_process_evidence(monkeypatch):
    fleet = daemon.Fleet()
    monkeypatch.setattr(daemon, "decode_observation",
                        lambda raw: ([], {}, set(), mock.Mock()))
    with pytest.raises(KeyError):
        fleet.update_host("newton", b"observation")
    fleet.unavailable.discard("newton")
    with pytest.raises(RuntimeError, match="requires process identity and status"):
        asyncio.run(fleet.host_disconnected("newton"))


def test_daemon_ready_and_stopping_bracket_its_owned_server(tmp_path, monkeypatch):
    records = []

    class Server:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def serve_forever(self):
            pass

    async def start(*args):
        return Server()

    monkeypatch.setattr(daemon, "RUNTIME", tmp_path)
    monkeypatch.setattr(daemon, "hosts", lambda: ["lovelace"])
    monkeypatch.setattr(daemon.asyncio, "start_unix_server", start)
    monkeypatch.setattr(daemon.os, "chmod", lambda *args: None)
    monkeypatch.setattr(daemon.journal, "record",
                        lambda event, **fields: records.append((event, fields)))
    fleet = daemon.Fleet()
    fleet.register_existing_muster = mock.AsyncMock()
    fleet.collect = mock.AsyncMock()

    asyncio.run(fleet.serve())

    fields = {"socket": str(tmp_path / "fleet.sock"), "hosts_text": "lovelace"}
    assert records == [("daemon_ready", fields), ("daemon_stopping", fields)]


def test_projection_events_distinguish_cold_same_host_and_cross_host(monkeypatch):
    state = viewer.Attachment("main", "/dev/pts/9", mock.Mock())
    records = []
    entries = {
        "lovelace": mock.Mock(source="local-old", client="/dev/pts/1", window="@1"),
        "newton": mock.Mock(source="remote-old", client="/dev/pts/2", window="@2"),
    }
    monkeypatch.setattr(viewer.journal, "record",
                        lambda event, **fields: records.append((event, fields)))
    monkeypatch.setattr(state, "create_host", lambda host, key: entries[host])
    monkeypatch.setattr(state, "resident_switch", lambda key, client: None)
    monkeypatch.setattr(state, "select_host", lambda entry: None)

    state.open("lovelace:/tmp/tmux/default:12:10:$1")
    state.open("lovelace:/tmp/tmux/default:12:10:$2")
    state.attachments["newton"] = entries["newton"]
    state.open("newton:/tmp/tmux/default:13:11:$1")

    assert [fields["path"] for event, fields in records
            if event == "projection_completed"] == ["cold", "same_host", "cross_host"]


def test_dead_presentation_records_tmux_exit_metadata_without_capture(monkeypatch):
    state = viewer.Attachment("main", "/dev/pts/9", mock.Mock())
    state.host = "newton"
    state.source = "source"
    state.attachments["newton"] = mock.Mock(
        window="@2", remote_file=None, master=None)
    records = []
    values = {"#{pane_dead}": "1", "#{pane_dead_status}\t#{pane_dead_signal}": "7\t9"}
    monkeypatch.setattr(state, "ui_value", lambda window, value: values[value])
    monkeypatch.setattr(viewer.journal, "record",
                        lambda event, **fields: records.append((event, fields)))

    assert state.check() == "Viewer attachment exited unexpectedly"

    assert records == [
        ("attachment_exited", {
            "slot": "main", "host": "newton", "window": "@2",
            "status": "7", "signal": "9"}),
        ("attachment_removed", {
            "slot": "main", "host": "newton", "window": "@2",
            "reason": "exited"}),
    ]
    assert "newton" not in state.attachments


def test_dead_presentation_records_exit_once_after_removal_succeeds(monkeypatch):
    state = viewer.Attachment("main", "/dev/pts/9", mock.Mock())
    state.host = "newton"
    state.source = "source"
    state.attachments["newton"] = mock.Mock(
        window="@2", remote_file=None, master=None)
    records = []
    values = {"#{pane_dead}": "1",
              "#{pane_dead_status}\t#{pane_dead_signal}": "7\t9"}
    monkeypatch.setattr(state, "ui_value", lambda window, value: values[value])
    state.ui.command.side_effect = [RuntimeError("kill failed"), ["@2"], []]
    monkeypatch.setattr(viewer.journal, "record",
                        lambda event, **fields: records.append((event, fields)))

    with pytest.raises(RuntimeError, match="kill failed"):
        state.check()
    assert records == []
    assert "newton" in state.attachments

    assert state.check() == "Viewer attachment exited unexpectedly"
    assert [event for event, _ in records] == [
        "attachment_exited", "attachment_removed"]
    assert "newton" not in state.attachments


def test_viewer_failure_stages_are_assigned_at_the_owning_boundary(monkeypatch):
    state = viewer.Attachment("main", "/dev/pts/9", mock.Mock())
    failures = []
    monkeypatch.setattr(viewer.os, "uname", lambda: mock.Mock(nodename=viewer.HUB))

    def caught(call):
        with pytest.raises(viewer.ViewerFailure) as raised:
            call()
        failures.append((raised.value.stage, raised.value.cause))

    monkeypatch.setattr(viewer, "daemon_request", mock.Mock(side_effect=OSError("down")))
    caught(lambda: state.daemon("status"))
    monkeypatch.setattr(viewer.subprocess, "run",
                        mock.Mock(side_effect=subprocess.CalledProcessError(1, "ssh")))
    caught(lambda: state.ssh("newton", "true"))
    caught(lambda: state.resolve("invalid"))
    state.ui.command.side_effect = RuntimeError("tmux failed")
    caught(lambda: state.create_host("lovelace", "lovelace:/tmp/tmux:1:1:$1"))
    monkeypatch.setattr(state, "daemon", lambda message: '{"error":"stale"}')
    caught(lambda: state.resident_switch("source", "/dev/pts/8"))
    caught(lambda: state.select_host(mock.Mock(window="@2")))
    monkeypatch.setattr(viewer.workstation, "request", mock.Mock(side_effect=OSError("gone")))
    caught(lambda: viewer.focus("main", "boltzmann"))

    assert failures == [
        ("daemon", "unavailable"),
        ("ssh", "command"),
        ("resolve", "invalid_identity"),
        ("attach", "window"),
        ("switch", "identity_or_client"),
        ("select", "window"),
        ("focus", "workstation"),
    ]


def test_journal_transport_failure_does_not_stop_successful_projection(monkeypatch):
    state = viewer.Attachment("main", "/dev/pts/9", mock.Mock())
    key = "lovelace:/tmp/tmux/default:12:10:$1"
    entry = mock.Mock(source=key, client="/dev/pts/8", window="@2",
                      remote_file=None, master=None)
    records = []
    monkeypatch.setattr(state, "create_host", lambda host, source: entry)
    monkeypatch.setattr(state, "select_host", lambda selected: None)
    monkeypatch.setattr(viewer.journal, "record",
                        lambda event, **fields: records.append((event, fields)) or False)
    worker = viewer.ViewerWorker(state, "main")
    try:
        worker.intent("PROJECT", key)
        assert worker.barrier("SOURCE") == key
        assert worker.thread.is_alive()
    finally:
        worker.close()
    assert [event for event, _ in records].count("projection_completed") == 1


def test_attachment_creation_and_removal_events_follow_real_lifecycle(monkeypatch):
    records = []
    ui = mock.Mock()
    ui.command.side_effect = [["@2"], ["/dev/pts/8"], []]
    state = viewer.Attachment("main", "/dev/pts/9", ui)
    monkeypatch.setattr(viewer.os, "uname", lambda: mock.Mock(nodename="lovelace"))
    monkeypatch.setattr(state, "resolve", lambda key: ("/tmp/tmux", 12, 10, "$1"))
    monkeypatch.setattr(state, "prove_switch", lambda key, client: None)
    monkeypatch.setattr(viewer.journal, "record",
                        lambda event, **fields: records.append((event, fields)))

    entry = state.create_host("lovelace", "source")
    state.attachments["lovelace"] = entry
    state.remove_host("lovelace", "clear")

    assert records == [
        ("attachment_created", {
            "slot": "main", "host": "lovelace", "route": "local",
            "window": "@2", "client": "/dev/pts/8"}),
        ("attachment_removed", {
            "slot": "main", "host": "lovelace", "window": "@2",
            "reason": "clear"}),
    ]


def test_failed_attachment_creation_records_only_removal(monkeypatch):
    records = []
    ui = mock.Mock()
    ui.command.side_effect = [["@2"], [], []]
    state = viewer.Attachment("main", "/dev/pts/9", ui)
    monkeypatch.setattr(viewer.os, "uname", lambda: mock.Mock(nodename="lovelace"))
    monkeypatch.setattr(state, "resolve", lambda key: ("/tmp/tmux", 12, 10, "$1"))
    monkeypatch.setattr(viewer.journal, "record",
                        lambda event, **fields: records.append((event, fields)))

    with pytest.raises(viewer.ViewerFailure):
        state.create_host("lovelace", "source")

    assert records == [("attachment_removed", {
        "slot": "main", "host": "lovelace", "window": "@2",
        "reason": "create_failed"})]


def test_failed_window_removal_retains_attachment_without_removed_event(monkeypatch):
    records = []
    ui = mock.Mock()
    ui.command.side_effect = [RuntimeError("kill failed"), ["@2"]]
    state = viewer.Attachment("main", "/dev/pts/9", ui)
    entry = mock.Mock(window="@2", remote_file=None)
    state.attachments["lovelace"] = entry
    monkeypatch.setattr(viewer.journal, "record",
                        lambda event, **fields: records.append((event, fields)))

    with pytest.raises(RuntimeError, match="kill failed"):
        state.remove_host("lovelace", "shutdown")

    assert state.attachments == {"lovelace": entry}
    assert records == []


def test_absent_window_completes_removal_after_kill_reports_failure(monkeypatch):
    records = []
    ui = mock.Mock()
    ui.command.side_effect = [RuntimeError("can't find window"), ["@3"]]
    state = viewer.Attachment("main", "/dev/pts/9", ui)
    state.attachments["lovelace"] = mock.Mock(window="@2", remote_file=None)
    monkeypatch.setattr(viewer.journal, "record",
                        lambda event, **fields: records.append((event, fields)))

    state.remove_host("lovelace", "shutdown")

    assert state.attachments == {}
    assert records == [("attachment_removed", {
        "slot": "main", "host": "lovelace", "window": "@2",
        "reason": "shutdown"})]


def test_create_cleanup_failure_preserves_original_attachment_failure(monkeypatch):
    records = []
    ui = mock.Mock()
    ui.command.side_effect = [
        ["@2"], [], RuntimeError("kill failed"), ["@2"]]
    state = viewer.Attachment("main", "/dev/pts/9", ui)
    monkeypatch.setattr(viewer.os, "uname", lambda: mock.Mock(nodename="lovelace"))
    monkeypatch.setattr(state, "resolve", lambda key: ("/tmp/tmux", 12, 10, "$1"))
    monkeypatch.setattr(viewer.journal, "record",
                        lambda event, **fields: records.append((event, fields)))

    with pytest.raises(viewer.ViewerFailure) as raised:
        state.create_host("lovelace", "source")

    assert (raised.value.stage, raised.value.cause) == (
        "attach", "client_registration")
    assert records == []


def test_nested_boundary_preserves_the_first_specific_failure():
    error = OSError("content")
    with pytest.raises(viewer.ViewerFailure) as raised:
        with viewer.boundary("daemon", "unavailable"):
            raise viewer.ViewerFailure("ssh", "command", error) from error
    assert (raised.value.stage, raised.value.cause, raised.value.error_type) == (
        "ssh", "command", "OSError")


def test_viewer_ready_and_stopping_bracket_the_controller_socket(tmp_path, monkeypatch):
    records = []
    state = mock.Mock(source="", host="")
    state.check.return_value = ""
    monkeypatch.setattr(viewer, "RUNTIME", tmp_path)
    monkeypatch.setattr(viewer.os, "ttyname", lambda fd: "/dev/pts/9")
    monkeypatch.setattr(viewer, "Attachment", lambda slot, tty: state)
    monkeypatch.setattr(viewer, "viewer_error", lambda value: None)
    monkeypatch.setattr(viewer.journal, "record",
                        lambda event, **fields: records.append((event, fields)))
    thread = threading.Thread(target=viewer.serve, args=("test",))
    thread.start()
    path = tmp_path / "viewer-test.sock"
    for _ in range(100):
        if path.exists():
            break
        time.sleep(.01)
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(str(path))
        client.sendall(b"SHUTDOWN\n")
        assert client.recv(16) == b"OK\n"
    thread.join(1)

    assert not thread.is_alive()
    fields = {"slot": "test", "tty": "/dev/pts/9"}
    assert records == [("viewer_ready", fields), ("viewer_stopping", fields)]


def test_operation_failure_records_controlled_cause_once_without_error_text(monkeypatch):
    records = []
    failed = threading.Event()
    state = mock.Mock(source="lovelace:/tmp/tmux/default:1:1:$1", host="lovelace")
    state.check.return_value = ""

    def fail(key, selected=None, host=None):
        failed.set()
        error = RuntimeError("prompt and transcript content")
        raise viewer.ViewerFailure("switch", "identity_or_client", error) from error

    state.open.side_effect = fail
    monkeypatch.setattr(viewer, "viewer_error", lambda value: None)
    monkeypatch.setattr(viewer.journal, "record",
                        lambda event, **fields: records.append((event, fields)))
    worker = viewer.ViewerWorker(state, "main")
    try:
        worker.intent("PROJECT", "newton:/tmp/tmux/default:2:2:$2")
        assert failed.wait(1)
        assert worker.barrier("SOURCE") == "lovelace:/tmp/tmux/default:1:1:$1"
    finally:
        worker.close()

    assert records == [("viewer_operation_failed", {
        "slot": "main", "operation": "PROJECT", "host": "newton",
        "source": "newton:/tmp/tmux/default:2:2:$2", "stage": "switch",
        "cause": "identity_or_client",
        "error_type": "RuntimeError"})]


@pytest.mark.parametrize("key", [
    "not-a-canonical-key",
    "alan:malformed",
    "-oProxyCommand=touch:/tmp/tmux:1:1:$1",
    "alan:x@-oProxyCommand=touch",
    "elsewhere:/tmp/tmux:1:1:$1",
])
def test_malformed_identity_fails_once_without_routing_or_stopping_worker(
        monkeypatch, key):
    records = []
    state = viewer.Attachment("main", "/dev/pts/9", mock.Mock())
    ensure_master = mock.Mock()
    monkeypatch.setattr(state, "ensure_master", ensure_master)
    monkeypatch.setattr(viewer, "viewer_error", lambda value: None)
    monkeypatch.setattr(viewer.journal, "record",
                        lambda event, **fields: records.append((event, fields)))
    worker = viewer.ViewerWorker(state, "main")
    try:
        worker.intent("PROJECT", key)
        assert worker.barrier("SOURCE") == ""
        assert worker.thread.is_alive()
    finally:
        worker.close()

    ensure_master.assert_not_called()
    assert records == [("viewer_operation_failed", {
        "slot": "main", "operation": "PROJECT", "host": "", "source": "",
        "stage": "resolve", "cause": "invalid_identity",
        "error_type": "ValueError"})]


def test_unexpected_worker_failure_records_controller_event_once(monkeypatch):
    records = []
    state = mock.Mock(source="old", host="newton")
    state.check.return_value = ""
    state.open.side_effect = AssertionError("prompt and transcript content")
    monkeypatch.setattr(viewer, "viewer_error", lambda value: None)
    monkeypatch.setattr(viewer.journal, "record",
                        lambda event, **fields: records.append((event, fields)))
    worker = viewer.ViewerWorker(state, "main")
    worker.intent("PROJECT", "newton:/tmp/tmux/default:2:2:$2")
    worker.thread.join(1)

    assert not worker.thread.is_alive()
    assert records == [("viewer_controller_failed", {
        "slot": "main", "operation": "PROJECT", "host": "newton",
        "source": "newton:/tmp/tmux/default:2:2:$2", "stage": "worker",
        "cause": "unexpected",
        "error_type": "AssertionError"})]
