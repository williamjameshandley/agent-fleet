import asyncio
import hashlib
import json
import unittest
from unittest import mock
from types import SimpleNamespace

from agent_fleet.commander import validate_proposal
from agent_fleet.daemon import Fleet


class CommanderContextTests(unittest.TestCase):
    def context(self, unavailable=(), profile_suffix="", transcript_name="old", sessions=()):
        fleet = Fleet()
        fleet.unavailable = set(unavailable)
        fleet.sessions = {"lovelace": list(sessions)}

        async def remote(host, *command):
            if command == ("fleet", "context"):
                return {"profile": host + profile_suffix, "unavailable": [],
                        "slots": [{"slot": "main", "source": ""}]}
            raise AssertionError((host, command))

        async def history(host):
            return {"host": host, "actors": [], "transcripts": [{
                "agent": "codex", "session_id": "history-thread", "mtime": 1,
                "cwd": "/srv/work", "name": transcript_name}]}

        fleet.remote_json = remote
        fleet.history_observation = history
        with mock.patch("agent_fleet.daemon.hosts", return_value=["newton", "lovelace"]):
            return asyncio.run(fleet.commander_context())

    def test_revision_hashes_the_canonical_body(self):
        context = self.context()
        revision = context.pop("revision")
        body = json.dumps(context, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode()
        self.assertEqual(revision, hashlib.sha256(body).hexdigest())
        self.assertEqual(context["hosts"], ["lovelace", "newton"])

    def test_changing_snapshot_content_changes_revision(self):
        baseline = self.context()["revision"]
        session = SimpleNamespace(
            ref=SimpleNamespace(key="source-1", server=SimpleNamespace(host="lovelace")),
            name="work", agent="codex", state="waiting", summary="summary",
            human_activity=2, transcript_id="live-thread")
        variants = [self.context(["newton"]), self.context(profile_suffix="-changed"),
                    self.context(transcript_name="changed"), self.context(sessions=[session])]
        self.assertTrue(all(item["revision"] != baseline for item in variants))


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.request = {"request_id": "r1", "snapshot": {
            "revision": "abc", "hosts": ["newton"],
            "sessions": [{"source": "source-1", "agent": "codex",
                          "transcript_id": "thread-1"}],
            "history": [{"key": "history-1"}],
            "workstations": {"boltzmann": {"slots": [{"slot": "main", "source": ""}]}}
        }}

    def test_accepts_the_closed_operations(self):
        proposals = [
            {"type": "show", "request_id": "r1", "snapshot_revision": "abc",
             "source": "source-1", "workstation": "boltzmann", "slot": "main"},
            {"type": "clear_slot", "request_id": "r1", "snapshot_revision": "abc",
             "workstation": "boltzmann", "slot": "main"},
            {"type": "create", "request_id": "r1", "snapshot_revision": "abc",
             "host": "newton", "agent": "claude", "name": "work", "cwd": None},
            {"type": "rename", "request_id": "r1", "snapshot_revision": "abc",
             "source": "source-1", "name": "new-name"},
            {"type": "archive", "request_id": "r1", "snapshot_revision": "abc",
             "source": "source-1"},
            {"type": "open", "request_id": "r1", "snapshot_revision": "abc",
             "history": "history-1", "workstation": "boltzmann", "slot": "main"},
        ]
        for proposal in proposals:
            self.assertIs(validate_proposal(proposal, self.request), proposal)

    def test_rejects_extra_fields_stale_identity_and_relative_cwd(self):
        bad = {"type": "archive", "request_id": "r1", "snapshot_revision": "abc",
               "source": "missing"}
        with self.assertRaises(ValueError):
            validate_proposal(bad, self.request)
        bad = {"type": "create", "request_id": "r1", "snapshot_revision": "abc",
               "host": "newton", "agent": "codex", "name": "work", "cwd": "relative"}
        with self.assertRaises(ValueError):
            validate_proposal(bad, self.request)
        bad["cwd"] = "/srv/work"
        bad["extra"] = True
        with self.assertRaises(ValueError):
            validate_proposal(bad, self.request)


if __name__ == "__main__":
    unittest.main()
