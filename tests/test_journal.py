from pathlib import Path

import pytest

from agent_fleet import journal


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
