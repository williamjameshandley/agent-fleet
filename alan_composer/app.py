# ruff: noqa: E402
import os
import socket
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import gi
gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from .archive import Archive, ROOT
from .audio import Capture, RATE, Segmenter, WakeDetector
from . import delivery, destination
from .editor import edit
from .model import Composition, Destination, Mode, classify
from .transcribe import Transcriber


RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "agent-fleet"
SOCKET = RUNTIME / "alan.sock"


class Composer:
    def __init__(self):
        self.composition = None
        self.archive = Archive()
        self.transcriber = Transcriber()
        self.pool = ThreadPoolExecutor(max_workers=3)
        self.pending = {}
        self.opening_from_wake = False
        self.paused_wake = False
        self.segmenter = Segmenter(self._segment)
        self.window, self.entry, self.status, self.target, self.activity = self._window()
        self.entry.connect("changed", self._entry_changed)
        self.wake = None
        self.capture = Capture(ROOT / "ambient", self._audio)
        if model := os.environ.get("ALAN_WAKE_MODEL"):
            self.wake = WakeDetector(model, self._wake)

    def start(self):
        self.capture.start()
        threading.Thread(target=self._serve, daemon=True).start()
        Gtk.main()

    def open(self, preroll=(), wake=False):
        if self.composition:
            return
        self.composition = Composition(destination=destination.capture())
        self.opening_from_wake = wake
        self.segmenter.start(preroll)
        self.entry.set_text("")
        self.activity.set_text("")
        self._show("LISTENING", "Opened")
        self.archive.record(self.composition, "opened",
                            destination=self._destination_data())

    def recover(self):
        if self.composition or not (item := self.archive.latest()):
            return
        target = Destination(**item["destination"]) if item.get("destination") else None
        self.composition = Composition(draft=item["draft"], destination=target)
        self.segmenter.start()
        self.entry.set_text(item["draft"])
        self.entry.set_position(-1)
        self.activity.set_text("")
        self._show("LISTENING", "Recovered")
        self.archive.record(self.composition, "recovered",
                            source=item["composition"], destination=self._destination_data())

    def _audio(self, block):
        self.segmenter.feed(block)
        if self.wake and (not self.composition or self.composition.mode is Mode.PAUSED):
            self.wake.feed(block)

    def _wake(self):
        GLib.idle_add(self._activate_wake, self.capture.preroll())

    def _activate_wake(self, preroll):
        if self.composition and self.composition.mode is Mode.PAUSED:
            self.paused_wake = True
            self.segmenter.start(preroll)
        else:
            self.open(preroll, True)
        return False

    def _segment(self, audio):
        if not self.composition or (
                self.composition.mode is not Mode.RECORDING and not self.paused_wake):
            return
        current = self.composition
        if self.paused_wake:
            self.paused_wake = False
            self.segmenter.stop()
        self.composition = replace(current, queued=current.queued + 1)
        token = object()
        path = self.archive.audio(current, audio, RATE)
        opening = self.opening_from_wake
        self.opening_from_wake = False
        self.pending[token] = (current, audio, str(path), opening)
        self.archive.record(current, "utterance", audio=str(path))
        GLib.idle_add(self._render_status, "TRANSCRIBING")
        self.pool.submit(self._transcribe, token, current.id, audio)

    def _transcribe(self, token, composition_id, audio):
        try:
            raw = self.transcriber(audio, RATE)
        except Exception as error:
            GLib.idle_add(self._transcription_failed, token, composition_id, str(error))
            return
        GLib.idle_add(self._raw, token, composition_id, raw)

    def _raw(self, token, composition_id, raw):
        pending = self.pending.pop(token, None)
        if not self.composition or self.composition.id != composition_id:
            if pending:
                self.archive.record(pending[0], "late_transcription", raw=raw,
                                    audio=pending[2])
            return False
        self.composition = replace(self.composition, queued=self.composition.queued - 1)
        kind, value = classify(raw, opening=pending[3])
        self.archive.record(self.composition, "transcribed", raw=raw, kind=kind)
        if kind == "control":
            self._control(value)
        elif kind == "instruction":
            self._run_edit(value, "")
        else:
            before = self.entry.get_text()
            self.composition = self.composition.append(value)
            self.entry.set_text(self.composition.draft)
            self.entry.set_position(-1)
            self._highlight(before, self.composition.draft)
            self._run_edit(None, raw)
        self._render_status("LISTENING" if self.composition.mode is Mode.RECORDING else "PAUSED")
        if self.wake and self.composition.mode is Mode.PAUSED:
            self.wake.reset()
        return False

    def _run_edit(self, instruction, raw):
        current = self.composition
        self._log("Editing")
        self.pool.submit(self._edit, current.id, current.draft, raw, instruction)

    def _edit(self, composition_id, draft, raw, instruction):
        try:
            result = edit(draft, raw, instruction,
                          {"destination": self._destination_data()})
        except Exception as error:
            GLib.idle_add(self._log, f"Edit failed: {error}")
            return
        GLib.idle_add(self._edited, composition_id, draft, result)

    def _edited(self, composition_id, previous, result):
        if not self.composition or self.composition.id != composition_id:
            return False
        if self.entry.get_text() != previous:
            self._log("Edit archived; draft advanced")
            return False
        self.composition = replace(self.composition, draft=result["draft"])
        self.entry.set_text(result["draft"])
        self.entry.set_position(-1)
        self._highlight(previous, result["draft"])
        self._log(result["log"])
        self.archive.record(self.composition, "edited", before=previous,
                            after=result["draft"], log=result["log"])
        return False

    def _control(self, command):
        if command == "pause":
            self.composition = self.composition.pause()
            self.segmenter.stop()
            if self.wake:
                self.wake.reset()
            self._render_status("PAUSED")
        elif command == "resume":
            self.composition = self.composition.resume()
            self.segmenter.start()
            self._render_status("LISTENING")
        elif command == "cancel":
            self._close("cancelled")
        elif command == "send":
            self._send()

    def _send(self):
        visible = self.entry.get_text()
        if not self.composition.destination:
            self._log("NO DESTINATION · prompt not sent")
            return
        try:
            delivery.send(self.composition.destination, visible)
        except subprocess.CalledProcessError as error:
            self._log(f"DELIVERY FAILED · {error}")
            return
        self.archive.record(self.composition, "sent", draft=visible,
                            destination=self._destination_data())
        self._close("sent", archive=False)

    def _close(self, outcome, archive=True):
        if archive:
            self.archive.record(self.composition, outcome, draft=self.entry.get_text(),
                                destination=self._destination_data())
        self.composition = None
        self.segmenter.stop()
        if self.wake:
            self.wake.reset()
        self.window.hide()

    def _window(self):
        window = Gtk.Window(title="Alan Composer")
        window.set_name("alan-composer")
        window.set_decorated(False)
        window.set_keep_above(True)
        window.set_skip_taskbar_hint(True)
        window.set_default_size(1920, 42)
        window.connect("key-press-event", self._key)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        status = Gtk.Label(label="LISTENING")
        target = Gtk.Label(label="NO DESTINATION")
        entry = Gtk.Entry()
        entry.set_hexpand(True)
        activity = Gtk.Entry()
        activity.set_editable(False)
        activity.set_can_focus(False)
        activity.set_width_chars(38)
        box.pack_start(status, False, False, 6)
        box.pack_start(target, False, False, 0)
        box.pack_start(entry, True, True, 0)
        box.pack_start(activity, False, False, 6)
        window.add(box)
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            #alan-composer { background: rgba(29,32,33,.94); border: 2px solid #a89984; }
            entry { background: #282828; color: #ebdbb2; border: 0; padding: 2px; }
            label { color: #ebdbb2; font: 10pt 'Source Code Pro Light'; }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        return window, entry, status, target, activity

    def _entry_changed(self, entry):
        if self.composition:
            self.composition = replace(self.composition, draft=entry.get_text())

    def _show(self, status, activity):
        screen = self.window.get_screen()
        display = screen.get_display()
        monitor = display.get_monitor_at_point(*display.get_default_seat().get_pointer().get_position()[1:])
        geometry = monitor.get_geometry()
        self._render_status(status)
        self.target.set_text(self.composition.destination.label if self.composition.destination else "NO DESTINATION")
        self._log(activity)
        self.window.show_all()
        self.window.resize(geometry.width, 42)
        self.window.move(geometry.x, geometry.y)
        self.window.present()
        self.entry.grab_focus()

    def _render_status(self, value):
        if self.composition and self.composition.queued:
            value += f" · {self.composition.queued} QUEUED"
        self.status.set_text(value)
        return False

    def _transcription_failed(self, token, composition_id, error):
        if self.composition and self.composition.id == composition_id:
            self._render_status("GROQ UNAVAILABLE")
            self._log(error)
        GLib.timeout_add_seconds(5, self._retry, token)
        return False

    def _retry(self, token):
        pending = self.pending.get(token)
        if pending:
            composition, audio, _path = pending
            self.pool.submit(self._transcribe, token, composition.id, audio)
        return False

    def _log(self, message):
        history = self.activity.get_text()
        self.activity.set_text(f"{history}  ·  {message}" if history else message)
        self.activity.set_position(-1)
        return False

    def _highlight(self, before, after):
        start = 0
        for old, new in zip(before, after):
            if old != new:
                break
            start += len(new.encode())
        attributes = Pango.AttrList()
        colour = Pango.attr_background_new(80 * 257, 73 * 257, 69 * 257)
        colour.start_index = start
        colour.end_index = len(after.encode())
        attributes.insert(colour)
        self.entry.set_attributes(attributes)
        GLib.timeout_add(1400, self._clear_highlight)

    def _clear_highlight(self):
        self.entry.set_attributes(Pango.AttrList())
        return False

    def _destination_data(self):
        destination = self.composition.destination if self.composition else None
        return destination.__dict__ if destination else None

    def _key(self, _window, event):
        if event.keyval == Gdk.KEY_Escape and self.composition:
            self._close("cancelled")
            return True
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and self.composition:
            self._send()
            return True
        return False

    def _serve(self):
        RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
        SOCKET.unlink(missing_ok=True)
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(SOCKET))
            os.chmod(SOCKET, 0o600)
            server.listen()
            while True:
                client, _ = server.accept()
                with client:
                    command = client.recv(32).strip()
                    if command == b"OPEN":
                        GLib.idle_add(self.open)
                    elif command == b"RECOVER":
                        GLib.idle_add(self.recover)
