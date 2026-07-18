import io
import os
import tomllib
import wave
from pathlib import Path

from groq import Groq


CONFIG = Path.home() / ".config/linux-voice/config.toml"


class Transcriber:
    def __init__(self):
        config = tomllib.loads(CONFIG.read_text())
        settings = config["transcription"]
        key = os.environ.get("GROQ_API_KEY", settings.get("api_key"))
        self.client = Groq(api_key=key)
        self.language = settings.get("language", "en")
        self.prompt = settings.get("prompt", "")

    def __call__(self, audio, rate):
        data = io.BytesIO()
        data.name = "utterance.wav"
        with wave.open(data, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes(audio)
        data.seek(0)
        result = self.client.audio.transcriptions.create(
            file=data,
            model="whisper-large-v3-turbo",
            language=self.language,
            prompt=self.prompt,
            response_format="text",
        )
        return result.strip()
