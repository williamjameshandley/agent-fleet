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
        class Detector:
            def __init__(self):
                self.scores = iter([.3, .4, .8, *([.05] * 15)])
                self.frames = []

            def predict(self, _block, frame_size):
                self.frames.append(frame_size)
                return next(self.scores)

            def reset_states(self):
                pass

        complete = []
        detector = Detector()
        segmenter = Segmenter(complete.append, detector)
        block = np.zeros(1280, dtype="int16")
        segmenter.start()
        for _ in range(18):
            segmenter.feed(block)
        self.assertEqual(len(complete[0]), 1280 * 3 * 2)
        self.assertEqual(set(detector.frames), {640})
        segmenter.stop()
        self.assertEqual(segmenter.blocks, [])

    def test_segmenter_rejects_an_isolated_vad_spike(self):
        class Detector:
            def __init__(self):
                self.scores = iter([.3, *([.01] * 20)])

            def predict(self, _block, frame_size):
                return next(self.scores)

            def reset_states(self):
                pass

        complete = []
        segmenter = Segmenter(complete.append, Detector())
        segmenter.start()
        for _ in range(21):
            segmenter.feed(np.zeros(1280, dtype="int16"))
        self.assertEqual(complete, [])


if __name__ == "__main__":
    unittest.main()
