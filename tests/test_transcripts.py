import json
from unittest import mock

import agent_fleet.transcripts as transcripts
from agent_fleet.transcripts import (PANE_FORMAT, indexed_claude_agents, last_human_time,
                                    latest_assistant_text, fold_adopted, preview,
                                    project_native, select_codex, transcript, verify)
from agent_fleet.model import ServerRef, Session, SessionRef


def rollout(path, identity, source="cli", parent_thread_id=None):
    path.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": identity, "source": source, "cwd": "/work",
        "parent_thread_id": parent_thread_id}}) + "\n")
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
                    "00000000-0000-0000-0000-000000000002", "subagent",
                    "00000000-0000-0000-0000-000000000001")
    assert select_codex([root, child], set()) == root


def test_transcript_identity_comes_from_rollout_filename(tmp_path):
    path = tmp_path / "rollout-00000000-0000-0000-0000-000000000001.jsonl"
    rollout(path, "00000000-0000-0000-0000-000000000001")
    assert transcript("codex", path).session_id == "00000000-0000-0000-0000-000000000001"


def test_catalog_rejects_duplicate_full_provider_identity(tmp_path, monkeypatch):
    identity = "00000000-0000-0000-0000-000000000001"
    first = tmp_path / f"one-{identity}.jsonl"
    second = tmp_path / f"two-{identity}.jsonl"
    first.write_text("{}\n")
    second.write_text("{}\n")
    items = [transcript("claude", first), transcript("claude", second)]
    monkeypatch.setattr(transcripts, "all_transcripts", lambda agent=None: items)
    with __import__("pytest").raises(ValueError, match="duplicate claude transcript identity"):
        transcripts.catalog()


def test_search_is_literal_case_insensitive_and_one_hit_per_message(tmp_path, monkeypatch):
    identity = "00000000-0000-0000-0000-000000000001"
    path = tmp_path / f"rollout-{identity}.jsonl"
    events = [
        {"type": "session_meta", "payload": {"cwd": "/srv/mdjudge"}},
        {"type": "event_msg", "payload": {
            "type": "user_message", "message": "MDJudge then mdjudge"}},
        {"type": "event_msg", "payload": {
            "type": "agent_message", "message": "unrelated"}},
    ]
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    monkeypatch.setattr(transcripts, "all_transcripts",
                        lambda agent=None: [transcript("codex", path)])

    assert transcripts.search("mdjudge") == [{
        "agent": "codex", "session_id": identity, "path": str(path),
        "line": 2, "role": "user", "cwd": "/srv/mdjudge",
        "text": "MDJudge then mdjudge",
    }]

    with path.open("a") as stream:
        stream.write(json.dumps({"type": "event_msg", "payload": {
            "type": "agent_message", "message": "new mdjudge result"}}) + "\n")
    assert [hit["line"] for hit in transcripts.search("mdjudge")] == [2, 4]


def test_archive_verifies_the_full_transcript_identity(monkeypatch):
    item = type("Transcript", (), {"session_id": "full-session-id"})()
    monkeypatch.setattr(transcripts, "find", lambda session_id, agent: item)
    with __import__("pytest").raises(RuntimeError, match="transcript identity changed"):
        verify("codex", "prefix")


def test_claude_resume_uses_the_full_verified_native_identity(monkeypatch):
    item = type("Transcript", (), {"session_id": "full-claude-id",
                                   "cwd": lambda self: "/work"})()
    monkeypatch.setattr(transcripts, "find", lambda session_id, agent: item)
    with mock.patch("agent_fleet.transcripts.subprocess.run") as run:
        transcripts.resume("claude", "full-claude-id", "work")
    assert run.call_args_list[0] == mock.call(
        ["/usr/bin/tmux", "-N", "new-session", "-d", "-s", "work", "-c", "/work",
         "claude", "--resume", "full-claude-id"], check=True)
    assert run.call_args_list[1] == mock.call(
        ["/usr/bin/tmux", "-N", "set-option", "-t", "work", "status", "on"], check=True)


