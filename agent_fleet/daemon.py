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
import queue
import re
from pathlib import Path

import networkx as nx

from .config import HUB, RUNTIME, hosts, ssh_environment
from .alan import address_identity
from .protocol import decode_message, decode_observation, encode
from .model import key_host
from .tmux import ControlSlot, capture, event_stream, split_key
from .alan import Watcher as AlanWatcher
from .quota import read as quota_read
from . import journal, proc, render


MARKER_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")


def remove_viewer_marker(host, owner, slot):
    if not MARKER_COMPONENT.fullmatch(owner) or not MARKER_COMPONENT.fullmatch(slot):
        raise ValueError("invalid viewer marker identity")
    path = RUNTIME / f"viewer-{owner}-{slot}-{host}.tty"
    path.unlink(missing_ok=True)


def events(host):
    """Stream one host's session events and answer preview requests."""
    lock = threading.Lock()
    consumer = threading.Event()
    controls = ControlSlot()
    changes = queue.Queue()
    alan = AlanWatcher(changes, consumer)

    def emit(message):
        with lock:
            print(message, flush=True)

    def requests():
        try:
            for line in sys.stdin:
                request = json.loads(line)
                try:
                    if "preview" in request:
                        key = request["key"]
                        if (not key.startswith("alan:") or
                                key.removeprefix("alan:").split("-", 1)[0]
                                in {"claude", "codex"}):
                            controls.get()
                        with alan.full_graph() as graph:
                            text = capture(request["key"], request["columns"],
                                           request["lines"], graph)
                        response = {"preview": request["preview"], "text": text}
                    elif "switch" in request:
                        control = controls.get()
                        target = (control.alan_target(request["actor"], {
                            "kind": request["agent"], "cwd": request["cwd"]})
                                  if "actor" in request else tuple(request["target"]))
                        duration = control.switch(target, request["client"])
                        response = {"switch": request["switch"], "duration": duration,
                                    "target": target}
                    elif "cleanup" in request:
                        remove_viewer_marker(host, request["owner"], request["slot"])
                        response = {"cleanup": request["cleanup"]}
                    else:
                        raise RuntimeError("unknown host request")
                except Exception as error:
                    tag = next(name for name in
                               ("preview", "switch", "cleanup")
                               if name in request)
                    response = {tag: request[tag], "error": str(error)}
                emit(json.dumps(response, separators=(",", ":")))
        finally:
            consumer.set()

    threading.Thread(target=requests, daemon=True).start()
    for sessions, graph, available in event_stream(
            host, consumer, controls, changes, alan_watcher=alan):
        usage = quota_read() if host == hosts()[0] else {}
        emit(encode(sessions, usage, [] if available else [host], graph=graph))


