import io
import os
import tomllib
import wave
from pathlib import Path

CONFIG = Path.home() / ".config/linux-voice/config.toml"
MIN_LOGPROB = -.5


def text(result):
    confidence = min((segment["avg_logprob"] for segment in result.segments), default=-1)
    return result.text.strip() if confidence >= MIN_LOGPROB else ""


class Transcriber:
    def __init__(self):
        from groq import Groq

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
        result = self._request(data, self.prompt)
        transcript = text(result)
        if not transcript:
            data.seek(0)
            transcript = text(self._request(data, ""))
        return transcript

    def _request(self, data, prompt):
        return self.client.audio.transcriptions.create(
            file=data,
            model="whisper-large-v3-turbo",
            language=self.language,
            prompt=prompt,
            response_format="verbose_json",
            timestamp_granularities=["segment"],
            temperature=0,
        )
