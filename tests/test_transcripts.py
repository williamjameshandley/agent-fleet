import json

from fleet_next.model import ServerRef, Session, SessionRef
from fleet_next.transcripts import (PANE_FORMAT, indexed_claude_agents, observe_native,
                                    select_codex, transcript)


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


def test_native_actor_recency_comes_from_its_vendor_transcript(tmp_path, monkeypatch):
    path = tmp_path / "00000000-0000-0000-0000-000000000001.jsonl"
    path.write_text('{"timestamp":"2026-07-24T08:27:16Z"}\n')
    item = transcript("claude", path)
    actor = Session(
        SessionRef(ServerRef("lovelace", "", 0, 0, "alan"), "claude-1"),
        "review", 0, 0, 0, 1, "tmux", "", "/work", "tracked",
        "claude", "waiting", transcript_id=item.session_id)
    monkeypatch.setattr("fleet_next.transcripts.all_transcripts", lambda: [item])

    observed = observe_native([actor])[0]

    assert observed.recency == 1784881636
