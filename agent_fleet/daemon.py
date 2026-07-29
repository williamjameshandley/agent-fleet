import asyncio
import os
import socket
import sys
import shlex
import json
import subprocess
import hashlib
import time

from .config import HUB, RUNTIME, hosts, ssh_environment
from .protocol import decode_message, encode
from .model import key_host


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
        command = ([sys.executable, "-m", "agent_fleet.cli", "events", "--host", host]
                   if host == os.uname().nodename
                   else ["ssh", "-T", "-o", "BatchMode=yes", host,
                         shlex.join(("fleet", "events", "--host", host))])
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
        try:
            await asyncio.sleep(.03)
            path = RUNTIME / "muster.sock"
            if not path.exists():
                return
            await self.wait_for_muster_idle()
            self.refresh_pending = False
            process = await asyncio.create_subprocess_exec(
                "curl", "-fsS", "--max-time", "2", "--unix-socket", str(path),
                "-XPOST", "-d", "transform-header(sh -c '/usr/bin/fleet header')+reload-sync(fleet items)",
                "http://localhost",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await process.wait()
        finally:
            self.refresh_pending = False

    async def wait_for_muster_idle(self):
        while True:
            process = await asyncio.create_subprocess_exec(
                "tmux", "list-clients", "-t", "=fleet@muster",
                "-F", "#{client_activity}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await process.communicate()
            if process.returncode:
                raise RuntimeError(stderr.decode().strip())
            activity = max(map(int, stdout.split()), default=0)
            delay = activity + 3 - time.time()
            if delay <= 0:
                return
            await asyncio.sleep(delay)

    async def reply(self, reader, writer):
        request = (await reader.readline()).decode().rstrip()
        if request == "snapshot":
            payload = encode([s for group in self.sessions.values() for s in group], self.usage,
                             sorted(self.unavailable))
        elif request.startswith("preview "):
            key, columns, lines = request.removeprefix("preview ").rsplit(" ", 2)
            payload = await self.preview(key, int(columns), int(lines))
        elif request == "commander-context":
            payload = json.dumps(await self.commander_context(), sort_keys=True,
                                 separators=(",", ":"))
        else:
            raise ValueError(f"unknown daemon request {request!r}")
        payload += "\n"
        writer.write(payload.encode())
        await writer.drain()
        writer.close()

    async def commander_context(self):
        sessions = sorted(
            ({"source": session.ref.key, "host": session.ref.server.host,
              "name": session.name, "agent": session.agent, "state": session.state,
              "summary": session.summary, "recency": session.human_activity,
              "transcript_id": session.transcript_id}
             for group in self.sessions.values() for session in group),
            key=lambda item: item["source"])
        source_hosts = sorted(hosts())
        observations = await asyncio.gather(
            *(self.remote_json(host, "fleet", "context")
              for host in ("boltzmann", "noether", "newton")),
            *(self.history_observation(host) for host in source_hosts))
        workstations = {
            host: {key: observation[key] for key in ("profile", "unavailable", "slots")}
            for host, observation in zip(("boltzmann", "noether", "newton"), observations[:3])
        }
        history = self.history_entries(sessions, source_hosts, observations[3:])
        body = {"version": 1, "sessions": sessions, "hosts": source_hosts,
                "unavailable": sorted(self.unavailable), "history": history,
                "workstations": workstations}
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False).encode()
        return {**body, "revision": hashlib.sha256(canonical).hexdigest()}

    async def remote_json(self, host, *command):
        argv = list(command) if host == os.uname().nodename.split(".", 1)[0] else [
            "ssh", "-T", "-o", "BatchMode=yes", host, shlex.join(command)]
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=ssh_environment())
        stdout, stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError(stderr.decode().strip() or f"{host}: {' '.join(command)} failed")
        return json.loads(stdout)

    async def history_observation(self, host):
        actors, transcripts = await asyncio.gather(
            self.remote_json(host, "fleet", "alan-actors"),
            self.remote_json(host, "fleet", "transcripts", "--limit", "100"))
        return {"host": host, "actors": actors, "transcripts": transcripts}

    @staticmethod
    def history_entries(sessions, source_hosts, observations):
        live = {(item["host"], item["agent"], item.get("transcript_id"))
                for item in sessions if item.get("transcript_id")}
        authorities = set(live)
        entries = []
        for host, observation in zip(source_hosts, observations):
            for actor in observation["actors"]:
                native_id = (actor.get("native") or {}).get("id")
                identity = host, actor.get("type"), native_id
                if (actor.get("type") in {"claude", "codex"} and native_id and
                        actor.get("state") in {"retired", "failed"}):
                    authorities.add(identity)
                    entries.append({"key": f'alan:{host}:{actor["addr"]}', "host": host,
                                    "agent": actor["type"],
                                    "name": actor.get("label") or actor["addr"],
                                    "cwd": actor.get("cwd") or "",
                                    "mtime": max(actor.get("human_activity", 0),
                                                 actor.get("created", 0))})
            for item in observation["transcripts"]:
                if (host, item["agent"], item["session_id"]) not in authorities:
                    entries.append({"key": f'{host}:{item["agent"]}:{item["session_id"]}',
                                    "host": host, "agent": item["agent"],
                                    "name": item["name"], "cwd": item["cwd"],
                                    "mtime": item["mtime"]})
        return sorted(entries, key=lambda item: item["key"])

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
        if not any(session.ref.key == key for group in self.sessions.values() for session in group):
            raise RuntimeError(f"session disappeared: {key}")
        host = key_host(key)
        if host in self.unavailable:
            raise RuntimeError(f"{host} is disconnected; refusing action")
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


def request(message):
    path = RUNTIME / "fleet.sock"
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(str(path))
        client.sendall((message + "\n").encode())
        chunks = []
        while chunk := client.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks).decode()


def projection():
    return request("snapshot")


def snapshot():
    if os.uname().nodename.split(".", 1)[0] == HUB:
        return projection()
    return subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", HUB,
                           "fleet projection"], text=True,
                          capture_output=True, check=True).stdout


def preview(key, columns=0, lines=0):
    if os.uname().nodename.split(".", 1)[0] != HUB:
        raise RuntimeError("pane previews are served by the Lovelace Muster")
    return request(f"preview {key} {columns} {lines}")


def commander_context():
    return request("commander-context")
