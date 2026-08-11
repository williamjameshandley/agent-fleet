import re


INFO = "6"
WARNING = "4"
ERROR = "3"
VALUES = {
    "cause": {"client_registration", "command", "identity_or_client",
              "invalid_identity", "invalid_reply", "policy", "refused",
              "unavailable", "unexpected", "window", "workstation"},
    "operation": {"CHECK", "CLEAR", "FOCUS", "OPEN", "PROJECT", "SHUTDOWN",
                  "SOURCE", "STATUS", "WORKSTATION"},
    "path": {"cold", "cross_host", "same_host"},
    "reason": {"clear", "create_failed", "exited", "missing", "rollback_failed",
               "select_failed", "shutdown"},
    "route": {"local", "remote"},
    "stage": {"attach", "daemon", "focus", "resolve", "select", "ssh", "switch",
              "worker"},
    "task": {"archive", "refresh_muster"},
}
ERROR_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
EVENTS = {
    "daemon_ready": ("daemon", INFO, "Fleet daemon ready", ("socket", "hosts_text")),
    "daemon_stopping": ("daemon", INFO, "Fleet daemon stopping", ("socket", "hosts_text")),
    "host_connected": ("daemon", INFO, "Fleet host connected", ("host", "pid")),
    "host_disconnected": (
        "daemon", WARNING, "Fleet host disconnected", ("host", "pid", "status")),
    "daemon_task_failed": (
        "daemon", ERROR, "Fleet daemon task failed", ("task", "error_type")),
    "viewer_ready": ("viewer", INFO, "Fleet viewer ready", ("slot", "tty")),
    "viewer_stopping": ("viewer", INFO, "Fleet viewer stopping", ("slot", "tty")),
    "attachment_created": (
        "viewer", INFO, "Fleet presentation attached",
        ("slot", "host", "route", "window", "client")),
    "attachment_removed": (
        "viewer", INFO, "Fleet presentation removed",
        ("slot", "host", "window", "reason")),
    "attachment_exited": (
        "viewer", WARNING, "Fleet presentation exited",
        ("slot", "host", "window", "status", "signal")),
    "projection_completed": (
        "viewer", INFO, "Fleet projection completed",
        ("slot", "host", "source", "path", "selection_ack_seconds",
         "transport_reply_seconds", "revalidate_switch_seconds")),
    "viewer_operation_failed": (
        "viewer", WARNING, "Fleet viewer operation failed",
        ("slot", "operation", "host", "source", "stage", "cause", "error_type")),
    "viewer_controller_failed": (
        "viewer", ERROR, "Fleet viewer controller failed",
        ("slot", "operation", "host", "source", "stage", "cause", "error_type")),
}


def _send(**fields):
    from systemd import journal
    journal.send(**fields)


def _value(name, value):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise TypeError(f"invalid journal value for {name}")
    text = str(value)
    if name in VALUES and text not in VALUES[name]:
        raise ValueError(f"invalid journal value for {name}")
    if name == "error_type" and not ERROR_TYPE.fullmatch(text):
        raise ValueError("invalid journal value for error_type")
    return text


def record(event, **fields):
    try:
        component, priority, message, names = EVENTS[event]
    except KeyError:
        raise ValueError(f"unknown Fleet journal event {event!r}") from None
    if set(fields) != set(names):
        raise ValueError(f"invalid fields for Fleet journal event {event!r}")
    entry = {
        "MESSAGE": message,
        "PRIORITY": priority,
        "SYSLOG_IDENTIFIER": "agent-fleet",
        "FLEET_COMPONENT": component,
        "FLEET_EVENT": event,
    }
    entry.update(("FLEET_" + name.upper(), _value(name, fields[name])) for name in names)
    try:
        _send(**entry)
    except Exception:
        return False
    return True
