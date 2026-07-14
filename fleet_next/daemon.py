import asyncio
import os
import socket
import sys
import shlex

from .config import RUNTIME, hosts
from .protocol import decode_message, encode


class Fleet:
    def __init__(self):
        self.sessions = {}
        self.usage = {}
        self.unavailable = set(hosts())
        self.refresh_pending = False

    async def collect(self, host):
        command = ([sys.executable, "-m", "fleet_next.cli", "events", "--host", host]
                   if host == os.uname().nodename
                   else ["ssh", "-T", "-o", "BatchMode=yes", host,
                         shlex.join(("fleet-next", "events", "--host", host))])
        while True:
            process = await asyncio.create_subprocess_exec(*command,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            errors = []

            async def stderr():
                assert process.stderr
                async for raw in process.stderr:
                    errors.append(raw.decode().rstrip())
                    print(f"{host}: {errors[-1]}", flush=True)

            drain = asyncio.create_task(stderr())
            try:
                assert process.stdout
                async for raw in process.stdout:
                    sessions, usage, _ = decode_message(raw)
                    self.sessions[host] = sessions
                    self.unavailable.discard(host)
                    if host == hosts()[0] and usage:
                        self.usage = usage
                    self.schedule_refresh()
                await drain
            finally:
                if process.returncode is None:
                    process.terminate()
                    await process.wait()
                if not drain.done():
                    drain.cancel()
            self.unavailable.add(host)
            self.schedule_refresh()
            await asyncio.sleep(1)

    def schedule_refresh(self):
        if not self.refresh_pending:
            self.refresh_pending = True
            asyncio.create_task(self.refresh_muster())

    async def refresh_muster(self):
        await asyncio.sleep(.03)
        self.refresh_pending = False
        path = RUNTIME / "muster.sock"
        if not path.exists():
            return
        process = await asyncio.create_subprocess_exec(
            "curl", "-fsS", "--max-time", ".2", "--unix-socket", str(path),
            "-XPOST", "-d", "reload-sync(fleet-next items)+transform-header(fleet-next header)",
            "http://localhost",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await process.wait()

    async def reply(self, reader, writer):
        await reader.readline()
        payload = encode([s for group in self.sessions.values() for s in group], self.usage,
                         sorted(self.unavailable)) + "\n"
        writer.write(payload.encode())
        await writer.drain()
        writer.close()

    async def serve(self):
        RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = RUNTIME / "fleet.sock"
        path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(self.reply, path)
        os.chmod(path, 0o600)
        async with server:
            async with asyncio.TaskGroup() as group:
                group.create_task(server.serve_forever())
                for host in hosts():
                    group.create_task(self.collect(host))


def snapshot():
    path = RUNTIME / "fleet.sock"
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(str(path))
        client.sendall(b"snapshot\n")
        chunks = []
        while chunk := client.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks).decode()
