from pathlib import Path
import asyncio
from unittest import mock

import pytest

from agent_fleet import daemon, journal


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
