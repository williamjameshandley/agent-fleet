from .daemon import snapshot
from .protocol import decode_message


def find(key, live=True):
    sessions, _, unavailable = decode_message(snapshot())
    for session in sessions:
        if session.ref.key == key:
            if live and session.ref.server.host in unavailable:
                raise SystemExit(f"{session.ref.server.host} is disconnected; refusing action")
            return session
    raise SystemExit(f"session disappeared: {key}")
