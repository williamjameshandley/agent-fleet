import json
from unittest import mock

import agent_fleet.transcripts as transcripts
from agent_fleet.transcripts import (PANE_FORMAT, indexed_claude_agents, last_human_time,
                                    latest_assistant_text, native_transcript, preview,
                                    select_codex, transcript, verify)
from agent_fleet.model import ServerRef, Session, SessionRef


def rollout(path, identity, source="cli"):
    path.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": identity, "source": source, "cwd": "/work"}}) + "\n")
    return str(path)


def test_pane_format_preserves_an_empty_title_field():
    assert "title=#{q:pane_title}" in PANE_FORMAT


def test_stopped_claude_agents_without_pids_are_ignored():
    live = {"pid": 42, "sessionId": "live"}
    stopped = {"sessionId": "stopped", "state": "stopped"}
    assert indexed_claude_agents(json.dumps([live, stopped])) == {42: live}


def test_explicit_codex_resume_selects_matching_rollout(tmp_path):
    first = rollout(tmp_path / "rollout-00000000-0000-0000-0000-000000000001.jsonl",
                    "00000000-0000-0000-0000-000000000001")
    resumed = rollout(tmp_path / "rollout-00000000-0000-0000-0000-000000000002.jsonl",
                      "00000000-0000-0000-0000-000000000002")
    assert select_codex([first, resumed], {"00000000-0000-0000-0000-000000000002"}) == resumed


def test_root_codex_rollout_is_selected_without_resume_argument(tmp_path):
    root = rollout(tmp_path / "rollout-00000000-0000-0000-0000-000000000001.jsonl",
                   "00000000-0000-0000-0000-000000000001")
    child = rollout(tmp_path / "rollout-00000000-0000-0000-0000-000000000002.jsonl",
                    "00000000-0000-0000-0000-000000000002", "subagent")
    assert select_codex([root, child], set()) == root


def test_transcript_identity_comes_from_rollout_filename(tmp_path):
    path = tmp_path / "rollout-00000000-0000-0000-0000-000000000001.jsonl"
    rollout(path, "00000000-0000-0000-0000-000000000001")
    assert transcript("codex", path).session_id == "00000000-0000-0000-0000-000000000001"


def test_archive_verifies_the_full_transcript_identity(monkeypatch):
    item = type("Transcript", (), {"session_id": "full-session-id"})()
    monkeypatch.setattr(transcripts, "find", lambda session_id, agent: item)
    with __import__("pytest").raises(SystemExit, match="transcript identity changed"):
        verify("codex", "prefix")


def test_claude_resume_uses_the_full_verified_native_identity(monkeypatch):
    item = type("Transcript", (), {"session_id": "full-claude-id",
                                   "cwd": lambda self: "/work"})()
    monkeypatch.setattr(transcripts, "find", lambda session_id, agent: item)
    with mock.patch("agent_fleet.transcripts.subprocess.run") as run:
        transcripts.resume("claude", "full-claude-id", "work")
    assert run.call_args_list[0] == mock.call(
        ["tmux", "new-session", "-d", "-s", "work", "-c", "/work",
         "claude", "--resume", "full-claude-id"], check=True)
    assert run.call_args_list[1] == mock.call(
        ["tmux", "set-option", "-t", "work", "status", "off"], check=True)


def test_codex_resume_uses_the_full_verified_native_identity(monkeypatch):
    item = type("Transcript", (), {"session_id": "full-codex-id",
                                   "cwd": lambda self: "/work"})()
    monkeypatch.setattr(transcripts, "find", lambda session_id, agent: item)
    with mock.patch("agent_fleet.transcripts.subprocess.run") as run:
        transcripts.resume("codex", "full-codex-id", "work")
    assert run.call_args_list[0] == mock.call(
        ["tmux", "new-session", "-d", "-s", "work", "-c", "/work",
         "codex", "resume", "full-codex-id"], check=True)
    assert run.call_args_list[1] == mock.call(
        ["tmux", "set-option", "-t", "work", "status", "off"], check=True)