class Fleet:
    def __init__(self):
        self.sessions = {}
        self.graphs = {}
        self.observed = 0
        self._composed = (None, nx.MultiDiGraph())
        self.usage = {}
        self.unavailable = set(hosts())
        self.tmux_unavailable = set()
        self.refresh_pending = False
        self.processes = {}
        self.previews = {}
        self.next_preview = 0
        self.switches = {}
        self.next_switch = 0
        self.cleanups = {}
        self.next_cleanup = 0
        self.changed = asyncio.Condition()
        self.action_error = ""
        self.muster_generation = None
        self.expanded = set()
        self.show_python = False
        self.view_revision = 0
        self.view_width = 100
        self._view_cache = None
        self.view_lock = asyncio.Lock()
        self.publication = 0
        self.pending_archives = set()
        self.background_tasks = set()
        self.task_names = {}

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
                    print(f"{host}: {errors[-1]}", file=sys.stderr, flush=True)

            drain = asyncio.create_task(stderr())
            try:
                assert process.stdout
                async for raw in process.stdout:
                    if raw.startswith(b'{"version":'):
                        self.update_host(host, raw)
                    elif self.host_reply(json.loads(raw)):
                        continue
                    else:
                        raise ValueError("invalid host response")
                    async with self.changed:
                        self.changed.notify_all()
                    self.schedule_refresh()
                await drain
            finally:
                if process.returncode is None:
                    process.terminate()
                    await process.wait()
                if not drain.done():
                    drain.cancel()
                await self.host_disconnected(host, process.pid, process.returncode)
            self.schedule_refresh()
            await asyncio.sleep(1)

    def update_host(self, host, raw):
        sessions, usage, unavailable, graph = decode_observation(raw)
        connected = host in self.unavailable
        self.sessions[host] = sessions
        self.graphs[host] = graph
        self.unavailable.discard(host)
        if host in unavailable:
            self.tmux_unavailable.add(host)
        else:
            self.tmux_unavailable.discard(host)
        if connected:
            journal.record("host_connected", host=host, pid=self.processes[host].pid)
        if host == hosts()[0] and usage:
            self.usage = usage
        self.observed += 1
        self.view_revision += 1
        self._view_cache = None

    def presentation_unavailable(self):
        return self.unavailable | self.tmux_unavailable

    def host_reply(self, message):
        if "preview" in message:
            _, future = self.previews.pop(message["preview"])
            if "error" in message:
                future.set_exception(RuntimeError(message["error"]))
            else:
                future.set_result(message["text"])
            return True
        if "switch" in message:
            _, future = self.switches.pop(message["switch"])
            if "error" in message:
                future.set_exception(RuntimeError(message["error"]))
            else:
                future.set_result((tuple(message["target"]), message["duration"]))
            return True
        if "cleanup" in message:
            _, future = self.cleanups.pop(message["cleanup"])
            if "error" in message:
                future.set_exception(RuntimeError(message["error"]))
            else:
                future.set_result(None)
            return True
        return False

    async def host_disconnected(self, host, pid=None, status=None):
        connected = host not in self.unavailable
        if connected and (pid is None or status is None):
            raise RuntimeError("connected host disconnect requires process identity and status")
        self.processes.pop(host, None)
        self.sessions.pop(host, None)
        self.graphs.pop(host, None)
        self.unavailable.add(host)
        self.tmux_unavailable.discard(host)
        if connected:
            journal.record("host_disconnected", host=host, pid=pid, status=status)
        self.observed += 1
        self.view_revision += 1
        self._view_cache = None
        async with self.changed:
            self.changed.notify_all()
        for pending in (self.previews, self.switches, self.cleanups):
            for number, (owner, future) in list(pending.items()):
                if owner == host:
                    future.set_exception(RuntimeError(f"{host} disconnected"))
                    del pending[number]

    def schedule_refresh(self):
        if not self.refresh_pending:
            self.refresh_pending = True
            self.own_task(asyncio.create_task(self.refresh_muster()), "refresh_muster")

    async def refresh_muster(self):
        try:
            await asyncio.sleep(.03)
            path = RUNTIME / "muster.sock"
            if not path.exists():
                return
            await self.wait_for_muster_idle()
            self.refresh_pending = False
            async with self.view_lock:
                action, artifacts = self.publish_view(self.view_width)
                await self.send_publication(path, action, artifacts)
        finally:
            self.refresh_pending = False

    @staticmethod
    async def send_publication(path, action, artifacts):
        process = await asyncio.create_subprocess_exec(
            "curl", "-fsS", "--max-time", "2", "--unix-socket", str(path),
            "-XPOST", "-d", action, "http://localhost",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        await process.wait()
        if process.returncode:
            for artifact in artifacts:
                artifact.unlink(missing_ok=True)

    async def wait_for_muster_idle(self):
        while True:
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/tmux", "-N", "list-clients", "-t", "=fleet@muster",
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
        if request.startswith("{"):
            self.action_error = ""
            self.schedule_refresh()
            try:
                value = await self.action(json.loads(request))
                payload = json.dumps({"ok": True, "value": value},
                                     separators=(",", ":"))
            except (KeyError, LookupError, OSError, RuntimeError, ValueError,
                    json.JSONDecodeError) as error:
                self.action_error = str(error)
                payload = json.dumps({"ok": False, "error": self.action_error},
                                     separators=(",", ":"))
            self.schedule_refresh()
        elif request == "snapshot":
            payload = encode([s for group in self.sessions.values() for s in group], self.usage,
                             sorted(self.presentation_unavailable()),
                             self.composed_graph())
        elif request.startswith("resolve "):
            key = request.removeprefix("resolve ")
            try:
                session = self.source(key)
            except (LookupError, RuntimeError) as error:
                value = {"error": str(error)}
            else:
                if session.ref.server.host in self.presentation_unavailable():
                    value = {"error": (f"{session.ref.server.host} presentation is "
                                       "unavailable; refusing action")}
                else:
                    value = {"agent": session.agent, "state": session.state,
                             "cwd": session.cwd,
                             "attachment": session.attachment.key
                             if session.attachment else ""}
            payload = json.dumps(value, separators=(",", ":"))
        elif request.startswith("switch "):
            try:
                value = json.loads(request.removeprefix("switch "))
                target, duration = await self.switch(value["key"], value["client"])
                response = {"target": target, "duration": duration}
            except (KeyError, LookupError, OSError, RuntimeError, ValueError,
                    json.JSONDecodeError) as error:
                response = {"error": str(error)}
            payload = json.dumps(response, separators=(",", ":"))
        elif request.startswith("cleanup "):
            try:
                value = json.loads(request.removeprefix("cleanup "))
                await self.cleanup(value["host"], value["owner"], value["slot"])
                response = {"ok": True}
            except (KeyError, LookupError, OSError, RuntimeError, ValueError,
                    json.JSONDecodeError) as error:
                response = {"error": str(error)}
            payload = json.dumps(response, separators=(",", ":"))
        elif request.startswith("items "):
            width = int(request.removeprefix("items "))
            self.view_width = width
            _, payload, _ = self.view(width)
        elif request == "header" or request.startswith("header "):
            width = (self.view_width if request == "header" else
                     int(request.removeprefix("header ")))
            self.view_width = width
            _, _, payload = self.view(width)
            if self.action_error:
                payload = f"Action failed: {self.action_error}\n{payload}"
        elif request == "cursor" or request.startswith("cursor "):
            active = request.removeprefix("cursor").strip()
            projected = self.projected()
            target = active or next(
                (item.session.ref.key for item in projected
                 if item.session.state == "waiting"), "")
            position = next(
                (index for index, item in enumerate(projected, 1)
                 if item.session.ref.key == target), None)
            payload = f"pos({position})" if position else ""
            self.schedule_refresh()
        elif request.startswith("muster-register\t"):
            try:
                values = request.split("\t")
                if len(values) != 6:
                    raise ValueError("invalid Muster registration")
                _, socket_path, pid, started, session_id, width = values
                payload = await self.register_muster(
                    (socket_path, int(pid), int(started), session_id), int(width))
            except (OSError, RuntimeError, ValueError) as error:
                payload = f"ERROR {error}"
        elif request.startswith(("fold\t", "toggle\t", "resize\t")):
            async with self.view_lock:
                payload = self.mutate_view(request)
        elif request.startswith(("archive\t", "refresh\t")):
            async with self.view_lock:
                payload = await self.mutate_action(request)
        elif request.startswith("next-waiting\t"):
            payload = self.next_waiting(request.removeprefix("next-waiting\t"))
        elif request.startswith("preview "):
            key, columns, lines = request.removeprefix("preview ").rsplit(" ", 2)
            payload = await self.preview(key, int(columns), int(lines))
        elif request == "commander-context":
            payload = json.dumps(await self.commander_context(), sort_keys=True,
                                 separators=(",", ":"))
        elif request == "history":
            payload = json.dumps(await self.history(), separators=(",", ":"))
        elif request.startswith("history-search "):
            try:
                query = json.loads(request.removeprefix("history-search "))
                value = {"ok": True, "value": await self.search_history(query)}
            except (KeyError, LookupError, OSError, RuntimeError, ValueError,
                    json.JSONDecodeError) as error:
                value = {"ok": False, "error": str(error)}
            payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
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

    def projected(self):
        ordered = render.order(
            [s for group in self.sessions.values() for s in group],
            sorted(self.presentation_unavailable()), self.composed_graph(),
            expanded=self.expanded, show_python=self.show_python)
        visible = []
        hidden_depth = None
        for item in ordered:
            if hidden_depth is not None:
                if item.depth > hidden_depth:
                    continue
                hidden_depth = None
            if item.session.ref.key in self.pending_archives:
                hidden_depth = item.depth
            else:
                visible.append(item)
        return visible

    def first_waiting(self):
        projected = self.projected()
        return next((item.session.ref.key for item in projected
                     if item.session.state == "waiting"), "")

    def view(self, width):
        key = (self.view_revision, width)
        if self._view_cache and self._view_cache[0] == key:
            return self._view_cache[1]
        projected = self.projected()
        value = (
            projected,
            render.rows_text(projected, sorted(self.presentation_unavailable()), width,
                             revision=self.view_revision),
            render.header_text(projected, self.usage,
                               sorted(self.presentation_unavailable())),
        )
        self._view_cache = (key, value)
        return value

    def publish_view(self, width, error=""):
        self.view_width = width
        _, rows, header = self.view(width)
        if self.pending_archives:
            count = len(self.pending_archives)
            header = f"Archiving {count} session{'s' if count != 1 else ''}...\n{header}"
        if error:
            header = f"Action failed: {error}\n{header}"
        self.publication += 1
        stem = RUNTIME / f"muster-view-{self.view_revision}-{self.publication}"
        rows_path = stem.with_suffix(".rows")
        header_path = stem.with_suffix(".header")
        rows_path.write_text(rows + ("\n" if rows else ""))
        header_path.write_text(header + "\n")
        rows_command = shlex.join(("/usr/bin/cat", str(rows_path)))
        header_command = shlex.join(("/usr/bin/cat", str(header_path)))
        rows_remove = shlex.join(("/usr/bin/rm", "-f", str(rows_path)))
        header_remove = shlex.join(("/usr/bin/rm", "-f", str(header_path)))
        action = (f"transform-header({header_command}; {header_remove})"
                  f"+reload-sync({rows_command}; {rows_remove})")
        return action, (rows_path, header_path)

    def mutate_view(self, request):
        values = request.split("\t")
        try:
            if self.muster_generation is None:
                raise RuntimeError("Muster generation is not registered")
            if values[0] == "fold" and len(values) == 5:
                _, operation, key, expected, width = values
                if operation not in {"open", "close"}:
                    raise ValueError("invalid fold request")
                width = int(width)
                if width < 1:
                    raise ValueError("invalid Muster width")
                int(expected)
                projected, _, _ = self.view(int(width))
                matches = [item for item in projected
                           if item.session.ref.key == key]
                if len(matches) != 1:
                    raise LookupError(
                        f"session is not in the displayed view: {key}")
                [item] = matches
                if item.session.ref.server.kind != "alan" or not item.child_count:
                    raise ValueError("fold requires an Alan parent with children")
                actor = item.session.ref.session_id
                changed = (actor not in self.expanded if operation == "open" else
                           actor in self.expanded)
                if operation == "open":
                    self.expanded.add(actor)
                else:
                    self.expanded.discard(actor)
            elif values[:2] == ["toggle", "python"] and len(values) == 3:
                width = int(values[2])
                if width < 1:
                    raise ValueError("invalid Muster width")
                self.show_python = not self.show_python
                changed = True
            elif values[0] == "resize" and len(values) == 2:
                width = int(values[1])
                if width < 1:
                    raise ValueError("invalid Muster width")
                changed = width != self.view_width
            else:
                raise ValueError("invalid Muster view request")
            if changed:
                self.view_revision += 1
                self._view_cache = None
            self.action_error = ""
            return self.publish_view(width)[0]
        except (LookupError, RuntimeError, ValueError) as error:
            self.action_error = str(error)
            width = self.view_width
            return self.publish_view(width, self.action_error)[0]

    async def mutate_action(self, request):
        values = request.split("\t")
        width = self.view_width
        try:
            if self.muster_generation is None:
                raise RuntimeError("Muster generation is not registered")
            if len(values) != 4 or values[0] not in {"archive", "refresh"}:
                raise ValueError("invalid Muster action request")
            operation, key, expected, raw_width = values
            width = int(raw_width)
            if width < 1:
                raise ValueError("invalid Muster width")
            int(expected)
            projected, _, _ = self.view(width)
            if not any(item.session.ref.key == key for item in projected):
                raise LookupError(f"session is not in the displayed view: {key}")
            if operation == "archive":
                session, host, authority = self.archive_authority(key)
                self.pending_archives.add(key)
                self.view_revision += 1
                self._view_cache = None
                self.action_error = ""
                action, artifacts = self.publish_view(width)
                task = asyncio.create_task(
                    self.complete_archive(key, host, authority, artifacts))
                self.own_task(task, "archive")
                return action
            await self.action({"operation": operation, "source": key})
            self.action_error = ""
            return self.publish_view(width)[0]
        except (LookupError, OSError, RuntimeError, ValueError) as error:
            self.action_error = str(error)
            return self.publish_view(width, self.action_error)[0]

    def background_task_done(self, task):
        self.background_tasks.discard(task)
        name = self.task_names.pop(task)
        if not task.cancelled():
            if error := task.exception():
                journal.record("daemon_task_failed", task=name,
                               error_type=type(error).__name__)

    def own_task(self, task, name):
        self.background_tasks.add(task)
        self.task_names[task] = name
        task.add_done_callback(self.background_task_done)

    async def publish_current_view(self, error=""):
        path = RUNTIME / "muster.sock"
        if not path.exists():
            return
        action, artifacts = self.publish_view(self.view_width, error)
        await self.send_publication(path, action, artifacts)

    @staticmethod
    async def wait_for_publication(artifacts):
        while any(path.exists() for path in artifacts):
            await asyncio.sleep(.001)

    async def complete_archive(self, key, host, authority, artifacts):
        error = ""
        try:
            viewers = await self.viewers()
            await self.update_viewers(viewers, f"CLEAR {key}")
            await self.authority(host, authority)
            await self.wait_for_absence(key)
        except (LookupError, OSError, RuntimeError, ValueError) as caught:
            error = str(caught)
        await self.wait_for_publication(artifacts)
        async with self.view_lock:
            self.pending_archives.discard(key)
            self.view_revision += 1
            self._view_cache = None
            self.action_error = error
            await self.publish_current_view(error)

    async def register_muster(self, generation, width):
        socket_path, pid, started, session_id = generation
        if not socket_path or not Path(socket_path).is_socket():
            raise ValueError("invalid Muster tmux socket")
        if proc.start_time(pid) != started or not re.fullmatch(r"\$[0-9]+", session_id):
            raise ValueError("invalid Muster tmux generation")
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/tmux", "-N", "-S", socket_path, "display-message", "-p",
            "-t", "=fleet@muster:", "#{pid}\t#{session_id}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        if process.returncode or stdout.decode().rstrip("\n") != f"{pid}\t{session_id}":
            raise ValueError(stderr.decode().strip() or "Muster tmux identity changed")
        if proc.start_time(pid) != started:
            raise ValueError("Muster tmux generation changed")
        async with self.view_lock:
            if generation != self.muster_generation:
                self.muster_generation = generation
                self.expanded.clear()
                self.show_python = False
                self.action_error = ""
                self.view_revision += 1
                self._view_cache = None
                for artifact in RUNTIME.glob("muster-view-*.*"):
                    artifact.unlink(missing_ok=True)
            self.view_width = width
        return "OK"

    async def register_existing_muster(self):
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/tmux", "-N", "display-message", "-p",
            "-t", "=fleet@muster:",
            "#{socket_path}\t#{pid}\t#{session_id}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await process.communicate()
        if process.returncode:
            return
        socket_path, pid, session_id = stdout.decode().rstrip("\n").split("\t")
        await self.register_muster(
            (socket_path, int(pid), proc.start_time(int(pid)), session_id),
            self.view_width)

    def next_waiting(self, active):
        waiting = [item.session for item in self.projected()
                   if item.session.state == "waiting"]
        if not waiting:
            return ""
        current = next((i for i, session in enumerate(waiting)
                        if session.ref.key == active), -1)
        return waiting[(current + 1) % len(waiting)].ref.key

    def composed_graph(self):
        generation, composed = self._composed
        if generation != self.observed:
            graphs = [graph for graph in self.graphs.values()
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
                "unavailable": sorted(self.presentation_unavailable()),
                "history": history,
                "workstations": workstations}
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False).encode()
        return {**body, "revision": hashlib.sha256(canonical).hexdigest()}

    async def history(self):
        source_hosts = sorted(set(hosts()) - self.unavailable)
        sessions = [
            {"host": session.ref.server.host, "agent": session.agent,
             "transcript_id": session.transcript_id}
            for group in self.sessions.values() for session in group
        ]
        results = await asyncio.gather(
            *(self.history_observation(host) for host in source_hosts),
            return_exceptions=True)
        observed = [(host, result) for host, result in zip(source_hosts, results)
                    if not isinstance(result, Exception)]
        return self.history_entries(sessions, [host for host, _ in observed],
                                    [result for _, result in observed])

    def source(self, key):
        if key in self.pending_archives:
            raise LookupError(f"session is being archived: {key}")
        matches = [session for group in self.sessions.values()
                   for session in group if session.ref.key == key]
        if len(matches) != 1:
            raise LookupError(f"session disappeared: {key}")
        session = matches[0]
        if session.ref.server.host in self.unavailable:
            raise RuntimeError(
                f"{session.ref.server.host} is disconnected; refusing action"
            )
        return session

    def available(self, host):
        if host not in hosts() or host in self.unavailable:
            raise RuntimeError(f"{host} is disconnected; refusing action")

    async def wait_for_source(self, predicate, description):
        try:
            async with asyncio.timeout(30):
                while True:
                    async with self.changed:
                        if source := next((session for group in self.sessions.values()
                                           for session in group if predicate(session)), None):
                            return source.ref.key
                        generation = self.observed
                        await self.changed.wait_for(lambda: self.observed != generation)
        except TimeoutError:
            raise RuntimeError(f"Fleet projection did not {description}") from None

    async def wait_for_absence(self, key):
        try:
            async with asyncio.timeout(30):
                while True:
                    async with self.changed:
                        if not any(session.ref.key == key for group in self.sessions.values()
                                   for session in group):
                            return
                        generation = self.observed
                        await self.changed.wait_for(lambda: self.observed != generation)
        except TimeoutError:
            raise RuntimeError(f"Fleet projection did not archive {key}") from None

    @staticmethod
    def action_name(value):
        if not isinstance(value, str):
            raise ValueError("session name is required")
        value = value.strip().strip(".:").replace(".", "-").replace(":", "-")
        if not value:
            raise ValueError("session name is required")
        return value

    async def authority(self, host, request):
        self.available(host)
        return await self.remote_json(
            host, sys.executable, "-c",
            "import sys; from agent_fleet.authority import execute_json; "
            "print(execute_json(sys.argv[1]))",
            json.dumps(request, separators=(",", ":")))

    async def viewers(self, source=None):
        found = []
        for path in sorted(RUNTIME.glob("viewer-*.sock")):
            try:
                reader, writer = await asyncio.open_unix_connection(path)
                writer.write(b"SOURCE\n")
                await writer.drain()
                current = (await reader.readline()).decode().rstrip("\n")
                writer.close()
                await writer.wait_closed()
            except OSError:
                continue
            if source is None or current == source:
                found.append(path)
        return found

    @staticmethod
    async def update_viewer(path, message):
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write((message + "\n").encode())
        await writer.drain()
        reply = (await reader.readline()).decode().rstrip("\n")
        writer.close()
        await writer.wait_closed()
        if reply != "OK":
            raise RuntimeError(reply or f"viewer {path.name} did not acknowledge")

    async def update_viewers(self, paths, message):
        errors = []
        for path in paths:
            try:
                await self.update_viewer(path, message)
            except (OSError, RuntimeError) as error:
                errors.append(f"{path.name}: {error}")
        if errors:
            raise RuntimeError("; ".join(errors))

    def archive_authority(self, key):
        session = self.source(key)
        host = session.ref.server.host
        self.available(host)
        if session.ref.server.kind == "alan":
            if session.agent not in {"llm", "claude", "codex"}:
                raise ValueError("archive requires a language actor")
            if session.attachment:
                if not session.transcript_id:
                    raise ValueError("archive requires a durable Claude or Codex identity")
                authority = {"operation": "archive-composite",
                             "actor": session.ref.session_id,
                             "agent": session.agent,
                             "source": session.attachment.key,
                             "transcript": session.transcript_id}
            else:
                authority = {"operation": "archive-alan",
                             "actor": session.ref.session_id,
                             "agent": session.agent}
        else:
            if session.agent not in {"claude", "codex"} or not session.transcript_id:
                raise ValueError("archive requires a durable Claude or Codex identity")
            authority = {"operation": "archive-tmux", "source": key,
                         "agent": session.agent,
                         "transcript": session.transcript_id}
        return session, host, authority

    async def action(self, request):
        operation = request.get("operation")
        fields = {
            "create": {"operation", "host", "agent", "name", "cwd"},
            "rename": {"operation", "source", "name"},
            "archive": {"operation", "source"},
            "refresh": {"operation", "source"},
            "restore": {"operation", "history", "name"},
        }
        if operation not in fields or set(request) != fields[operation]:
            raise ValueError("invalid Fleet action")
        if any(not isinstance(value, str) for value in request.values()):
            raise ValueError("invalid Fleet action")
        if operation == "create":
            host = request["host"]
            self.available(host)
            if request["agent"] not in {"claude", "codex"}:
                raise ValueError("create requires Claude or Codex")
            if not isinstance(request["cwd"], str) or not request["cwd"]:
                raise ValueError("create requires a directory")
            name = self.action_name(request["name"])
            value = await self.authority(host, {
                "operation": "create", "agent": request["agent"],
                "name": name, "cwd": request["cwd"],
            })
            key = value["source"]
            await self.wait_for_source(lambda session: session.ref.key == key,
                                       f"create {key}")
            return {"source": key}

        if operation == "restore":
            key = request["history"]
            if key.startswith("alan:"):
                actor = key.removeprefix("alan:")
                if actor.count("@") != 1 or not all(actor.split("@", 1)):
                    raise ValueError("invalid Alan history identity")
                host = actor.rsplit("@", 1)[1]
                self.available(host)
                await self.authority(host, {"operation": "restore-alan",
                                            "actor": actor})
                await self.wait_for_source(lambda session: session.ref.key == key,
                                           f"restore {key}")
                return {"source": key}
            try:
                host, agent, transcript = key.split(":", 2)
            except ValueError:
                raise ValueError("invalid transcript history identity") from None
            self.available(host)
            if agent not in {"claude", "codex"} or not transcript:
                raise ValueError("invalid transcript history identity")
            if any(session.ref.server.host == host and session.agent == agent
                   and session.transcript_id == transcript
                   for group in self.sessions.values() for session in group):
                raise ValueError("that transcript already has a live session")
            name = self.action_name(request["name"])
            await self.authority(host, {
                "operation": "restore-transcript", "agent": agent,
                "transcript": transcript, "name": name,
            })
            source = await self.wait_for_source(
                lambda session: session.ref.server.host == host
                and session.agent == agent and session.transcript_id == transcript,
                f"restore {key}")
            return {"source": source}

        key = request["source"]
        session = self.source(key)
        host = session.ref.server.host
        if operation == "refresh":
            viewers = await self.viewers(key)
        elif operation == "archive":
            viewers = await self.viewers()
        else:
            viewers = []
        if operation == "rename":
            name = self.action_name(request["name"])
            authority = ({"operation": "rename-alan",
                          "actor": session.ref.session_id, "name": name}
                         if session.ref.server.kind == "alan" else
                         {"operation": "rename-tmux", "source": key,
                          "name": name})
            return await self.authority(host, authority)
        if operation == "refresh":
            if session.ref.server.kind != "alan":
                raise ValueError("refresh requires an Alan actor")
            if session.agent not in {"claude", "codex"}:
                raise ValueError("refresh requires a Claude or Codex actor")
            if session.state != "waiting":
                raise ValueError(f"refresh requires a waiting actor: {session.ref.session_id}")
            value = await self.authority(host, {
                "operation": "refresh", "actor": session.ref.session_id,
            })
            self.source(key)
            await self.update_viewers(viewers, f"OPEN {key}")
            return value
        _, host, authority = self.archive_authority(key)
        self.pending_archives.add(key)
        try:
            await self.update_viewers(viewers, f"CLEAR {key}")
            await self.authority(host, authority)
            await self.wait_for_absence(key)
        finally:
            self.pending_archives.discard(key)
        return {}

    async def remote_json(self, host, *command):
        target = ("/usr/bin/env", "-u", "LOOP_SOCKET", "-u", "LOOP_CAPABILITIES",
                  *command)
        argv = list(target) if host == os.uname().nodename.split(".", 1)[0] else [
            "ssh", "-T", "-o", "BatchMode=yes", host, shlex.join(target)]
        environment = ssh_environment()
        environment.pop("LOOP_SOCKET", None)
        environment.pop("LOOP_CAPABILITIES", None)
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=environment)
        stdout, stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError(stderr.decode().strip() or f"{host}: {' '.join(command)} failed")
        return json.loads(stdout)

    async def history_observation(self, host):
        graph = self.graphs.get(host)
        actors = [] if graph is None else graph.graph.get("actors", [])
        transcripts = await self.remote_json(
            host, sys.executable, "-c",
            "import json; from agent_fleet.transcripts import history; "
            "print(json.dumps(history(100)))",
        )
        return {"host": host, "actors": actors, "transcripts": transcripts}

    async def search_observation(self, host, query):
        graph = self.graphs.get(host)
        hits = await self.remote_json(
            host, sys.executable, "-c",
            "import json,sys; from agent_fleet.transcripts import search; "
            "print(json.dumps(search(sys.argv[1])))",
            query,
        )
        return {"actors": [] if graph is None else graph.graph.get("actors", []),
                "hits": hits}

    async def search_history(self, query):
        if not isinstance(query, str) or not query:
            raise ValueError("history search query is required")
        source_hosts = sorted(set(hosts()) - self.unavailable)
        observations = await asyncio.gather(
            *(self.search_observation(host, query) for host in source_hosts))
        rows = []
        for host, observation in zip(source_hosts, observations):
            owners = {}
            for actor in observation["actors"]:
                if actor.get("kind") not in {"claude", "codex"}:
                    continue
                identity = actor["kind"], address_identity(actor["addr"], actor["kind"])
                owners.setdefault(identity, []).append(actor)
            for hit in observation["hits"]:
                matches = owners.get((hit["agent"], hit["session_id"]), [])
                if len(matches) > 1:
                    addresses = ", ".join(sorted(actor["addr"] for actor in matches))
                    raise RuntimeError(
                        f"ambiguous {hit['agent']} transcript ownership "
                        f"{hit['session_id']}: {addresses}"
                    )
                if matches:
                    actor = matches[0]
                    source = f"alan:{actor['addr']}"
                    name = actor.get("label") or actor["addr"]
                    lifecycle = actor.get("state") or ""
                else:
                    source = f"{host}:{hit['agent']}:{hit['session_id']}"
                    name = Path(hit["cwd"]).name or hit["agent"]
                    lifecycle = "standalone"
                rows.append({**hit, "host": host, "source": source,
                             "name": name, "lifecycle": lifecycle})
        return rows

    @staticmethod
    def history_entries(sessions, source_hosts, observations):
        live = {(item["host"], item["agent"], item.get("transcript_id"))
                for item in sessions if item.get("transcript_id")}
        authorities = set(live)
        entries = []
        for host, observation in zip(source_hosts, observations):
            claimed = {}
            for actor in observation["actors"]:
                native_id = address_identity(actor["addr"], actor.get("kind"))
                identity = host, actor.get("kind"), native_id
                retained = (actor.get("kind") == "llm" or
                            actor.get("kind") in {"claude", "codex"})
                if native_id:
                    claimed.setdefault(identity, []).append(actor["addr"])
                if retained and actor.get("state") in {"retired", "unavailable"}:
                    if native_id:
                        authorities.add(identity)
                    entries.append({"key": f'alan:{actor["addr"]}', "host": host,
                                    "agent": actor["kind"],
                                    "name": actor.get("label") or actor["addr"],
                                    "cwd": actor.get("cwd") or "",
                                    "mtime": max(actor.get("human_activity", 0),
                                                 actor.get("created", 0))})
            duplicates = {identity: addresses for identity, addresses in claimed.items()
                          if len(addresses) > 1}
            if duplicates:
                identity, addresses = next(iter(duplicates.items()))
                raise RuntimeError(
                    f"ambiguous {identity[1]} transcript ownership {identity[2]}: "
                    + ", ".join(sorted(addresses)))
            authorities.update(claimed)
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
        await self.register_existing_muster()
        configured = hosts()
        fields = {"socket": str(path), "hosts_text": " ".join(configured)}
        journal.record("daemon_ready", **fields)
        try:
            async with server:
                async with asyncio.TaskGroup() as group:
                    group.create_task(server.serve_forever())
                    for host in configured:
                        group.create_task(self.collect(host))
        finally:
            journal.record("daemon_stopping", **fields)
            for task in tuple(self.background_tasks):
                task.cancel()
            await asyncio.gather(*tuple(self.background_tasks), return_exceptions=True)

    async def preview(self, key, columns=0, lines=0):
        session = next((session for group in self.sessions.values() for session in group
                        if session.ref.key == key), None)
        if session is None:
            raise RuntimeError(f"session disappeared: {key}")
        host = key_host(key)
        if host in self.unavailable:
            raise RuntimeError(f"{host} is disconnected; refusing action")
        if host in self.tmux_unavailable and not key.startswith("alan:"):
            raise RuntimeError(f"{host} tmux server is unavailable")
        process = self.processes[host]
        assert process.stdin
        self.next_preview += 1
        number = self.next_preview
        future = asyncio.get_running_loop().create_future()
        self.previews[number] = (host, future)
        source = session.attachment.key if session.attachment else key
        process.stdin.write((json.dumps({"preview": number, "key": source,
                                         "columns": columns, "lines": lines}) + "\n").encode())
        await process.stdin.drain()
        return await future

    async def switch(self, key, client):
        session = self.source(key)
        host = key_host(key)
        if host in self.tmux_unavailable:
            raise RuntimeError(f"{host} tmux server is unavailable")
        self.next_switch += 1
        number = self.next_switch
        future = asyncio.get_running_loop().create_future()
        self.switches[number] = (host, future)
        payload = {"switch": number, "client": client}
        if session.attachment:
            payload["target"] = split_key(session.attachment.key)[1:]
        elif key.startswith("alan:"):
            payload["actor"] = key.removeprefix("alan:")
            payload["agent"] = session.agent
            payload["cwd"] = session.cwd
        else:
            payload["target"] = split_key(key)[1:]
        process = self.processes[host]
        assert process.stdin
        process.stdin.write((json.dumps(payload) + "\n").encode())
        await process.stdin.drain()
        return await future

    async def cleanup(self, host, owner, slot):
        if host in self.unavailable:
            raise RuntimeError(f"{host} is disconnected; refusing cleanup")
        self.next_cleanup += 1
        number = self.next_cleanup
        future = asyncio.get_running_loop().create_future()
        self.cleanups[number] = (host, future)
        process = self.processes[host]
        assert process.stdin
        process.stdin.write((json.dumps({"cleanup": number, "owner": owner,
                                         "slot": slot}) + "\n").encode())
        await process.stdin.drain()
        await future


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


def history():
    return request("history")


def history_search(query):
    response = json.loads(request("history-search " + json.dumps(query)))
    if set(response) == {"ok", "error"} and response["ok"] is False:
        raise RuntimeError(response["error"])
    if set(response) != {"ok", "value"} or response["ok"] is not True:
        raise RuntimeError("invalid Fleet history search response")
    return response["value"]


def action(envelope):
    response = json.loads(request(json.dumps(envelope, separators=(",", ":"))))
    if set(response) == {"ok", "error"} and response["ok"] is False:
        raise RuntimeError(response["error"])
    if set(response) != {"ok", "value"} or response["ok"] is not True:
        raise RuntimeError("invalid Fleet action response")
    return response["value"]
