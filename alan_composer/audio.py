import queue
import subprocess
import threading
from collections import deque
from pathlib import Path

RATE = 16000
FRAME = 1280


class Capture:
    def __init__(self, root, frames):
        import sounddevice as sd

        self.root = root
        self.frames = frames
        self.queue = queue.Queue()
        self.history = deque(maxlen=20)
        self.stream = sd.InputStream(
            samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME,
            callback=self._capture,
        )
        self.writer = threading.Thread(target=self._write, daemon=True)

    def start(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.writer.start()
        self.stream.start()

    def _capture(self, data, _count, _time, _status):
        block = data[:, 0].copy()
        self.history.append(block)
        self.queue.put(block)
        self.frames(block)

    def preroll(self):
        return list(self.history)

    def _write(self):
        command = [
            "ffmpeg", "-nostdin", "-loglevel", "warning",
            "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "pipe:0",
            "-c:a", "flac", "-f", "segment", "-segment_time", "300",
            "-segment_format", "ogg", "-reset_timestamps", "1", "-strftime", "1",
            str(self.root / "%Y%m%d-%H%M%S.oga"),
        ]
        with subprocess.Popen(command, stdin=subprocess.PIPE) as encoder:
            while True:
                encoder.stdin.write(self.queue.get().tobytes())


class WakeDetector:
    def __init__(self, model_path, complete, threshold=0.5):
        from openwakeword.model import Model

        resources = Path.home() / ".local/share/openwakeword"
        self.name = Path(model_path).stem
        self.complete = complete
        self.threshold = threshold
        self.armed = True
        self.model = Model(
            wakeword_models=[model_path],
            inference_framework="onnx",
            melspec_model_path=str(resources / "melspectrogram.onnx"),
            embedding_model_path=str(resources / "embedding_model.onnx"),
        )

    def feed(self, block):
        score = float(self.model.predict(block)[self.name])
        if self.armed and score >= self.threshold:
            self.armed = False
            self.complete()

    def reset(self):
        self.model.reset()
        self.armed = True
