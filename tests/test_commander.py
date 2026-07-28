import asyncio
import hashlib
import io
import json
import unittest
from unittest import mock
from types import SimpleNamespace

from agent_fleet.commander import validate_proposal
from agent_fleet import commander_client
from agent_fleet.commander_client import related, render
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

    def test_stalled_observation_does_not_block_snapshot_reply(self):
        async def exercise():
            fleet = Fleet()
            stalled = asyncio.Event()

            async def remote(*_args):
                await stalled.wait()

            fleet.remote_json = remote
            fleet.history_observation = remote
            with mock.patch("agent_fleet.daemon.hosts", return_value=["lovelace"]):
                context = asyncio.create_task(fleet.commander_context())
                await asyncio.sleep(0)
                reader = asyncio.StreamReader()
                reader.feed_data(b"snapshot\n")
                reader.feed_eof()
                writer = mock.Mock()
                writer.drain = mock.AsyncMock()
                await asyncio.wait_for(fleet.reply(reader, writer), .1)
                context.cancel()

            self.assertTrue(writer.write.called)

        asyncio.run(exercise())


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

    def test_mailbox_composition_uses_request_parentage(self):
        root = "llm-actor#7"
        self.assertTrue(related({"id": root}, root))
        self.assertTrue(related({"id": "will#2", "parent": root}, root))
        self.assertTrue(related({"id": "llm-actor#8", "parent": root}, root))
        self.assertFalse(related({"id": "will#3", "parent": "llm-actor#6"}, root))

    def test_response_shapes_render_and_terminate_only_at_the_boundary(self):
        proposal = {"type": "archive", "request_id": "r1",
                    "snapshot_revision": "abc", "source": "source-1"}
        envelopes = [
            {"payload": {"kind": "message", "text": "Explanation."}},
            {"payload": {"kind": "message", "text": json.dumps(proposal)}},
            {"payload": {"kind": "finished"}},
        ]

        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            terminal = [render(envelope, self.request) for envelope in envelopes]

        self.assertEqual(terminal, [False, False, True])
        self.assertIn("Explanation.", stdout.getvalue())
        self.assertIn('"type": "archive"', stdout.getvalue())

        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertTrue(render(
                {"payload": {"kind": "error", "of": "provider", "reason": "failed"}},
                self.request))
        self.assertEqual(stdout.getvalue(), "provider: failed\n")

    def test_exchange_tails_the_socket_peer_identity(self):
        class Thread:
            def __init__(self, target, args, daemon):
                self.args = args

            def start(self):
                output = self.args[2]
                output.put({"id": "socket-peer#0", "parent": "llm-1#0",
                            "payload": {"kind": "finished"}})

        with mock.patch.object(commander_client, "commander_context", return_value=json.dumps({})), \
             mock.patch.object(commander_client.alan, "peer",
                               return_value="socket-peer") as peer, \
             mock.patch.object(commander_client, "current_end",
                               return_value=-1) as current_end, \
             mock.patch.object(commander_client.alan, "commander_request",
                               return_value={"addr": "llm-1",
                                             "envelope_id": "llm-1#0"}) as request, \
             mock.patch.object(commander_client.threading, "Thread", Thread), \
             mock.patch.dict("os.environ", {"USER": "wrong", "LOGNAME": "wrong"}):
            commander_client.exchange("status")

        peer.assert_called_once_with()
        current_end.assert_called_once_with("socket-peer")
        self.assertEqual(request.call_args.args[0]["kind"], "commander_request")


if __name__ == "__main__":
    unittest.main()