def test_codex_resume_uses_the_full_verified_native_identity(monkeypatch):
    item = type("Transcript", (), {"session_id": "full-codex-id",
                                   "cwd": lambda self: "/work"})()
    monkeypatch.setattr(transcripts, "find", lambda session_id, agent: item)
    with mock.patch("agent_fleet.transcripts.subprocess.run") as run:
        transcripts.resume("codex", "full-codex-id", "work")
    assert run.call_args_list[0] == mock.call(
        ["/usr/bin/tmux", "-N", "new-session", "-d", "-s", "work", "-c", "/work",
         "codex", "resume", "full-codex-id"], check=True)
    assert run.call_args_list[1] == mock.call(
        ["/usr/bin/tmux", "-N", "set-option", "-t", "work", "status", "on"], check=True)


def test_resume_rejects_a_prefix_before_creating_tmux(monkeypatch):
    item = type("Transcript", (), {"session_id": "full-session-id"})()
    monkeypatch.setattr(transcripts, "find", lambda session_id, agent: item)
    with mock.patch("agent_fleet.transcripts.subprocess.run") as run, \
         __import__("pytest").raises(RuntimeError, match="transcript identity changed"):
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


def test_attribution_and_sglang2_recover_summary_recency_and_path_by_full_id(tmp_path):
    source = ServerRef("lovelace", "", 0, 0, "alan")
    sessions = []
    native = {}
    for offset, name in enumerate(("attribution", "sglang2"), 1):
        identity = f"00000000-0000-0000-0000-{offset:012d}"
        actor = f"codex-{identity}@lovelace"
        path = tmp_path / f"rollout-{identity}.jsonl"
        path.write_text("".join(json.dumps(event) + "\n" for event in [
            {"type": "event_msg", "timestamp": f"2026-07-20T10:00:0{offset}Z",
             "payload": {"type": "user_message", "message": name}},
            {"type": "event_msg", "payload": {
                "type": "agent_message", "message": f"{name} summary"}},
        ]))
        sessions.append(Session(
            SessionRef(source, actor), name, 1, 0, 0, 1, "alan", "", "/work",
            "codex", "waiting", transcript_id=identity))
        native[("codex", identity)] = transcript("codex", path)

    projected = project_native(sessions, native)

    assert [item.summary for item in projected] == [
        "attribution summary", "sglang2 summary"]
    assert all(item.human_activity > 0 for item in projected)
    assert [item.transcript_path for item in projected] == [
        str(item.path) for item in native.values()]


def test_unmatched_alan_provider_has_no_invented_summary_or_path():
    source = ServerRef("newton", "", 0, 0, "alan")
    session = Session(SessionRef(source, "codex-1"), "work", 1, 0, 0, 1,
                      "alan", "", "/work", "codex", "waiting",
                      summary="Alan output is not a provider summary",
                      transcript_id="missing", transcript_path="/obsolete/path")
    [projected] = project_native([session], {})
    assert projected.summary == ""
    assert projected.transcript_path == ""


def test_adopted_actor_folds_the_provider_row_but_retains_its_attachment():
    identity = "00000000-0000-0000-0000-000000000001"
    alan_server = ServerRef("newton", "", 0, 0, "alan")
    tmux_server = ServerRef("newton", "/tmp/tmux/default", 42, 10)
    actor = Session(
        SessionRef(alan_server, f"codex-{identity}@newton"),
        "actor",
        1,
        0,
        0,
        1,
        "alan",
        "",
        "/work",
        "codex",
        "waiting",
        transcript_id=identity,
    )
    provider = Session(
        SessionRef(tmux_server, "$7"),
        "fleet@native-test",
        1,
        2,
        1,
        1,
        "codex",
        "",
        "/work",
        "codex",
        "working",
        transcript_id=identity,
    )

    assert fold_adopted([provider, actor]) == [
        __import__("dataclasses").replace(actor, attachment=provider.ref)
    ]


