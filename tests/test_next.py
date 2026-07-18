import unittest
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from fleet_next.model import ServerRef, Session, SessionRef
from fleet_next.protocol import decode, encode
from fleet_next.ui import STATE_ORDER
from fleet_next.tmux import split_key
from fleet_next.actions import agent_command


class IdentityTests(unittest.TestCase):
    def session(self, host, sid="$1"):
        return Session(SessionRef(ServerRef(host, "/tmp/tmux/default", 12, 10), sid),
                       "work", 1, 2, 0, 1, "codex", "waiting", "/work", "tracked")

    def test_identical_tmux_ids_on_different_hosts_are_distinct(self):
        self.assertNotEqual(self.session("newton").ref, self.session("lovelace").ref)

    def test_protocol_round_trip_preserves_canonical_identity(self):
        sessions = [self.session("newton"), self.session("lovelace")]
        self.assertEqual(decode(encode(sessions)), sessions)

    def test_done_is_attention_not_agent_or_lifecycle_state(self):
        session = self.session("newton")
        done = Session(**{**session.__dict__, "attention": "done"})
        self.assertEqual((session.agent, done.agent), ("codex", "codex"))
        self.assertEqual(done.state, "waiting")

    def test_working_sorts_before_waiting_and_done(self):
        self.assertLess(STATE_ORDER["working"], STATE_ORDER["waiting"])

    def test_source_key_contains_server_generation(self):
        session = self.session("newton")
        self.assertEqual(split_key(session.ref.key),
                         ("newton", "/tmp/tmux/default", 12, 10, "$1"))

    def test_new_cli_has_no_destructive_surface(self):
        root = Path(__file__).parents[1]
        paths = [root / "fleet-next", *(root / "fleet_next").glob("*.py")]
        source = "\n".join(path.read_text() for path in paths)
        for command in ("kill-session", "kill-window", "unlink-window"):
            self.assertNotIn(command, source)

    def test_commander_uses_native_codex(self):
        launcher = (Path(__file__).parents[1] / "fleet-commander").read_text()
        self.assertIn('.thread_name == "commander"', launcher)
        self.assertIn("codex --sandbox danger-full-access resume $id", launcher)
        self.assertIn('"$1" = restart', launcher)
        self.assertNotIn("fleet-next commander\"", launcher)

    def test_muster_and_main_route_to_the_lovelace_hub(self):
        root = Path(__file__).parents[1]
        muster = (root / "fleet-muster").read_text()
        main = (root / "fleet-viewer").read_text()
        service = (root / "fleet-next.service").read_text()
        self.assertIn('exec ssh -tt -o BatchMode=yes "$hub" fleet-muster', muster)
        self.assertIn("new-session -d -s fleet@main", main)
        self.assertIn("set-option -t fleet@main prefix None", main)
        self.assertIn("fleet-next viewer-status main", main)
        self.assertIn("ConditionHost=lovelace", service)

    def test_muster_always_opens_the_global_main_viewer(self):
        source = (Path(__file__).parents[1] / "fleet_next/ui.py").read_text()
        self.assertIn("fleet-next show --slot main {1}", source)
        self.assertIn('"--no-sort"', source)

    def test_named_viewers_remain_local(self):
        launcher = (Path(__file__).parents[1] / "fleet-viewer").read_text()
        self.assertTrue(launcher.rstrip().endswith('exec fleet-next viewer --slot "$slot"'))

    def test_management_prompts_never_read_raw_terminal_input(self):
        source = (Path(__file__).parents[1] / "fleet_next/actions.py").read_text()
        self.assertNotRegex(source, r"(?<![A-Za-z_])input\(")
        self.assertIn('"rofi", "-dmenu"', source)

    def test_created_agents_skip_startup_permission_interstitials(self):
        self.assertEqual(agent_command("claude", "work"),
                         ["claude", "--dangerously-skip-permissions", "--name", "work"])
        self.assertEqual(agent_command("codex", "work"),
                         ["codex", "--sandbox", "danger-full-access",
                          "--ask-for-approval", "never"])

    def test_viewer_dismiss_is_an_explicit_clear(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as runtime:
            env = {**os.environ, "XDG_RUNTIME_DIR": runtime,
                   "PYTHONPATH": str(root)}
            process = subprocess.Popen([sys.executable, "-m", "fleet_next.cli",
                                        "viewer", "--slot", "test"], env=env)
            socket = Path(runtime) / "agent-fleet/viewer-test.sock"
            try:
                for _ in range(100):
                    if socket.exists():
                        break
                    time.sleep(.01)
                subprocess.run([sys.executable, "-m", "fleet_next.cli", "dismiss",
                                "--slot", "test"], env=env, check=True)
                code = "from fleet_next.viewer import slots; print(slots())"
                result = subprocess.run([sys.executable, "-c", code], env=env,
                                        text=True, capture_output=True, check=True)
                self.assertEqual(result.stdout.strip(), "[('test', '')]")
            finally:
                process.terminate()
                process.wait()
