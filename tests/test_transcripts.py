import asyncio
import json
from dataclasses import replace
from pathlib import Path
from unittest import mock

import agent_fleet.transcripts as transcripts
from agent_fleet.transcripts import (PANE_FORMAT, indexed_claude_agents, last_human_time,
                                    latest_assistant_text, fold_adopted, preview,
                                    project_native, select_codex, transcript, verify)
from agent_fleet.model import ServerRef, Session, SessionRef
from agent_fleet.daemon import Fleet


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


def test_claude_observe_accepts_optional_status(monkeypatch):
    cases = [
        ({}, "✳ waiting", "waiting"),
        ({}, "working", "working"),
        ({"state": "blocked"}, "working", "needs-action"),
        ({"status": "idle"}, "working", "waiting"),
    ]
    entries = [
        {"pid": 100 + index, "sessionId": f"session-{index}", "cwd": "/work",
         "kind": "interactive", "startedAt": 1, **fields}
        for index, (fields, _title, _state) in enumerate(cases)
    ]
    panes = "".join(
        f"name=work-{index} session=${index} pid={100 + index} "
        f"command=claude title={title!r}\n"
        for index, (_fields, title, _state) in enumerate(cases)
    )
    sessions = [
        Session(SessionRef(ServerRef("lovelace", "/tmp/tmux", 1, 1), f"${index}"),
                f"work-{index}", 1, 0, 0, 1, "claude", title, "/work")
        for index, (_fields, title, _state) in enumerate(cases)
    ]

    def run(arguments, **_kwargs):
        output = json.dumps(entries) if arguments[:2] == ["claude", "agents"] else panes
        return type("Result", (), {"stdout": output})()

    monkeypatch.setattr(transcripts.subprocess, "run", run)
    monkeypatch.setattr(transcripts, "process_tree", lambda: {})

    assert [session.reported_state for session in transcripts.observe(sessions, {})] == [
        state for _fields, _title, state in cases]


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


def test_claude_leading_records_without_timestamps_have_zero_recency(tmp_path):
    path = tmp_path / "claude.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in [
        {"type": "mode", "mode": "normal", "sessionId": "session-1"},
        {"type": "permission-mode", "permissionMode": "bypassPermissions",
         "sessionId": "session-1"},
    ]))

    assert transcripts.last_event_time(path) == 0


def test_timestamp_free_transcript_still_rejects_malformed_json(tmp_path):
    path = tmp_path / "claude.jsonl"
    path.write_text('{"type":"mode"}\n{"broken":}\n')

    with __import__("pytest").raises(json.JSONDecodeError):
        transcripts.last_event_time(path)


def test_transcript_still_rejects_invalid_timestamp(tmp_path):
    path = tmp_path / "claude.jsonl"
    path.write_text(json.dumps({"timestamp": "not-a-time"}) + "\n")

    with __import__("pytest").raises(ValueError):
        transcripts.last_event_time(path)


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


def test_native_resume_launches_the_integrated_provider_without_attaching(monkeypatch):
    item = type("Transcript", (), {"session_id": "full-codex-id",
                                   "cwd": lambda self: "/work"})()
    monkeypatch.setattr(transcripts, "find", lambda session_id, agent: item)
    with mock.patch.dict("agent_fleet.transcripts.os.environ",
                         {"HOME": "/home/will", "XDG_RUNTIME_DIR": "/run/user/1000"}), \
         mock.patch("agent_fleet.transcripts.tempfile.mkdtemp",
                    return_value="/run/user/1000/alan/native/codex-root") as temporary, \
         mock.patch("agent_fleet.transcripts.secrets.token_hex",
                    return_value="0123456789abcdef"), \
         mock.patch("agent_fleet.transcripts.subprocess.run") as run:
        transcripts.resume_native("codex", "full-codex-id")
    temporary.assert_called_once_with(prefix="codex-", dir=Path("/run/user/1000/alan/native"))
    assert run.call_args_list == [
        mock.call([
            "/usr/bin/tmux", "-N", "new-session", "-d", "-s",
            "fleet@native-0123456789abcdef", "-c", "/work",
            "-e", "ALAN_NATIVE_INNER=1", "-e",
            "ALAN_NATIVE_ROOT=/run/user/1000/alan/native/codex-root",
            "/usr/lib/alan/alan-native-session", "codex", "resume", "full-codex-id",
        ], check=True),
        mock.call(["/usr/bin/tmux", "-N", "set-option", "-t",
                   "fleet@native-0123456789abcdef", "status", "off"], check=True),
        mock.call(["/usr/bin/tmux", "-N", "set-option", "-t",
                   "fleet@native-0123456789abcdef", "mouse", "on"], check=True),
    ]


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


