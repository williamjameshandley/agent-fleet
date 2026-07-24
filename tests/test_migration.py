import os
import tempfile
import time
import unittest
import json
import subprocess
import hashlib
from pathlib import Path
from unittest import mock

from libtmux import Server

from fleet_next.migration import adopt_local, migration_id, _rollback_argv
from fleet_next.model import ServerRef, Session, SessionRef
from fleet_next import actions


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.socket_name = f"fleet-migration-{os.getpid()}-{time.time_ns()}"
        self.tmux = Server(socket_name=self.socket_name)
        self.executable = Path(self.temporary.name) / "codex"
        self.executable.symlink_to("/usr/bin/sleep")
        self.session = self.tmux.new_session(
            session_name="legacy", attach=False,
            window_command=f"{self.executable} 1000")
        self.pane = self.session.active_pane
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            argv = Path(f"/proc/{self.pane.pane_pid}/cmdline").read_bytes()
            if b"codex\x00" in argv:
                break
            time.sleep(.01)
        self.host = os.uname().nodename
        source = ServerRef(self.host, self.session.socket_path, int(self.session.pid),
                           int(self.session.start_time))
        self.item = Session(
            SessionRef(source, self.session.session_id), "legacy", 1, 1, 0, 1,
            "codex", "waiting", self.temporary.name, "done", "codex", "waiting",
            transcript_id="thread-1", human_activity=456)
        self.migration_id = migration_id(self.item.ref.key, "thread-1")

    def tearDown(self):
        try:
            self.tmux.kill_server()
        except Exception:
            pass
        self.temporary.cleanup()

    def patches(self):
        status = {"commit": {"payload": {"actor": "codex-new"}},
                  "actor": {"addr": "codex-new"}, "ready": True,
                  "ambiguous": False}
        return (mock.patch("fleet_next.migration.server", return_value=self.tmux),
                mock.patch("fleet_next.migration.inventory", return_value=[self.item]),
                mock.patch("fleet_next.migration.observe", return_value=[self.item]),
                mock.patch("fleet_next.migration.alan_request",
                           return_value={"actors": []}),
                mock.patch("fleet_next.migration.import_native", return_value={
                    "ok": True, "committed": True, "addr": "codex-new"}),
                mock.patch("fleet_next.migration.native_import_status",
                           return_value=status))

    def test_real_tmux_holder_is_removed_only_after_committed_ready_actor(self):
        patches = self.patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4] as imported, \
             patches[5]:
            result = adopt_local(self.item.ref.key, self.migration_id)
        self.assertEqual(result["new_key"], f"alan:{self.host}:codex-new")
        self.assertEqual(result["last_touch"], 456)
        self.assertEqual(result["attention"], "done")
        imported.assert_called_once_with(
            "codex", "thread-1", "legacy", self.temporary.name, "done", 456,
            {"host": self.host, "key": self.item.ref.key,
             "pane_id": self.pane.pane_id, "preserved_session": None,
             "preserved_panes": []}, self.migration_id)
        self.assertFalse(self.tmux.has_session("legacy"))

    def test_rollback_argv_preserves_running_flags_and_adds_exact_resume_identity(self):
        self.assertEqual(
            _rollback_argv(self.pane, "codex", "thread-1"),
            (int(self.pane.pane_pid), [str(self.executable), "1000"],
             [str(self.executable), "1000", "resume", "thread-1"]))

    def test_precommit_failure_restores_exact_provider_argv(self):
        patches = self.patches()
        with patches[0], patches[1], patches[2], patches[3], \
             mock.patch("fleet_next.migration.import_native", return_value={
                 "ok": False, "committed": False, "error": "precommit"}), \
             mock.patch("fleet_next.migration.native_import_status", return_value={
                 "commit": None, "actor": None, "ready": False, "ambiguous": False}), \
             mock.patch("fleet_next.migration.discard_native_import",
                        side_effect=RuntimeError("unknown_import")), \
             mock.patch("fleet_next.migration._rollback_argv",
                        return_value=(int(self.pane.pane_pid),
                                      [str(self.executable), "1000"],
                                      [str(self.executable), "1000"])):
            with self.assertRaisesRegex(RuntimeError, "precommit"):
                adopt_local(self.item.ref.key, self.migration_id)
        self.assertTrue(self.tmux.has_session("legacy"))
        pane = self.tmux.sessions.get(session_name="legacy").active_pane
        deadline = time.monotonic() + 2
        while pane.pane_current_command != "sleep" and time.monotonic() < deadline:
            time.sleep(.01)
        command = Path(f"/proc/{pane.pane_pid}/cmdline").read_bytes()
        self.assertIn(b"codex\x00", command)

    def test_inferred_waiting_fallback_is_not_migratable(self):
        uncertain = Session(**{**self.item.__dict__, "reported_state": ""})
        with mock.patch("fleet_next.migration.server", return_value=self.tmux), \
             mock.patch("fleet_next.migration.inventory", return_value=[uncertain]), \
             mock.patch("fleet_next.migration.observe", return_value=[uncertain]):
            with self.assertRaisesRegex(RuntimeError, "literal waiting state required"):
                adopt_local(uncertain.ref.key, self.migration_id)
        self.assertTrue(self.tmux.has_session("legacy"))

    def test_final_state_revalidation_refuses_a_newly_working_provider(self):
        working = Session(**{**self.item.__dict__, "reported_state": "working"})
        with mock.patch("fleet_next.migration.server", return_value=self.tmux), \
             mock.patch("fleet_next.migration.inventory", return_value=[self.item]), \
             mock.patch("fleet_next.migration.observe",
                        side_effect=[[self.item], [working]]), \
             mock.patch("fleet_next.migration.alan_request", return_value={"actors": []}):
            with self.assertRaisesRegex(RuntimeError, "source changed before cutover"):
                adopt_local(self.item.ref.key, self.migration_id)
        self.assertTrue(self.tmux.has_session("legacy"))
        self.assertEqual(self.tmux.sessions.get(
            session_name="legacy").active_pane.pane_current_command, "codex")

    def test_final_revalidation_refuses_a_changed_foreground_process(self):
        patches = self.patches()
        current_pid, current_argv, rollback = _rollback_argv(
            self.pane, "codex", "thread-1")
        with patches[0], patches[1], patches[2], patches[3], \
             mock.patch("fleet_next.migration._rollback_argv", return_value=(
                 current_pid + 1, current_argv, rollback)):
            with self.assertRaisesRegex(RuntimeError, "provider process changed"):
                adopt_local(self.item.ref.key, self.migration_id)
        self.assertEqual(self.tmux.sessions.get(
            session_name="legacy").active_pane.pane_current_command, "codex")

    def test_holder_entry_failure_rolls_back_exact_provider(self):
        patches = self.patches()

        def fail_after_holder(tmux, pane_id, _command):
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                current = tmux.cmd("display-message", "-p", "-t", pane_id,
                                   "#{pane_current_command}").stdout[0]
                if current == "sleep":
                    break
                time.sleep(.01)
            raise RuntimeError("holder failed")

        with patches[0], patches[1], patches[2], patches[3], \
             mock.patch("fleet_next.migration._wait_command",
                        side_effect=fail_after_holder), \
             mock.patch("fleet_next.migration._rollback_argv",
                        return_value=(int(self.pane.pane_pid),
                                      [str(self.executable), "1000"],
                                      [str(self.executable), "1000"])):
            with self.assertRaisesRegex(RuntimeError, "holder failed"):
                adopt_local(self.item.ref.key, self.migration_id)
        deadline = time.monotonic() + 2
        command = b""
        while time.monotonic() < deadline:
            pane_pid = self.tmux.cmd("display-message", "-p", "-t", "legacy",
                                     "#{pane_pid}").stdout[0]
            command = Path(f"/proc/{pane_pid}/cmdline").read_bytes()
            if b"codex\x00" in command:
                break
            time.sleep(.01)
        self.assertIn(b"codex\x00", command)

    def test_actual_failed_holder_exec_keeps_pane_and_rolls_back_provider(self):
        import fleet_next.migration as migration
        patches = self.patches()
        original = migration._respawn

        def fail_holder(tmux, pane, condition, cwd, argv):
            if argv == ["sleep", "infinity"]:
                return original(tmux, pane, condition, cwd, ["/does/not/exist"])
            return original(tmux, pane, condition, cwd, argv)

        with patches[0], patches[1], patches[2], patches[3], \
             mock.patch("fleet_next.migration._respawn", side_effect=fail_holder), \
             mock.patch("fleet_next.migration._rollback_argv", return_value=(
                 int(self.pane.pane_pid), [str(self.executable), "1000"],
                 [str(self.executable), "1000"])):
            with self.assertRaisesRegex(RuntimeError, "pane died"):
                adopt_local(self.item.ref.key, self.migration_id)
        deadline = time.monotonic() + 2
        command = b""
        while time.monotonic() < deadline:
            pane_pid = self.tmux.cmd("display-message", "-p", "-t", "legacy",
                                     "#{pane_pid}").stdout[0]
            command = Path(f"/proc/{pane_pid}/cmdline").read_bytes()
            if b"codex\x00" in command:
                break
            time.sleep(.01)
        self.assertIn(b"codex\x00", command)

    def test_holder_rollback_refuses_a_raced_replacement(self):
        patches = self.patches()

        def replace_holder(_tmux, pane_id, _command):
            self.tmux.cmd("respawn-pane", "-k", "-t", pane_id, "tail -f /dev/null")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                current = self.tmux.cmd("display-message", "-p", "-t", pane_id,
                                        "#{pane_current_command}").stdout[0]
                if current == "tail":
                    break
                time.sleep(.01)
            raise RuntimeError("holder observation failed")

        with patches[0], patches[1], patches[2], patches[3], \
             mock.patch("fleet_next.migration._wait_command",
                        side_effect=replace_holder):
            with self.assertRaisesRegex(RuntimeError,
                                        "neutral holder identity could not be established"):
                adopt_local(self.item.ref.key, self.migration_id)
        pane = self.tmux.sessions.get(session_name="legacy").active_pane
        self.assertEqual(pane.pane_current_command, "tail")

    def test_tracked_attention_and_last_touch_reach_the_commit_boundary(self):
        item = Session(**{**self.item.__dict__, "attention": "tracked"})
        patches = self.patches()
        with patches[0], mock.patch("fleet_next.migration.inventory", return_value=[item]), \
             mock.patch("fleet_next.migration.observe", return_value=[item]), patches[3], \
             patches[4] as imported, patches[5]:
            adopt_local(item.ref.key, self.migration_id)
        self.assertEqual(imported.call_args.args[4:6], ("tracked", 456))

    def test_lost_import_reply_reconciles_committed_ready_actor(self):
        patches = self.patches()
        with patches[0], patches[1], patches[2], patches[3], \
             mock.patch("fleet_next.migration.import_native", side_effect=OSError), \
             patches[5]:
            outcome = adopt_local(self.item.ref.key, self.migration_id)
        self.assertEqual(outcome["actor"], "codex-new")
        self.assertFalse(self.tmux.has_session("legacy"))

    def test_committed_provider_failure_repairs_forward_without_legacy_restart(self):
        patches = self.patches()
        not_ready = {"commit": {"payload": {"actor": "codex-new"}},
                     "actor": {"addr": "codex-new"}, "ready": False,
                     "ambiguous": False}
        ready = {**not_ready, "ready": True}
        with patches[0], patches[1], patches[2], \
             mock.patch("fleet_next.migration.alan_request", side_effect=[
                 {"actors": []}, {"addr": "codex-new"}]) as alan, \
             mock.patch("fleet_next.migration.import_native", return_value={
                 "ok": False, "committed": True, "addr": "codex-new",
                 "error": "provider_not_ready"}), \
             mock.patch("fleet_next.migration.native_import_status",
                        side_effect=[not_ready, ready, ready]):
            outcome = adopt_local(self.item.ref.key, self.migration_id)
        self.assertEqual(outcome["actor"], "codex-new")
        self.assertEqual(alan.call_args_list[-1], mock.call({
            "op": "spawn", "source": "codex-new"}))
        self.assertFalse(self.tmux.has_session("legacy"))

    def test_provider_death_after_successful_reply_repairs_forward(self):
        patches = self.patches()
        not_ready = {"commit": {"payload": {"actor": "codex-new"}},
                     "actor": {"addr": "codex-new"}, "ready": False,
                     "ambiguous": False}
        ready = {**not_ready, "ready": True}
        with patches[0], patches[1], patches[2], \
             mock.patch("fleet_next.migration.alan_request", side_effect=[
                 {"actors": []}, {"addr": "codex-new"}]) as alan, \
             mock.patch("fleet_next.migration.import_native", return_value={
                 "ok": True, "committed": True, "addr": "codex-new"}), \
             mock.patch("fleet_next.migration.native_import_status",
                        side_effect=[not_ready, not_ready, ready]):
            outcome = adopt_local(self.item.ref.key, self.migration_id)
        self.assertEqual(outcome["actor"], "codex-new")
        self.assertEqual(alan.call_args_list[-1], mock.call({
            "op": "spawn", "source": "codex-new"}))

    def test_committed_failure_is_reconciled_from_the_neutral_holder_on_rerun(self):
        patches = self.patches()
        with patches[0], patches[1], patches[2], patches[3], \
             mock.patch("fleet_next.migration.import_native", return_value={
                 "ok": True, "committed": True, "addr": "codex-new"}), \
             mock.patch("fleet_next.migration.native_import_status",
                        side_effect=OSError("status unavailable")):
            with self.assertRaises(OSError):
                adopt_local(self.item.ref.key, self.migration_id)
        self.assertEqual(self.tmux.sessions.get(
            session_name="legacy").active_pane.pane_current_command, "sleep")
        holder = Session(**{**self.item.__dict__, "agent_name": "shell",
                            "reported_state": "", "transcript_id": "",
                            "command": "sleep"})
        ready = {"commit": {"payload": {"actor": "codex-new"}},
                 "actor": {"addr": "codex-new"}, "ready": True,
                 "ambiguous": False}
        with mock.patch("fleet_next.migration.server", return_value=self.tmux), \
             mock.patch("fleet_next.migration.inventory", return_value=[holder]), \
             mock.patch("fleet_next.migration.observe", return_value=[holder]), \
             mock.patch("fleet_next.migration.native_import_status", return_value=ready):
            outcome = adopt_local(self.item.ref.key, self.migration_id,
                                  "codex", "thread-1")
        self.assertEqual(outcome["new_key"], f"alan:{self.host}:codex-new")
        self.assertFalse(self.tmux.has_session("legacy"))

    def test_holder_cleanup_failure_never_restarts_committed_legacy_provider(self):
        patches = self.patches()
        original = self.tmux.cmd

        def command(*arguments):
            if any("kill-session" in str(argument) for argument in arguments):
                return mock.Mock(stdout=["FLEET_STALE"])
            return original(*arguments)

        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
             mock.patch.object(self.tmux, "cmd", side_effect=command):
            outcome = adopt_local(self.item.ref.key, self.migration_id)
        self.assertEqual(outcome["new_key"], f"alan:{self.host}:codex-new")
        self.assertEqual(outcome["cleanup_error"], "legacy holder cleanup failed")
        self.assertTrue(self.tmux.has_session("legacy"))
        pane = self.tmux.sessions.get(session_name="legacy").active_pane
        self.assertEqual(pane.pane_current_command, "sleep")

    def test_non_agent_windows_and_panes_keep_their_processes(self):
        self.tmux.cmd("split-window", "-d", "-t", self.pane.pane_id, "sleep 1000")
        self.tmux.cmd("new-window", "-d", "-t", "legacy", "sleep 1000")
        before = {line.split("\t")[0]: int(line.split("\t")[1])
                  for line in self.tmux.cmd("list-panes", "-t", "legacy", "-a", "-F",
                                            "#{pane_id}\t#{pane_pid}").stdout
                  if line.split("\t")[0] != self.pane.pane_id}
        item = Session(**{**self.item.__dict__, "windows": 2})
        status = {"commit": {"payload": {"actor": "codex-new"}},
                  "actor": {"addr": "codex-new"}, "ready": True,
                  "ambiguous": False}
        with mock.patch("fleet_next.migration.server", return_value=self.tmux), \
             mock.patch("fleet_next.migration.inventory", return_value=[item]), \
             mock.patch("fleet_next.migration.observe", return_value=[item]), \
             mock.patch("fleet_next.migration.alan_request", return_value={"actors": []}), \
             mock.patch("fleet_next.migration.import_native", return_value={
                 "ok": True, "committed": True, "addr": "codex-new"}), \
             mock.patch("fleet_next.migration.native_import_status",
                        return_value=status):
            outcome = adopt_local(item.ref.key, self.migration_id)
        auxiliary = outcome["preserved_session"]
        self.assertTrue(self.tmux.has_session(auxiliary))
        after = {line.split("\t")[1]: (line.split("\t")[0], int(line.split("\t")[2]))
                 for line in self.tmux.cmd("list-panes", "-a", "-F",
                                           "#{session_name}\t#{pane_id}\t#{pane_pid}").stdout}
        for pane_id, pane_pid in before.items():
            self.assertEqual(after[pane_id], (auxiliary, pane_pid))

    def test_working_revalidation_precedes_multi_pane_topology_mutation(self):
        self.tmux.cmd("split-window", "-d", "-t", self.pane.pane_id, "sleep 1000")
        self.tmux.cmd("new-window", "-d", "-t", "legacy", "sleep 1000")
        before = self.tmux.cmd("list-panes", "-t", "legacy", "-a", "-F",
                               "#{window_id}\t#{pane_id}\t#{pane_pid}").stdout
        item = Session(**{**self.item.__dict__, "windows": 2})
        working = Session(**{**item.__dict__, "reported_state": "working"})
        with mock.patch("fleet_next.migration.server", return_value=self.tmux), \
             mock.patch("fleet_next.migration.inventory", return_value=[item]), \
             mock.patch("fleet_next.migration.observe",
                        side_effect=[[item], [working]]), \
             mock.patch("fleet_next.migration.alan_request", return_value={"actors": []}):
            with self.assertRaisesRegex(RuntimeError, "source changed before cutover"):
                adopt_local(item.ref.key, self.migration_id)
        self.assertEqual([session.session_name for session in self.tmux.sessions], ["legacy"])
        self.assertEqual(
            self.tmux.cmd("list-panes", "-t", "legacy", "-a", "-F",
                          "#{window_id}\t#{pane_id}\t#{pane_pid}").stdout,
            before)