def test_resume_rejects_a_prefix_before_creating_tmux(monkeypatch):
    item = type("Transcript", (), {"session_id": "full-session-id"})()
    monkeypatch.setattr(transcripts, "find", lambda session_id, agent: item)
    with mock.patch("agent_fleet.transcripts.subprocess.run") as run, \
         __import__("pytest").raises(SystemExit, match="transcript identity changed"):
        transcripts.resume("codex", "prefix", "work")
    run.assert_not_called()


def test_claude_human_activity_excludes_tool_results_and_meta_events(tmp_path):
    path = tmp_path / "00000000-0000-0000-0000-000000000001.jsonl"
    events = [
        {"type": "user", "timestamp": "2026-07-20T10:00:00Z",
         "message": {"content": "human prompt"}},
        {"type": "user", "timestamp": "2026-07-20T11:00:00Z",
         "message": {"content": [{"type": "tool_result"}]}},
        {"type": "user", "timestamp": "2026-07-20T12:00:00Z", "isMeta": True,
         "message": {"content": "synthetic"}},
    ]
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    assert last_human_time(transcript("claude", path)) == 1784541600


def test_codex_human_activity_uses_latest_user_message(tmp_path):
    path = tmp_path / "rollout-00000000-0000-0000-0000-000000000001.jsonl"
    events = [
        {"type": "event_msg", "timestamp": "2026-07-20T10:00:00Z",
         "payload": {"type": "user_message", "message": "human prompt"}},
        {"type": "event_msg", "timestamp": "2026-07-20T11:00:00Z",
         "payload": {"type": "agent_message", "message": "response"}},
    ]
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    assert last_human_time(transcript("codex", path)) == 1784541600


def test_latest_assistant_text_reads_backwards_from_native_transcript(tmp_path):
    path = tmp_path / "rollout-00000000-0000-0000-0000-000000000001.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in [
        {"type": "event_msg", "payload": {
            "type": "agent_message", "message": "older reply"}},
        {"type": "event_msg", "payload": {
            "type": "agent_message", "message": "latest\nreply"}},
    ]))
    assert latest_assistant_text(transcript("codex", path)) == "latest reply"


def test_alan_claude_transcript_uses_native_identity_and_cwd(tmp_path, monkeypatch):
    identity = "00000000-0000-0000-0000-000000000001"
    path = tmp_path / "-work" / f"{identity}.jsonl"
    path.parent.mkdir()
    path.write_text("{}\n")
    monkeypatch.setattr(transcripts, "CLAUDE", tmp_path)
    source = ServerRef("newton", "", 0, 0, "alan")
    session = Session(SessionRef(source, "claude-1"), "work", 1, 0, 0, 1,
                      "tmux", "work", "/work", "claude", "waiting",
                      transcript_id=identity)
    assert native_transcript(session).path == path


def test_preview_renders_recent_native_conversation(tmp_path, monkeypatch):
    path = tmp_path / "rollout-thread-1.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in [
        {"type": "event_msg", "payload": {
            "type": "user_message", "message": "inspect the interface"}},
        {"type": "event_msg", "payload": {
            "type": "agent_message", "message": "working on it"}},
    ]))
    native = type("Native", (), {"path": path})()
    monkeypatch.setattr(transcripts, "find", lambda session_id, agent: native)

    assert preview("codex", "thread-1", columns=80, lines=20) == (
        "User\ninspect the interface\n\nAssistant\nworking on it\n")


def test_claude_preview_keeps_only_the_last_eight_messages(tmp_path, monkeypatch):
    path = tmp_path / "session.jsonl"
    path.write_text("".join(json.dumps({
        "type": "user", "message": {"content": f"message {index}"}}) + "\n"
        for index in range(10)))
    native = type("Native", (), {"path": path})()
    monkeypatch.setattr(transcripts, "find", lambda session_id, agent: native)

    rendered = preview("claude", "session-1")
    assert "message 0" not in rendered
    assert "message 1" not in rendered
    assert "message 2" in rendered
    assert "message 9" in rendered
