import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alan_composer.archive import Archive
from alan_composer import destination
from alan_composer.events import EventServer
from alan_composer import i3
from alan_composer.model import Composition, Mode


class VoiceModelTests(unittest.TestCase):
    def test_draft_and_pause(self):
        composition = Composition().append("first").append("second")
        self.assertEqual(composition.draft, "first second")
        self.assertEqual(composition.pause().mode, Mode.PAUSED)
        self.assertEqual(composition.pause().resume().mode, Mode.RECORDING)

    def test_archive_is_append_only_jsonl(self):
        with tempfile.TemporaryDirectory() as root:
            archive = Archive(root)
            composition = Composition()
            archive.record(composition, "opened")
            archive.record(composition, "cancelled", draft="recover me")
            self.assertEqual(len(archive.events.read_text().splitlines()), 2)
            self.assertEqual(archive.latest()["draft"], "recover me")

    def test_event_server_publishes_authoritative_mode(self):
        left, right = socket.socketpair()
        server = EventServer(Path("unused"), lambda _event: None)
        server.client = left
        server.set_mode("paused")
        self.assertEqual(json.loads(right.makefile().readline()), {
            "type": "mode", "state": "paused",
        })
        left.close()
        right.close()

    def test_mode_survives_disconnected_satellite(self):
        class Closed:
            def sendall(self, _payload):
                raise BrokenPipeError

        server = EventServer(Path("unused"), lambda _event: None)
        server.client = Closed()
        server.set_mode("closed")
        self.assertEqual(server.mode, "closed")
        self.assertIsNone(server.client)

    def test_event_server_rejects_unknown_action(self):
        with self.assertRaisesRegex(ValueError, "unknown voice action"):
            EventServer._validate({"type": "action", "action": "guess"})

    @patch("alan_composer.destination._active_pane", return_value="%7")
    @patch("alan_composer.destination.find")
    @patch("alan_composer.destination.os.uname")
    @patch("alan_composer.destination.subprocess.run")
    def test_global_main_is_resolved_on_hub(self, run, uname, find, _pane):
        uname.return_value.nodename = "boltzmann"
        run.side_effect = [
            type("Result", (), {"stdout": json.dumps({
                "focused": True, "window": 42,
                "window_properties": {"instance": "fleet-main"},
            })})(),
            type("Result", (), {"returncode": 0, "stdout": "source-key\n"})(),
        ]
        session = type("Session", (), {
            "ref": type("Ref", (), {
                "server": type("Server", (), {"host": "newton"})(),
                "session_id": "$1",
            })(),
            "name": "work",
        })()
        find.return_value = session

        selected = destination.capture()

        command = run.call_args_list[1].args[0]
        self.assertEqual(command[:5], ["ssh", "-T", "-o", "BatchMode=yes", "lovelace"])
        self.assertIn("agent_fleet.viewer import exchange", command[7])
        self.assertEqual(selected.key, "source-key")

    @patch("alan_composer.i3.subprocess.run")
    @patch("alan_composer.i3.subprocess.check_output")
    def test_i3_places_composer_at_workspace_top(self, output, run):
        output.return_value = json.dumps({
            "type": "workspace", "rect": {"width": 1920},
            "nodes": [{"window": 42, "rect": {"width": 1920}, "nodes": []}],
        }).encode()
        i3.place(42)
        self.assertEqual(run.call_args.args[0][-1],
                         "move container to workspace current, floating disable, move up")

    @patch("alan_composer.i3.subprocess.run")
    @patch("alan_composer.i3.subprocess.check_output")
    def test_i3_climbs_nested_layout_to_workspace_width(self, output, run):
        output.side_effect = [
            json.dumps({
                "type": "workspace", "rect": {"width": 1920},
                "nodes": [{"window": 42, "rect": {"width": 960}, "nodes": []}],
            }).encode(),
            json.dumps({
                "type": "workspace", "rect": {"width": 1920},
                "nodes": [{"window": 42, "rect": {"width": 1920}, "nodes": []}],
            }).encode(),
        ]
        i3.place(42)
        self.assertEqual(run.call_args_list[-1].args[0][-1], "move up")

    @patch("alan_composer.i3.subprocess.run")
    @patch("alan_composer.i3.subprocess.check_output")
    def test_i3_resizes_tiled_height_relatively(self, output, run):
        output.return_value = json.dumps({
            "window": None, "nodes": [{
                "window": 42, "rect": {"height": 500}, "nodes": [],
            }],
        }).encode()
        i3.resize(42, 120)
        self.assertEqual(run.call_args.args[0][-1], "resize shrink height 380 px")

if __name__ == "__main__":
    unittest.main()