class ManifestTests(unittest.TestCase):
    def test_cleanup_retry_uses_manifest_native_identity_through_projection(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "native-session-migration"
            directory.mkdir()
            source = ServerRef("lovelace", "/tmp/tmux/default", 1, 2)
            holder = Session(SessionRef(source, "$1"), "legacy", 1, 1, 0, 1,
                             "sleep", "", "/tmp", "tracked")
            record = {"key": holder.ref.key, "host": "lovelace",
                      "provider": "codex", "native_id": "thread-1",
                      "name": "legacy", "cwd": "/tmp", "attention": "tracked",
                      "last_touch": 456, "reported_state": "waiting", "windows": 1}
            manifest = {"captured": 1, "hosts": ["lovelace"], "sessions": [record]}
            raw = actions._canonical(manifest)
            entry = hashlib.sha256(actions._canonical(record)).hexdigest()
            wanted = migration_id(holder.ref.key, "thread-1")
            (directory / "manifest.json").write_bytes(raw)
            (directory / "manifest.sha256").write_text(
                hashlib.sha256(raw).hexdigest() + "\n")
            (directory / "outcomes.jsonl").write_text(json.dumps({
                "key": holder.ref.key, "migration_id": wanted,
                "manifest_entry": entry, "status": "migrated",
                "new_key": "alan:lovelace:codex-new",
                "cleanup_error": "legacy holder cleanup failed"}) + "\n")
            completed = subprocess.CompletedProcess([], 0, stdout=json.dumps({
                "new_key": "alan:lovelace:codex-new",
                "cleanup_error": "legacy holder cleanup failed"}))
            with mock.patch.object(actions, "RUNTIME", Path(temporary)), \
                 mock.patch("fleet_next.actions.decode_message",
                            return_value=([holder], {}, [])), \
                 mock.patch("fleet_next.actions.snapshot", return_value="ignored"), \
                 mock.patch("fleet_next.actions.find", return_value=holder), \
                 mock.patch("fleet_next.actions.viewer.slots", return_value=[]), \
                 mock.patch("fleet_next.actions.host_command",
                            return_value=completed) as owner, \
                 mock.patch("fleet_next.actions.wait_for_projection",
                            side_effect=RuntimeError("not projected")) as projection:
                with self.assertRaises(SystemExit):
                    actions.converge()
            owner.assert_called_once_with(
                "lovelace", "fleet-next", "adopt-local", holder.ref.key,
                wanted, "codex", "thread-1", capture_output=True)
            projection.assert_called_once_with(
                "alan:lovelace:codex-new", "thread-1")
            outcome = json.loads((directory / "outcomes.jsonl").read_text().splitlines()[-1])
            self.assertIn("projection_error", outcome)
            self.assertEqual(outcome["cleanup_error"], "legacy holder cleanup failed")
            with mock.patch.object(actions, "RUNTIME", Path(temporary)), \
                 mock.patch("fleet_next.actions.decode_message",
                            return_value=([holder], {}, [])), \
                 mock.patch("fleet_next.actions.snapshot", return_value="ignored"), \
                 mock.patch("fleet_next.actions.adopt", return_value={
                     "new_key": "alan:lovelace:codex-new"}) as owner_again:
                actions.converge()
            owner_again.assert_called_once_with(holder.ref.key, record)

    def test_viewer_handoff_failure_does_not_repeat_owner_cutover(self):
        source = ServerRef("lovelace", "/tmp/tmux/default", 1, 2)
        item = Session(SessionRef(source, "$1"), "legacy", 1, 1, 0, 1,
                       "codex", "waiting", "/tmp", "tracked", "codex", "waiting",
                       transcript_id="thread-1", human_activity=456)
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps({
            "new_key": "alan:lovelace:codex-new"}))
        with mock.patch("fleet_next.actions.find", return_value=item), \
             mock.patch("fleet_next.actions.viewer.slots", return_value=[
                 ("main", item.ref.key)]), \
             mock.patch("fleet_next.actions.host_command", return_value=completed) as owner, \
             mock.patch("fleet_next.actions.wait_for_projection"), \
             mock.patch("fleet_next.actions.viewer.request",
                        side_effect=subprocess.CalledProcessError(
                            1, ["tmux", "list-clients"])):
            with self.assertRaisesRegex(RuntimeError, "viewer handoff failed") as raised:
                actions.adopt(item.ref.key)
        self.assertEqual(raised.exception.outcome["new_key"],
                         "alan:lovelace:codex-new")
        owner.assert_called_once_with(
            "lovelace", "fleet-next", "adopt-local", item.ref.key,
            migration_id(item.ref.key, "thread-1"), "codex", "thread-1",
            capture_output=True)

    def test_manifest_native_divergence_never_reaches_owner_host(self):
        source = ServerRef("lovelace", "/tmp/tmux/default", 1, 2)
        item = Session(SessionRef(source, "$1"), "legacy", 1, 1, 0, 1,
                       "codex", "waiting", "/tmp", "tracked", "codex", "waiting",
                       transcript_id="new-thread")
        expected = {"provider": "codex", "native_id": "old-thread"}
        with mock.patch("fleet_next.actions.find", return_value=item), \
             mock.patch("fleet_next.actions.host_command") as owner:
            with self.assertRaisesRegex(RuntimeError, "differs from migration manifest"):
                actions.adopt(item.ref.key, expected)
        owner.assert_not_called()

    def test_projection_failure_preserves_committed_outcome(self):
        source = ServerRef("lovelace", "/tmp/tmux/default", 1, 2)
        item = Session(SessionRef(source, "$1"), "legacy", 1, 1, 0, 1,
                       "codex", "waiting", "/tmp", "tracked", "codex", "waiting",
                       transcript_id="thread-1")
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps({
            "new_key": "alan:lovelace:codex-new"}))
        with mock.patch("fleet_next.actions.find", return_value=item), \
             mock.patch("fleet_next.actions.viewer.slots", return_value=[]), \
             mock.patch("fleet_next.actions.host_command", return_value=completed), \
             mock.patch("fleet_next.actions.wait_for_projection",
                        side_effect=RuntimeError("not projected")):
            with self.assertRaises(actions.ProjectionHandoffError) as raised:
                actions.adopt(item.ref.key)
        self.assertEqual(raised.exception.outcome["new_key"],
                         "alan:lovelace:codex-new")

    def test_manifest_requires_every_host_and_is_digest_protected(self):
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(actions, "RUNTIME", Path(temporary)), \
             mock.patch("fleet_next.actions.hosts", return_value=["one", "two"]), \
             mock.patch("fleet_next.actions.host_command", side_effect=[
                 subprocess.CompletedProcess([], 0, stdout=json.dumps([{
                     "key": "one:key", "host": "one", "provider": "codex",
                     "native_id": "thread-1", "name": "one", "cwd": "/tmp",
                     "attention": "tracked", "last_touch": 456,
                     "reported_state": "waiting", "windows": 1}])),
                 subprocess.CompletedProcess([], 0, stdout="[]")]) as command:
            manifest = actions._load_or_capture_manifest()
            self.assertEqual(manifest["hosts"], ["one", "two"])
            self.assertEqual(manifest["sessions"][0]["last_touch"], 456)
            self.assertEqual(command.call_count, 2)
            path = Path(temporary) / "native-session-migration/manifest.json"
            path.write_text(path.read_text() + " ")
            with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
                actions._load_or_capture_manifest()

    def test_unavailable_host_creates_no_partial_manifest(self):
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(actions, "RUNTIME", Path(temporary)), \
             mock.patch("fleet_next.actions.hosts", return_value=["one", "two"]), \
             mock.patch("fleet_next.actions.host_command", side_effect=[
                 subprocess.CompletedProcess([], 0, stdout="[]"),
                 subprocess.CalledProcessError(255, ["ssh", "two"]) ]):
            with self.assertRaises(subprocess.CalledProcessError):
                actions._load_or_capture_manifest()
            self.assertFalse((Path(temporary) /
                              "native-session-migration/manifest.json").exists())

    def test_convergence_uses_live_state_not_frozen_manifest_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "native-session-migration"
            directory.mkdir()
            source = ServerRef("lovelace", "/tmp/tmux/default", 1, 2)
            live = Session(SessionRef(source, "$1"), "legacy", 1, 1, 0, 1,
                           "codex", "waiting", "/tmp", "tracked", "codex", "waiting",
                           transcript_id="thread-1", human_activity=789)
            record = {"key": live.ref.key, "host": "lovelace",
                      "provider": "codex", "native_id": "thread-1",
                      "name": "legacy", "cwd": "/tmp", "attention": "tracked",
                      "last_touch": 456, "reported_state": "working", "windows": 1}
            manifest = {"captured": 1, "hosts": ["lovelace"], "sessions": [record]}
            raw = actions._canonical(manifest)
            (directory / "manifest.json").write_bytes(raw)
            (directory / "manifest.sha256").write_text(
                hashlib.sha256(raw).hexdigest() + "\n")
            (directory / "outcomes.jsonl").write_text(json.dumps({
                "key": live.ref.key,
                "migration_id": migration_id(live.ref.key, "thread-1"),
                "manifest_entry": "wrong-entry", "status": "migrated",
                "new_key": "alan:lovelace:codex-wrong"}) + "\n")
            with mock.patch.object(actions, "RUNTIME", Path(temporary)), \
                 mock.patch("fleet_next.actions.decode_message",
                            return_value=([live], {}, [])), \
                 mock.patch("fleet_next.actions.snapshot", return_value="ignored"), \
                 mock.patch("fleet_next.actions.adopt", return_value={
                     "new_key": "alan:lovelace:codex-new"}) as adopt:
                actions.converge()
            adopt.assert_called_once_with(live.ref.key, record)
            outcome = json.loads((directory / "outcomes.jsonl").read_text().splitlines()[-1])
            self.assertEqual(outcome["status"], "migrated")
            (directory / "outcomes.jsonl").unlink()
            projection = actions.ProjectionHandoffError(
                {"new_key": "alan:lovelace:codex-new"}, "not projected", ["main"])
            with mock.patch.object(actions, "RUNTIME", Path(temporary)), \
                 mock.patch("fleet_next.actions.decode_message",
                            return_value=([live], {}, [])), \
                 mock.patch("fleet_next.actions.snapshot", return_value="ignored"), \
                 mock.patch("fleet_next.actions.adopt", side_effect=projection):
                with self.assertRaises(SystemExit):
                    actions.converge()
            outcome = json.loads((directory / "outcomes.jsonl").read_text())
            self.assertEqual(outcome["status"], "migrated")
            self.assertEqual(outcome["new_key"], "alan:lovelace:codex-new")
            self.assertIn("projection_error", outcome)
            with mock.patch.object(actions, "RUNTIME", Path(temporary)), \
                 mock.patch("fleet_next.actions.decode_message",
                            return_value=([live], {}, [])), \
                 mock.patch("fleet_next.actions.snapshot", return_value="ignored"), \
                 mock.patch("fleet_next.actions.wait_for_projection") as projection_retry, \
                 mock.patch("fleet_next.actions.viewer.request", side_effect=
                            subprocess.CalledProcessError(
                                1, ["tmux", "list-clients"])) as viewer_retry, \
                 mock.patch("fleet_next.actions.adopt") as owner:
                with self.assertRaises(SystemExit):
                    actions.converge()
            owner.assert_not_called()
            projection_retry.assert_called_once_with(
                "alan:lovelace:codex-new", "thread-1")
            viewer_retry.assert_called_once_with("main", "alan:lovelace:codex-new")
            outcome = json.loads((directory / "outcomes.jsonl").read_text().splitlines()[-1])
            self.assertNotIn("projection_error", outcome)
            self.assertIn("viewer_error", outcome)
            self.assertEqual(outcome["new_key"], "alan:lovelace:codex-new")


if __name__ == "__main__":
    unittest.main()