def test_codex_response_items_supply_messages_and_human_activity(tmp_path):
    path = tmp_path / "rollout-00000000-0000-0000-0000-000000000001.jsonl"
    events = [
        {"type": "response_item", "timestamp": "2026-07-20T10:00:00Z",
         "payload": {"type": "message", "role": "user", "content": [
             {"type": "input_text", "text": "human prompt"}]}},
        {"type": "response_item", "timestamp": "2026-07-20T11:00:00Z",
         "payload": {"type": "message", "role": "assistant", "content": [
             {"type": "output_text", "text": "latest\nreply"}]}},
    ]
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    item = transcript("codex", path)

    assert latest_assistant_text(item) == "latest reply"
    assert last_human_time(item) == 1784541600


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
        __import__("dataclasses").replace(
            actor, reported_state="working", attachment=provider.ref)
    ]

    assert fold_adopted([
        __import__("dataclasses").replace(provider, reported_state="waiting"),
        __import__("dataclasses").replace(actor, reported_state="working"),
    ]) == [
        __import__("dataclasses").replace(
            actor, reported_state="working", attachment=provider.ref)
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
    monkeypatch.setattr(transcripts, "codex_candidates", lambda _tree: (["rollout.jsonl"], set()))
    monkeypatch.setattr(transcripts, "transcript", lambda _agent, _path: item)
    monkeypatch.setattr(transcripts, "codex_state",
                        lambda _item: ("waiting", "native reply", 3))
    monkeypatch.setattr(transcripts, "last_human_time", lambda _item: 2)

    [projected] = transcripts.observe([session], {})

    assert projected.agent == "codex"
    assert projected.transcript_id == identity


def test_pristine_native_codex_joins_by_its_published_actor_socket(tmp_path,
                                                                  monkeypatch):
    identity = "00000000-0000-0000-0000-000000000001"
    actor_address = f"codex-{identity}@newton"
    alan_server = ServerRef("newton", "", 0, 0, "alan")
    tmux_server = ServerRef("newton", "/tmp/tmux", 1, 1)
    actor = Session(
        SessionRef(alan_server, actor_address), "Analysis", 1, 0, 0, 1,
        "alan", "", "/work", "codex", "waiting", transcript_id=identity,
        worked=False,
    )
    provider = Session(
        SessionRef(tmux_server, "$7"), "fleet@native-test", 1, 2, 1, 1,
        "python3", "will", "/work",
    )
    root = tmp_path / "codex-root"
    root.mkdir()
    (root / "loop.sock").symlink_to(
        tmp_path / "actors" / transcripts.runtime_name(actor_address) / "loop.sock")
    unrelated = tmp_path / "rollout-00000000-0000-0000-0000-000000000002.jsonl"
    rollout(unrelated, "00000000-0000-0000-0000-000000000002")
    current = tmp_path / f"rollout-{identity}.jsonl"
    candidates = [str(unrelated)]
    real_readlink = transcripts.os.readlink

    def run(arguments, **_kwargs):
        if arguments[:2] == ["claude", "agents"]:
            return type("Result", (), {"stdout": "[]", "returncode": 0})()
        if arguments[:3] == ["/usr/bin/tmux", "-N", "list-panes"]:
            output = ("name=fleet@native-test session=$7 pid=100 "
                      "command=python3 title=will\n")
            return type("Result", (), {"stdout": output, "returncode": 0})()
        assert arguments == ["/usr/bin/tmux", "-N", "show-environment",
                             "-t", "$7", "ALAN_NATIVE_ROOT"]
        return type("Result", (), {
            "stdout": f"ALAN_NATIVE_ROOT={root}\n", "returncode": 0})()

    monkeypatch.setattr(transcripts.subprocess, "run", run)
    monkeypatch.setattr(transcripts, "process_tree", lambda: {100: [101]})
    monkeypatch.setattr(
        transcripts.os, "readlink",
        lambda path: "/usr/bin/codex" if path == "/proc/101/exe"
        else real_readlink(path),
    )
    monkeypatch.setattr(
        transcripts, "codex_candidates", lambda _tree: (candidates, set()))

    [projected] = transcripts.observe([provider, actor], {})

    assert projected.ref == actor.ref
    assert projected.name == "Analysis"
    assert projected.transcript_id == identity
    assert projected.attachment == provider.ref

    async def create_from_projection():
        fleet = Fleet()
        fleet.unavailable.clear()
        with mock.patch("agent_fleet.daemon.hosts", return_value=["newton"]), \
             mock.patch.object(fleet, "authority", return_value={
                 "source": actor.ref.key}):
            pending = asyncio.create_task(fleet.action({
                "operation": "create", "host": "newton", "agent": "codex",
                "name": "Analysis", "cwd": "/work"}))
            await asyncio.sleep(0)
            assert not pending.done()
            fleet.sessions = {"newton": [projected]}
            fleet.observed += 1
            async with fleet.changed:
                fleet.changed.notify_all()
            assert await pending == {"source": actor.ref.key}

    asyncio.run(create_from_projection())

    current.write_text(
        json.dumps({"type": "session_meta", "timestamp": "2026-08-26T09:00:00Z",
                    "payload": {"id": identity, "cwd": "/work"}}) + "\n" +
        json.dumps({"type": "event_msg", "timestamp": "2026-08-26T09:00:01Z",
                    "payload": {"type": "agent_message", "message": "ready"}}) + "\n")
    candidates[:] = [str(unrelated), str(current)]

    [worked] = transcripts.observe(
        [provider, replace(actor, worked=True)],
        {("codex", identity): transcript("codex", current)},
    )

    assert worked.ref == actor.ref
    assert worked.summary == "ready"
    assert worked.attachment == provider.ref

    candidates[:] = [str(unrelated)]
    mismatch = transcripts.observe([provider, replace(actor, worked=True)], {})

    bare_actor = next(item for item in mismatch if item.ref == actor.ref)
    mismatched = next(item for item in mismatch if item.ref == provider.ref)
    assert bare_actor.attachment is None
    assert mismatched.reported_state == "needs-action"
    assert mismatched.summary == "Codex transcript is missing for worked actor"
    assert mismatched.transcript_id == ""


def test_native_provider_kind_mismatch_is_visible(tmp_path, monkeypatch):
    identity = "00000000-0000-0000-0000-000000000001"
    actor_address = f"claude-{identity}@newton"
    actor = Session(
        SessionRef(ServerRef("newton", "", 0, 0, "alan"), actor_address),
        "Analysis", 1, 0, 0, 1, "alan", "", "/work", "claude", "waiting",
        transcript_id=identity, worked=False,
    )
    provider = Session(
        SessionRef(ServerRef("newton", "/tmp/tmux", 1, 1), "$7"),
        "fleet@native-test", 1, 2, 1, 1, "python3", "will", "/work",
    )
    root = tmp_path / "codex-root"
    root.mkdir()
    (root / "loop.sock").symlink_to(
        tmp_path / "actors" / transcripts.runtime_name(actor_address) / "loop.sock")
    real_readlink = transcripts.os.readlink

    def run(arguments, **_kwargs):
        if arguments[:2] == ["claude", "agents"]:
            return type("Result", (), {"stdout": "[]", "returncode": 0})()
        if arguments[:3] == ["/usr/bin/tmux", "-N", "list-panes"]:
            output = ("name=fleet@native-test session=$7 pid=100 "
                      "command=python3 title=will\n")
            return type("Result", (), {"stdout": output, "returncode": 0})()
        return type("Result", (), {
            "stdout": f"ALAN_NATIVE_ROOT={root}\n", "returncode": 0})()

    monkeypatch.setattr(transcripts.subprocess, "run", run)
    monkeypatch.setattr(transcripts, "process_tree", lambda: {100: [101]})
    monkeypatch.setattr(
        transcripts.os, "readlink",
        lambda path: "/usr/bin/codex" if path == "/proc/101/exe"
        else real_readlink(path),
    )
    monkeypatch.setattr(transcripts, "codex_candidates", lambda _tree: ([], set()))

    projected = transcripts.observe([provider, actor], {})

    assert actor in projected
    mismatch = next(item for item in projected if item.ref == provider.ref)
    assert mismatch.agent == "codex"
    assert mismatch.reported_state == "needs-action"
    assert mismatch.summary == "Codex provider is attached to claude actor"
    assert mismatch.transcript_id == ""


def test_invalid_provider_transcript_isolated_to_its_session(tmp_path, monkeypatch):
    identity = "00000000-0000-0000-0000-000000000001"
    path = tmp_path / f"rollout-{identity}.jsonl"
    path.write_text('{"type":"session_meta","payload":{"id":')
    server = ServerRef("newton", "/tmp/tmux", 1, 1)
    broken = Session(
        SessionRef(server, "$7"), "broken", 1, 2, 1, 1,
        "codex", "", "/work",
    )
    healthy = Session(
        SessionRef(server, "$8"), "healthy", 1, 2, 1, 1,
        "zsh", "", "/work",
    )

    def run(arguments, **_kwargs):
        output = ("[]" if arguments[:2] == ["claude", "agents"] else
                  "name=broken session=$7 pid=100 command=codex title=''\n")
        return type("Result", (), {"stdout": output})()

    monkeypatch.setattr(transcripts.subprocess, "run", run)
    monkeypatch.setattr(transcripts, "process_tree", lambda: {})
    monkeypatch.setattr(transcripts, "codex_candidates", lambda _tree: ([str(path)], set()))

    projected_broken, projected_healthy = transcripts.observe([broken, healthy], {})

    assert projected_broken.agent == "codex"
    assert projected_broken.reported_state == "needs-action"
    assert projected_broken.transcript_id == identity
    assert projected_broken.summary.startswith("Transcript is invalid:")
    assert projected_healthy == healthy


def test_invalid_actor_transcript_isolated_to_its_session(tmp_path):
    identity = "00000000-0000-0000-0000-000000000001"
    path = tmp_path / f"rollout-{identity}.jsonl"
    path.write_text('{"type":"event_msg","payload":')
    server = ServerRef("newton", "", 0, 0, "alan")
    broken = Session(
        SessionRef(server, f"codex-{identity}@newton"), "broken", 1, 2, 1, 1,
        "alan", "", "/work", "codex", "waiting", transcript_id=identity,
    )
    healthy = Session(
        SessionRef(ServerRef("newton", "/tmp/tmux", 1, 1), "$8"),
        "healthy", 1, 2, 1, 1, "zsh", "", "/work",
    )

    projected_broken, projected_healthy = transcripts.project_native(
        [broken, healthy], {("codex", identity): transcript("codex", path)})

    assert projected_broken.reported_state == "needs-action"
    assert projected_broken.summary.startswith("Transcript is invalid:")
    assert projected_healthy == healthy


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


ANTIGRAVITY_ID = "0a1b2c3d-0000-4000-8000-000000000001"


def antigravity_transcript(root, identity, events):
    path = (root / "brain" / identity / ".system_generated/logs"
            / "transcript_full.jsonl")
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    return path


def test_antigravity_identity_comes_from_its_brain_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(transcripts, "ANTIGRAVITY", tmp_path)
    path = antigravity_transcript(tmp_path, ANTIGRAVITY_ID, [
        {"step_index": 0, "type": "USER_INPUT", "content": "hello",
         "created_at": "2026-08-17T00:00:00Z"}])

    assert transcript("antigravity", path).session_id == ANTIGRAVITY_ID
    (item,) = transcripts.all_transcripts("antigravity")
    assert item.session_id == ANTIGRAVITY_ID
    assert item.path == path


def test_antigravity_resume_uses_the_conversation_identity(monkeypatch):
    item = type("Transcript", (), {"session_id": ANTIGRAVITY_ID,
                                   "cwd": lambda self: "/work"})()
    monkeypatch.setattr(transcripts, "find", lambda session_id, agent: item)
    with mock.patch("agent_fleet.transcripts.subprocess.run") as run:
        transcripts.resume("antigravity", ANTIGRAVITY_ID, "work")
    assert run.call_args_list[0] == mock.call(
        ["/usr/bin/tmux", "-N", "new-session", "-d", "-s", "work", "-c", "/work",
         "agy", "--conversation", ANTIGRAVITY_ID], check=True)


def test_antigravity_user_text_is_unwrapped_and_roles_are_mapped():
    user = {"type": "USER_INPUT", "content":
            "<USER_REQUEST>\nfix the build\n</USER_REQUEST>\n"
            "<ADDITIONAL_METADATA>\nThe current local time is now.\n"
            "</ADDITIONAL_METADATA>"}
    reply = {"type": "PLANNER_RESPONSE", "content": "done"}

    assert transcripts.event_text("antigravity", user, "user") == "fix the build"
    assert transcripts.event_text("antigravity", user, "assistant") == ""
    assert transcripts.event_text("antigravity", reply, "assistant") == "done"
    assert transcripts.event_text("antigravity", reply, "user") == ""


def test_antigravity_state_is_working_until_the_turn_checkpoint(tmp_path):
    working = antigravity_transcript(tmp_path, ANTIGRAVITY_ID, [
        {"type": "USER_INPUT", "content": "task",
         "created_at": "2026-08-17T00:00:00Z"},
        {"type": "CONVERSATION_HISTORY", "created_at": "2026-08-17T00:00:01Z"},
    ])
    item = transcript("antigravity", working)
    state, summary, updated = transcripts.antigravity_state(item)
    assert (state, summary, updated) == ("working", "", 1786924801)

    with working.open("a") as stream:
        stream.write(json.dumps({
            "type": "PLANNER_RESPONSE", "content": "all fixed",
            "created_at": "2026-08-17T00:00:02Z"}) + "\n")
        stream.write(json.dumps({
            "type": "CHECKPOINT", "content": "{{ CHECKPOINT 0 }}",
            "created_at": "2026-08-17T00:00:03Z"}) + "\n")
    state, summary, updated = transcripts.antigravity_state(item)
    assert (state, summary, updated) == ("waiting", "all fixed", 1786924803)


def test_antigravity_human_activity_uses_created_at(tmp_path):
    path = antigravity_transcript(tmp_path, ANTIGRAVITY_ID, [
        {"type": "USER_INPUT", "content": "one",
         "created_at": "2026-08-17T00:00:00Z"},
        {"type": "PLANNER_RESPONSE", "content": "reply",
         "created_at": "2026-08-17T00:00:01Z"},
        {"type": "USER_INPUT", "content": "two",
         "created_at": "2026-08-17T00:00:02Z"},
        {"type": "CHECKPOINT", "created_at": "2026-08-17T00:00:03Z"},
    ])
    assert last_human_time(transcript("antigravity", path)) == 1786924802


def test_antigravity_workspace_reads_the_summaries_database(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setattr(transcripts, "ANTIGRAVITY", tmp_path)
    assert transcripts.antigravity_workspace(ANTIGRAVITY_ID) == ""

    with sqlite3.connect(tmp_path / "conversation_summaries.db") as connection:
        connection.execute(
            "create table conversation_summaries"
            " (conversation_id text, workspace_uris text)")
        connection.execute(
            "insert into conversation_summaries values (?, ?)",
            (ANTIGRAVITY_ID, json.dumps(["file:///work/project"])))
    assert transcripts.antigravity_workspace(ANTIGRAVITY_ID) == "/work/project"
    assert transcripts.antigravity_workspace("missing") == ""


def test_antigravity_candidates_scan_open_conversation_databases(tmp_path):
    import os

    database = tmp_path / "antigravity-cli/conversations" / f"{ANTIGRAVITY_ID}.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"")
    with database.open("rb"):
        conversations = transcripts.antigravity_candidates([os.getpid()])
    assert conversations == {ANTIGRAVITY_ID}
    assert transcripts.antigravity_candidates([os.getpid()]) == set()


def test_agy_panes_present_as_antigravity():
    session = Session(SessionRef(ServerRef("newton", "/tmp/tmux/default", 12, 10), "$1"),
                      "work", 1, 2, 0, 1, "/usr/bin/agy", "waiting", "/work")
    assert session.agent == "antigravity"
    assert session.state == "waiting"


def test_alan_antigravity_actor_summary_survives_projection():
    source = ServerRef("newton", "", 0, 0, "alan")
    session = Session(SessionRef(source, "antigravity-1"), "work", 1, 0, 0, 1,
                      "alan", "", "/work", "antigravity", "waiting",
                      summary="last antigravity output", transcript_id="")
    [projected] = project_native([session], {})
    assert projected.summary == "last antigravity output"
    assert projected.transcript_path == ""


def test_antigravity_search_hits_carry_the_summaries_workspace(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setattr(transcripts, "ANTIGRAVITY", tmp_path)
    path = antigravity_transcript(tmp_path, ANTIGRAVITY_ID, [
        {"type": "USER_INPUT", "content":
         "<USER_REQUEST>\nfind the needle\n</USER_REQUEST>",
         "created_at": "2026-08-17T00:00:00Z"},
    ])
    with sqlite3.connect(tmp_path / "conversation_summaries.db") as connection:
        connection.execute(
            "create table conversation_summaries"
            " (conversation_id text, workspace_uris text)")
        connection.execute(
            "insert into conversation_summaries values (?, ?)",
            (ANTIGRAVITY_ID, json.dumps(["file:///srv/project"])))
    monkeypatch.setattr(transcripts, "all_transcripts",
                        lambda agent=None: [transcript("antigravity", path)])

    assert transcripts.search("needle") == [{
        "agent": "antigravity", "session_id": ANTIGRAVITY_ID, "path": str(path),
        "line": 1, "role": "user", "cwd": "/srv/project",
        "text": "find the needle",
    }]


GROK_ID = "01a00000-0000-7000-8000-000000000001"


def grok_update(timestamp, kind, text=None, method="session/update", **fields):
    update = {"sessionUpdate": kind, **fields}
    if text is not None:
        update["content"] = {"type": "text", "text": text}
    return {"timestamp": timestamp, "method": method,
            "params": {"sessionId": GROK_ID, "update": update}}


def grok_session(root, identity, events, cwd="/work"):
    directory = root / "%2Fwork" / identity
    directory.mkdir(parents=True)
    (directory / "summary.json").write_text(json.dumps(
        {"info": {"id": identity, "cwd": cwd}}))
    path = directory / "updates.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    return path


def test_grok_identity_comes_from_its_session_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(transcripts, "GROK", tmp_path)
    path = grok_session(tmp_path, GROK_ID, [
        grok_update(100, "user_message_chunk", "hello")])

    item = transcript("grok", path)
    assert item.session_id == GROK_ID
    assert item.cwd() == "/work"
    (found,) = transcripts.all_transcripts("grok")
    assert found.session_id == GROK_ID
    assert found.path == path


def test_grok_resume_uses_the_session_identity(monkeypatch):
    item = type("Transcript", (), {"session_id": GROK_ID,
                                   "cwd": lambda self: "/work"})()
    monkeypatch.setattr(transcripts, "find", lambda session_id, agent: item)
    with mock.patch("agent_fleet.transcripts.subprocess.run") as run:
        transcripts.resume("grok", GROK_ID, "work")
    assert run.call_args_list[0] == mock.call(
        ["/usr/bin/tmux", "-N", "new-session", "-d", "-s", "work", "-c", "/work",
         "grok", "--resume", GROK_ID], check=True)


def test_grok_native_resume_arguments_use_the_flag_form(monkeypatch):
    item = type("Transcript", (), {"session_id": GROK_ID,
                                   "cwd": lambda self: "/work"})()
    monkeypatch.setattr(transcripts, "verify", lambda agent, session_id: item)
    with mock.patch("agent_fleet.transcripts.subprocess.run") as run:
        transcripts.resume_native("grok", GROK_ID)
    arguments = run.call_args_list[0].args[0]
    assert arguments[-3:] == ["grok", "--resume", GROK_ID]
    assert "/usr/lib/alan/alan-native-session" in arguments


def test_grok_update_chunks_map_to_roles():
    user = grok_update(100, "user_message_chunk", "fix the build")
    reply = grok_update(101, "agent_message_chunk", "done")
    thought = grok_update(101, "agent_thought_chunk", "thinking")

    assert transcripts.event_text("grok", user, "user") == "fix the build"
    assert transcripts.event_text("grok", user, "assistant") == ""
    assert transcripts.event_text("grok", reply, "assistant") == "done"
    assert transcripts.event_text("grok", reply, "user") == ""
    assert transcripts.event_text("grok", thought, "assistant") == ""


def test_grok_state_is_working_until_the_turn_completes(tmp_path):
    path = grok_session(tmp_path, GROK_ID, [
        grok_update(100, "user_message_chunk", "task"),
        grok_update(101, "agent_thought_chunk", "planning"),
    ])
    item = transcript("grok", path)
    assert transcripts.grok_state(item) == ("working", "", 101)

    with path.open("a") as stream:
        stream.write(json.dumps(grok_update(
            102, "agent_message_chunk", "all fixed")) + "\n")
        stream.write(json.dumps(grok_update(
            103, "turn_completed", method="_x.ai/session/update",
            prompt_id="prompt-1", stop_reason="end_turn")) + "\n")
    assert transcripts.grok_state(item) == ("waiting", "all fixed", 103)


def test_grok_human_activity_uses_integer_timestamps(tmp_path):
    path = grok_session(tmp_path, GROK_ID, [
        grok_update(100, "user_message_chunk", "one"),
        grok_update(101, "agent_message_chunk", "reply"),
        grok_update(102, "user_message_chunk", "two"),
        grok_update(103, "agent_message_chunk", "reply"),
    ])
    assert last_human_time(transcript("grok", path)) == 102


def test_grok_candidates_scan_open_session_files(tmp_path):
    import os

    events = tmp_path / ".grok/sessions/%2Fwork" / GROK_ID / "events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_bytes(b"")
    with events.open("rb"):
        sessions = transcripts.grok_candidates([os.getpid()])
    assert sessions == {GROK_ID: events.parent}
    assert transcripts.grok_candidates([os.getpid()]) == {}


def test_grok_panes_present_as_grok_with_title_states():
    ref = SessionRef(ServerRef("newton", "/tmp/tmux/default", 12, 10), "$1")
    waiting = Session(ref, "work", 1, 2, 0, 1, "/usr/bin/grok", "Fix Things - grok",
                      "/work")
    assert waiting.agent == "grok"
    assert waiting.state == "waiting"

    working = Session(ref, "work", 1, 2, 0, 1, "/usr/bin/grok",
                      "⠦ - Waiting for response… - Fix Things - grok", "/work")
    assert working.state == "working"
    responding = Session(ref, "work", 1, 2, 0, 1, "/usr/bin/grok",
                         "⠹ - Responding - Fix Things - grok", "/work")
    assert responding.state == "working"
