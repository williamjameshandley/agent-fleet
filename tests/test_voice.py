import io
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from alan_composer.archive import Archive
from alan_composer.model import Composition, Mode, classify
from alan_composer.transcribe import Transcriber


class VoiceModelTests(unittest.TestCase):
    def test_local_controls_are_exact(self):
        self.assertEqual(classify("Alan, send."), ("control", "send"))
        self.assertEqual(classify("Alan, make this shorter"),
                         ("instruction", "make this shorter"))
        self.assertEqual(classify("Alan, here is the prompt", opening=True),
                         ("dictation", "here is the prompt"))
        self.assertEqual(classify("Alan, pause", opening=True),
                         ("control", "pause"))
        self.assertEqual(classify("A long rambling sentence"),
                         ("dictation", "A long rambling sentence"))

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

    def test_alan_home_stream_is_full_duplex(self):
        received = []
        done = threading.Event()

        class FakeSocket:
            def __init__(self):
                self.sent = []

            def sendall(self, data):
                self.sent.append(data)

            def makefile(self):
                return io.StringIO(
                    '{"ok": true}\n'
                    '{"type":"utterance","text":"hello"}\n')

            def shutdown(self, _direction):
                done.set()

        sock = FakeSocket()
        auth = io.BytesIO(
            b'endpoint_id="composer-boltzmann"\nauth_token="secret"\n')
        transcriber = Transcriber(received.append)
        with patch("urllib.request.urlopen", return_value=auth), \
                patch("socket.create_connection", return_value=sock):
            transcriber.start()
            transcriber.feed(memoryview(b"pcm"))
            transcriber.stop()

        self.assertTrue(done.wait(1))
        header = json.loads(sock.sent[0])
        self.assertEqual(header["protocol"], "alan-audio/2")
        self.assertEqual(header["endpoint_id"], "composer-boltzmann")
        self.assertEqual(sock.sent[1], b"pcm")
        for _ in range(100):
            if received:
                break
            done.wait(.01)
        self.assertEqual(received, ["hello"])

if __name__ == "__main__":
    unittest.main()
