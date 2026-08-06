import asyncio
import os
import socket
import sys
import shlex
import json
import subprocess
import hashlib
import time
import threading

import networkx as nx

from .config import HUB, RUNTIME, hosts, ssh_environment
from .alan import provider_identity
from .protocol import decode_graph, decode_message, encode
from .model import key_host
from .tmux import capture, event_stream
from .quota import read as quota_read
from . import render


def events(host):
    """Stream one host's session events and answer preview requests."""
    lock = threading.Lock()
    consumer = threading.Event()

    def emit(message):
        with lock:
            print(message, flush=True)

    def requests():
        try:
            for line in sys.stdin:
                request = json.loads(line)
                try:
                    text = capture(request["key"], request["columns"], request["lines"])
                    response = {"preview": request["preview"], "text": text}
                except RuntimeError as error:
                    response = {"preview": request["preview"], "error": str(error)}
                emit(json.dumps(response, separators=(",", ":")))
        finally:
            consumer.set()

    threading.Thread(target=requests, daemon=True).start()
    for sessions, graph in event_stream(host, consumer):
        usage = quota_read() if host == hosts()[0] else {}
        emit(encode(sessions, usage, graph=graph))


class Fleet:
    def __init__(self):
        self.sessions = {}
        self.observations = {}
        self.observed = 0
        self._composed = (None, nx.MultiDiGraph())
        self.usage = {}
        self.unavailable = set(hosts())
        self.refresh_pending = False
        self.processes = {}
        self.previews = {}
        self.next_preview = 0

    async def collect(self, host):
        python = (sys.executable, "-c",
                  "import sys; from agent_fleet.daemon import events; events(sys.argv[1])",
                  host)
        command = (list(python) if host == os.uname().nodename else
                   ["ssh", "-T", "-o", "BatchMode=yes", host, shlex.join(python)])
        while True:
            process = await asyncio.create_subprocess_exec(*command,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=sys.maxsize)
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
                    self.observations[host] = raw
                    self.observed += 1
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
                self.observations.pop(host, None)
                self.observed += 1
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
                "-XPOST", "-d", "transform-header(sh -c '/usr/lib/agent-fleet/ui header')+reload-sync(/usr/lib/agent-fleet/ui items)",
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
                             sorted(self.unavailable), self.composed_graph())
        elif request.startswith("items "):
            projected = await self.projected()
            payload = render.rows_text(projected, sorted(self.unavailable),
                                       int(request.removeprefix("items ")))
        elif request == "header":
            projected = await self.projected()
            payload = render.header_text(projected, self.usage,
                                         sorted(self.unavailable))
        elif request == "cursor" or request.startswith("cursor "):
            active = request.removeprefix("cursor").strip()
            payload = active or await self.first_waiting()
        elif request.startswith("preview "):
            key, columns, lines = request.removeprefix("preview ").rsplit(" ", 2)
            payload = await self.preview(key, int(columns), int(lines))
        elif request == "commander-context":
            payload = json.dumps(await self.commander_context(), sort_keys=True,
                                 separators=(",", ":"))
        else:
            raise ValueError(f"unknown daemon request {request!r}")
        payload += "\n"
        try:
            writer.write(payload.encode())
            await writer.drain()
        except ConnectionResetError:
            return
        finally:
            writer.close()

    async def projected(self):
        expanded, show_python = await asyncio.gather(
            self.muster_option("@fleet_expanded"),
            self.muster_option("@fleet_show_python"))
        return render.order(
            [s for group in self.sessions.values() for s in group],
            sorted(self.unavailable), self.composed_graph(),
            expanded=set(expanded.split()),
            show_python=show_python.strip() == "1")

    async def first_waiting(self):
        projected = await self.projected()
        return next((item.session.ref.key for item in projected
                     if item.session.state == "waiting"), "")

    @staticmethod
    async def muster_option(name):
        process = await asyncio.create_subprocess_exec(
            "tmux", "show-options", "-qv", "-t", "=fleet@muster:", name,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await process.communicate()
        return stdout.decode()

    def composed_graph(self):
        generation, composed = self._composed
        if generation != self.observed:
            graphs = [graph for graph in
                      (decode_graph(raw) for raw in self.observations.values())
                      if graph is not None]
            composed = nx.compose_all(graphs) if graphs else nx.MultiDiGraph()
            composed.graph["actors"] = [
                actor
                for graph in graphs
                for actor in graph.graph.get("actors", [])
            ]
            self._composed = (self.observed, composed)
        return composed

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
            *(self.remote_json(
                host, sys.executable, "-c",
                "import json; from agent_fleet.actions import context; print(json.dumps(context()))",
              )
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
        raw = self.observations.get(host)
        actors = [] if raw is None or decode_graph(raw) is None else await self.remote_json(
                host, sys.executable, "-c",
                "import json; from agent_fleet.alan import actors; print(json.dumps(actors()))",
            )
        transcripts = await self.remote_json(
            host, sys.executable, "-c",
            "import json; from agent_fleet.transcripts import history; "
            "print(json.dumps(history(100)))",
        )
        return {"host": host, "actors": actors, "transcripts": transcripts}

    @staticmethod
    def history_entries(sessions, source_hosts, observations):
        live = {(item["host"], item["agent"], item.get("transcript_id"))
                for item in sessions if item.get("transcript_id")}
        authorities = set(live)
        entries = []
        for host, observation in zip(source_hosts, observations):
            for actor in observation["actors"]:
                native_id = provider_identity(actor["addr"], actor.get("kind"))
                identity = host, actor.get("kind"), native_id
                retained = (actor.get("kind") == "llm" or
                            actor.get("kind") in {"claude", "codex"})
                if retained and actor.get("state") in {"retired", "unavailable"}:
                    if native_id:
                        authorities.add(identity)
                    entries.append({"key": f'alan:{actor["addr"]}', "host": host,
                                    "agent": actor["kind"],
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
                           shlex.join((sys.executable, "-c",
                                      "from agent_fleet.daemon import projection; print(projection(), end='')"))], text=True,
                          capture_output=True, check=True).stdout


def preview(key, columns=0, lines=0):
    if os.uname().nodename.split(".", 1)[0] != HUB:
        raise RuntimeError("pane previews are served by the Lovelace Muster")
    return request(f"preview {key} {columns} {lines}")


def commander_context():
    return request("commander-context")
