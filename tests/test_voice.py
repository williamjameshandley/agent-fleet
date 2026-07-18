import tempfile
import unittest

import numpy as np

from alan_composer.archive import Archive
from alan_composer.audio import Segmenter
from alan_composer.model import Composition, Mode, classify


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

    def test_segmenter_preroll_and_stop(self):
        complete = []
        segmenter = Segmenter(complete.append)
        speech = np.full(1280, 1000, dtype="int16")
        silence = np.zeros(1280, dtype="int16")
        segmenter.start([speech])
        for _ in range(15):
            segmenter.feed(silence)
        self.assertTrue(complete[0].startswith(speech.tobytes()))
        segmenter.start([speech])
        segmenter.stop()
        self.assertEqual(segmenter.blocks, [])


if __name__ == "__main__":
    unittest.main()
