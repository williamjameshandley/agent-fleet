import asyncio
import os
import socket
import sys
import shlex
import json

from .config import RUNTIME, hosts
from .protocol import decode_message, encode


class Fleet:
    def __init__(self):
        self.sessions = {}
        self.usage = {}
        self.unavailable = set(hosts())
        self.refresh_pending = False
        self.processes = {}
        self.previews = {}
        self.next_preview = 0

    async def collect(self, host):
        command = ([sys.executable, "-m", "fleet_next.cli", "events", "--host", host]
                   if host == os.uname().nodename
                   else ["ssh", "-T", "-o", "BatchMode=yes", host,
                         shlex.join(("fleet-next", "events", "--host", host))])
        while True:
            process = await asyncio.create_subprocess_exec(*command,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            self.processes[host] = process
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
                    message = json.loads(raw)
                    if "preview" in message:
                        _, future = self.previews.pop(message["preview"])
                        if "error" in message:
                            future.set_exception(RuntimeError(message["error"]))
                        else:
                            future.set_result(message["text"])
                        continue
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
                self.processes.pop(host, None)
                for number, (owner, future) in list(self.previews.items()):
                    if owner == host:
                        future.set_exception(RuntimeError(f"{host} disconnected"))
                        del self.previews[number]
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
        request = (await reader.readline()).decode().rstrip()
        if request == "snapshot":
            payload = encode([s for group in self.sessions.values() for s in group], self.usage,
                             sorted(self.unavailable))
        elif request.startswith("preview "):
            key, columns, lines = request.removeprefix("preview ").rsplit(" ", 2)
            payload = await self.preview(key, int(columns), int(lines))
        else:
            raise ValueError(f"unknown daemon request {request!r}")
        payload += "\n"
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

    async def preview(self, key, columns=0, lines=0):
        host = key.split(":", 1)[0]
        if host in self.unavailable:
            raise RuntimeError(f"{host} is disconnected")
        process = self.processes[host]
        assert process.stdin
        self.next_preview += 1
        number = self.next_preview
        future = asyncio.get_running_loop().create_future()
        self.previews[number] = (host, future)
        process.stdin.write((json.dumps({"preview": number, "key": key,
                                         "columns": columns, "lines": lines}) + "\n").encode())
        await process.stdin.drain()
        return await future


def snapshot():
    path = RUNTIME / "fleet.sock"
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(str(path))
        client.sendall(b"snapshot\n")
        chunks = []
        while chunk := client.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks).decode()


def preview(key, columns=0, lines=0):
    path = RUNTIME / "fleet.sock"
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(str(path))
        client.sendall((f"preview {key} {columns} {lines}\n").encode())
        chunks = []
        while chunk := client.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks).decode()