def test_native_wrapper_derives_provider_from_its_process_tree(monkeypatch):
    identity = "00000000-0000-0000-0000-000000000001"
    session = Session(
        SessionRef(ServerRef("newton", "/tmp/tmux", 1, 1), "$7"),
        "fleet@native-test", 1, 2, 1, 1, "python3", "", "/work",
    )
    item = type("Transcript", (), {"session_id": identity})()

    def run(arguments, **_kwargs):
        output = ("[]" if arguments[:2] == ["claude", "agents"] else
                  "name=fleet@native-test session=$7 pid=100 command=python3 title=''\n")
        return type("Result", (), {"stdout": output})()

    monkeypatch.setattr(transcripts.subprocess, "run", run)
    monkeypatch.setattr(transcripts, "process_tree", lambda: {100: [101]})
    monkeypatch.setattr(transcripts.os, "readlink", lambda path: (
        "/usr/bin/codex" if path == "/proc/101/exe" else "/usr/bin/python3"))
    monkeypatch.setattr(transcripts, "codex_transcript", lambda _tree: item)
    monkeypatch.setattr(transcripts, "codex_state",
                        lambda _item: ("waiting", "native reply", 3))
    monkeypatch.setattr(transcripts, "last_human_time", lambda _item: 2)

    [projected] = transcripts.observe([session], {})

    assert projected.agent == "codex"
    assert projected.transcript_id == identity


def test_adoption_join_is_host_local():
    identity = "00000000-0000-0000-0000-000000000001"
    actor = Session(
        SessionRef(ServerRef("newton", "", 0, 0, "alan"),
                   f"codex-{identity}@newton"),
        "actor", 1, 0, 0, 1, "alan", "", "/work", "codex", "waiting",
        transcript_id=identity,
    )
    provider = Session(
        SessionRef(ServerRef("boltzmann", "/tmp/tmux/default", 42, 10), "$7"),
        "fleet@native-test", 1, 2, 1, 1, "codex", "", "/work", "codex", "working",
        transcript_id=identity,
    )

    assert fold_adopted([provider, actor]) == [provider, actor]


def test_empty_native_transcript_projects_blank_state(tmp_path):
    identity = "00000000-0000-0000-0000-000000000001"
    path = tmp_path / f"rollout-{identity}.jsonl"
    path.touch()
    item = transcript("codex", path)
    source = ServerRef("newton", "", 0, 0, "alan")
    session = Session(SessionRef(source, "codex-1"), "work", 1, 0, 0, 1,
                      "alan", "", "/work", "codex", "waiting",
                      summary="stale", transcript_id=identity)

    [projected] = project_native([session], {("codex", identity): item})

    assert projected.summary == ""
    assert projected.human_activity == 0
    assert projected.transcript_path == str(path)


def test_preview_renders_recent_native_conversation(tmp_path, monkeypatch):
    path = tmp_path / "rollout-thread-1.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in [
        {"type": "event_msg", "payload": {
            "type": "user_message", "message": "inspect the interface"}},
        {"type": "event_msg", "payload": {
            "type": "agent_message", "message": "working on it"}},
    ]))
    native = transcripts.transcript("codex", path)
    monkeypatch.setattr(transcripts, "find", lambda session_id, agent: native)

    assert preview("codex", "thread-1", columns=80, lines=20) == (
        "User\ninspect the interface\n\nAssistant\nworking on it\n")


def test_claude_preview_keeps_only_the_last_eight_messages(tmp_path, monkeypatch):
    path = tmp_path / "session.jsonl"
    path.write_text("".join(json.dumps({
        "type": "user", "message": {"content": f"message {index}"}}) + "\n"
        for index in range(10)))
    native = transcripts.transcript("claude", path)
    monkeypatch.setattr(transcripts, "find", lambda session_id, agent: native)

    rendered = preview("claude", "session-1")
    assert "message 0" not in rendered
    assert "message 1" not in rendered
    assert "message 2" in rendered
    assert "message 9" in rendered
