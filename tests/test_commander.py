import asyncio
import hashlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import networkx as nx

from agent_fleet import alan
from agent_fleet.commander import validate_proposal
from agent_fleet import commander_client
from agent_fleet.commander_client import render
from agent_fleet.daemon import Fleet
from agent_fleet.protocol import encode


class CommanderContextTests(unittest.TestCase):
    def test_history_queries_only_available_source_hosts(self):
        fleet = Fleet()
        fleet.unavailable = {"noether"}
        fleet.sessions = {}
        fleet.history_observation = mock.AsyncMock(return_value={
            "host": "lovelace", "actors": [], "transcripts": [],
        })
        with mock.patch("agent_fleet.daemon.hosts",
                        return_value=["lovelace", "noether"]):
            self.assertEqual(asyncio.run(fleet.history()), [])
        fleet.history_observation.assert_awaited_once_with("lovelace")

    def test_one_disconnected_history_host_does_not_erase_other_hosts(self):
        fleet = Fleet()
        fleet.unavailable = set()
        fleet.sessions = {}

        async def history(host):
            if host == "noether":
                raise RuntimeError("connection closed")
            return {"host": host, "actors": [], "transcripts": [{
                "agent": "codex", "session_id": "thread", "mtime": 1,
                "cwd": "/work", "name": "retained",
            }]}

        fleet.history_observation = history
        with mock.patch("agent_fleet.daemon.hosts",
                        return_value=["lovelace", "noether"]):
            self.assertEqual(asyncio.run(fleet.history()), [{
                "key": "lovelace:codex:thread", "host": "lovelace",
                "agent": "codex", "name": "retained", "cwd": "/work",
                "mtime": 1,
            }])

    def context(self, unavailable=(), profile_suffix="", transcript_name="old", sessions=()):
        fleet = Fleet()
        fleet.unavailable = set(unavailable)
        fleet.sessions = {"lovelace": list(sessions)}

        async def remote(host, *command):
            if "agent_fleet.actions import context" in command[2]:
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
            human_activity=2, transcript_id="live-thread", worked=True)
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

    def test_history_uses_no_alan_query_when_the_graph_is_absent(self):
        fleet = Fleet()
        fleet.graphs = {"turing": None}
        fleet.observed = 1

        async def remote(host, *command):
            self.assertEqual(host, "turing")
            self.assertIn("agent_fleet.transcripts import history", command[2])
            return [{"agent": "codex", "session_id": "thread-1"}]

        fleet.remote_json = remote
        self.assertEqual(asyncio.run(fleet.history_observation("turing")), {
            "host": "turing", "actors": [],
            "transcripts": [{"agent": "codex", "session_id": "thread-1"}],
        })

    def test_history_retains_bare_language_actor_and_native_actor_authority(self):
        identity = "00000000-0000-4000-8000-000000000001"
        observations = [{"actors": [
            {"addr": "llm-review@lovelace", "kind": "llm", "state": "retired",
             "label": "review", "cwd": "/work", "created": 2,
             "human_activity": 3},
            {"addr": f"codex-{identity}@lovelace", "kind": "codex", "state": "retired",
             "label": "work", "cwd": "/work", "created": 2,
             "human_activity": 4, "native_id": "persisted-native-id"},
        ], "transcripts": [{
            "agent": "codex", "session_id": identity, "mtime": 4,
            "name": "duplicate", "cwd": "/work",
        }]}]
        history = Fleet.history_entries([], ["newton"], observations)
        self.assertEqual([item["key"] for item in history], [
            f"alan:codex-{identity}@lovelace", "alan:llm-review@lovelace"])

    def test_mdjudge_search_joins_exact_retired_tablet_actor(self):
        identity = "00000000-0000-4000-8000-000000000001"
        actor = f"codex-{identity}@lovelace"
        fleet = Fleet()
        fleet.unavailable = set()

        async def observation(host, query):
            self.assertEqual((host, query), ("lovelace", "mdjudge"))
            return {"actors": [{
                "addr": actor, "kind": "codex", "state": "retired",
                "label": "tablet", "cwd": "/work",
            }], "hits": [{
                "agent": "codex", "session_id": identity,
                "path": "/native/rollout.jsonl", "line": 9, "role": "user",
                "cwd": "/work", "text": "build mdjudge",
            }]}

        fleet.search_observation = observation
        with mock.patch("agent_fleet.daemon.hosts", return_value=["lovelace"]):
            [result] = asyncio.run(fleet.search_history("mdjudge"))
        self.assertEqual(result["source"], f"alan:{actor}")
        self.assertEqual(result["name"], "tablet")
        self.assertEqual(result["lifecycle"], "retired")

    def test_unowned_search_hit_remains_standalone_provider_history(self):
        fleet = Fleet()
        fleet.unavailable = set()
        fleet.search_observation = mock.AsyncMock(return_value={
            "actors": [], "hits": [{
                "agent": "claude", "session_id": "full-id", "path": "/native/a.jsonl",
                "line": 2, "role": "assistant", "cwd": "/work", "text": "topic",
            }]})
        with mock.patch("agent_fleet.daemon.hosts", return_value=["lovelace"]):
            [result] = asyncio.run(fleet.search_history("topic"))
        self.assertEqual(result["source"], "lovelace:claude:full-id")
        self.assertEqual(result["lifecycle"], "standalone")

    def test_search_uses_only_available_source_corpora(self):
        fleet = Fleet()
        fleet.unavailable = {"noether"}
        fleet.search_observation = mock.AsyncMock(return_value={
            "actors": [], "hits": [],
        })
        with mock.patch("agent_fleet.daemon.hosts",
                        return_value=["lovelace", "noether"]):
            self.assertEqual(asyncio.run(fleet.search_history("topic")), [])
        fleet.search_observation.assert_awaited_once_with("lovelace", "topic")

    def test_multiple_actors_claiming_search_identity_fail_visibly(self):
        fleet = Fleet()
        fleet.unavailable = set()
        identity = "00000000-0000-4000-8000-000000000001"
        fleet.search_observation = mock.AsyncMock(return_value={
            "actors": [
                {"addr": f"codex-{identity}@lovelace", "kind": "codex"},
                {"addr": f"codex-{identity}@newton", "kind": "codex"},
            ], "hits": [{"agent": "codex", "session_id": identity,
                          "path": "/native/a", "line": 1, "role": "user",
                          "cwd": "/work", "text": "topic"}],
        })
        with mock.patch("agent_fleet.daemon.hosts", return_value=["lovelace"]), \
             self.assertRaisesRegex(RuntimeError, "ambiguous codex transcript ownership"):
            asyncio.run(fleet.search_history("topic"))


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

    def test_accepts_archive_of_retained_alan_bare_language_actor(self):
        self.request["snapshot"]["sessions"].append({
            "source": "alan:llm-review@newton", "agent": "llm",
            "transcript_id": "",
        })
        proposal = {"type": "archive", "request_id": "r1",
                    "snapshot_revision": "abc",
                    "source": "alan:llm-review@newton"}
        self.assertIs(validate_proposal(proposal, self.request), proposal)

    def test_accepts_archive_of_a_pristine_workless_native_session(self):
        self.request["snapshot"]["sessions"].append({
            "source": "alan:claude-fresh@lovelace", "agent": "claude",
            "transcript_id": "", "worked": False,
        })
        proposal = {"type": "archive", "request_id": "r1",
                    "snapshot_revision": "abc",
                    "source": "alan:claude-fresh@lovelace"}
        self.assertIs(validate_proposal(proposal, self.request), proposal)

    def test_rejects_archive_of_a_worked_session_without_a_transcript(self):
        self.request["snapshot"]["sessions"].append({
            "source": "alan:claude-busy@lovelace", "agent": "claude",
            "transcript_id": "", "worked": True,
        })
        proposal = {"type": "archive", "request_id": "r1",
                    "snapshot_revision": "abc",
                    "source": "alan:claude-busy@lovelace"}
        with self.assertRaises(ValueError):
            validate_proposal(proposal, self.request)

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

    def test_output_renders_only_a_validated_proposal(self):
        proposal = {"type": "archive", "request_id": "r1",
                    "snapshot_revision": "abc", "source": "source-1"}
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            render({"status": "ok", "value": json.dumps(proposal)}, self.request)
        self.assertIn('"type": "archive"', stdout.getvalue())

        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            render({"status": "error", "error": "provider failed"}, self.request)
        self.assertEqual(stdout.getvalue(), "provider failed\n")

    def test_exchange_uses_send_and_observation(self):
        with mock.patch.object(commander_client, "commander_context", return_value=json.dumps({})), \
             mock.patch.object(commander_client.alan, "commander_actor",
                               return_value="llm-1@newton"), \
             mock.patch.object(commander_client.loop, "send",
                               return_value={"input": "llm-1@newton#1"}) as send, \
             mock.patch.object(commander_client.alan, "wait_output",
                               return_value={"status": "ok", "value": "done"}), \
             mock.patch.object(commander_client, "render") as render_output:
            commander_client.exchange("status")

        self.assertEqual(send.call_args.args[0], "llm-1@newton")
        self.assertEqual(send.call_args.args[1]["kind"], "prompt")
        render_output.assert_called_once()

class CommanderActorTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(
            "os.environ", {"XDG_STATE_HOME": self.tempdir.name}
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.tempdir.cleanup()

    @staticmethod
    def graph(*actors):
        graph = nx.MultiDiGraph()
        graph.graph["actors"] = list(actors)
        return graph

    class Stream:
        def __init__(self, graph):
            self.graph = graph

        def __iter__(self):
            return iter((self.graph,))

        def __next__(self):
            graph, self.graph = self.graph, None
            if graph is None:
                raise StopIteration
            return graph

        def close(self):
            pass

    @classmethod
    def stream(cls, graph):
        return cls.Stream(graph)

    def test_first_request_creates_and_reuses_an_ordinary_actor(self):
        with mock.patch.object(alan.loop, "observe",
                               return_value=self.stream(self.graph())), \
             mock.patch.object(alan.loop, "spawn",
                               return_value="llm-commander@newton") as spawn:
            self.assertEqual(alan.commander_actor(), "llm-commander@newton")
        spawn.assert_called_once_with({"kind": "llm", "preset": "commander"})

        existing = {"addr": "llm-commander@newton", "preset": "commander"}
        with mock.patch.object(alan.loop, "observe",
                               return_value=self.stream(self.graph(existing))), \
             mock.patch.object(alan.loop, "spawn") as spawn:
            self.assertEqual(alan.commander_actor(), "llm-commander@newton")
        spawn.assert_not_called()

    def test_a_retired_commander_is_not_treated_as_live(self):
        retired = {"addr": "llm-old@newton", "preset": "commander",
                   "state": "retired"}
        with mock.patch.object(alan.loop, "observe",
                               return_value=self.stream(self.graph(retired))), \
             mock.patch.object(alan.loop, "spawn",
                               return_value="llm-fresh@newton") as spawn:
            self.assertEqual(alan.commander_actor(), "llm-fresh@newton")
        spawn.assert_called_once_with({"kind": "llm", "preset": "commander"})

    def test_multiple_commanders_fail_visibly(self):
        current = self.graph(
            {"addr": "llm-first@newton", "preset": "commander"},
            {"addr": "llm-second@newton", "preset": "commander"},
        )

        with mock.patch.object(alan.loop, "observe",
                               return_value=self.stream(current)):
            with self.assertRaisesRegex(RuntimeError, "multiple Commander actors"):
                alan.commander_actor()

    def test_concurrent_first_use_creates_one_actor(self):
        creating = threading.Event()
        release = threading.Event()
        results = []
        created = []

        def spawn(*_args, **_kwargs):
            creating.set()
            release.wait()
            created.append(True)
            return "llm-commander@newton"

        def observe(**kwargs):
            self.assertEqual(kwargs, {"stream": True, "actors": True})
            actors = ([{"addr": "llm-commander@newton", "preset": "commander"}]
                      if created else [])
            return self.stream(self.graph(*actors))

        def resolve():
            results.append(alan.commander_actor())

        with mock.patch.object(alan.loop, "observe", side_effect=observe), \
             mock.patch.object(alan.loop, "spawn", side_effect=spawn) as create:
            first = threading.Thread(target=resolve)
            second = threading.Thread(target=resolve)
            first.start()
            self.assertTrue(creating.wait(1))
            second.start()
            release.set()
            first.join(1)
            second.join(1)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(results, ["llm-commander@newton", "llm-commander@newton"])
        create.assert_called_once_with({"kind": "llm", "preset": "commander"})


if __name__ == "__main__":
    unittest.main()
