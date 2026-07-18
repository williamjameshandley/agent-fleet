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
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from .archive import Archive, ROOT
from .audio import Capture, WakeDetector
from . import delivery, destination
from .editor import edit
from .model import Composition, Destination, Mode, classify
from .transcribe import Transcriber


RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "agent-fleet"
SOCKET = RUNTIME / "alan.sock"


class TextPane:
    def __init__(self, name, editable=True):
        self.view = Gtk.TextView()
        self.view.set_name(name)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.view.set_editable(editable)
        self.view.set_cursor_visible(editable)
        self.view.set_left_margin(6)
        self.view.set_right_margin(6)
        self.view.set_top_margin(4)
        self.view.set_bottom_margin(4)
        self.buffer = self.view.get_buffer()
        self.widget = Gtk.ScrolledWindow()
        self.widget.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.widget.add(self.view)

    def connect(self, signal, callback):
        return self.buffer.connect(signal, lambda buffer: callback(self))

    def get_text(self):
        return self.buffer.get_text(
            self.buffer.get_start_iter(), self.buffer.get_end_iter(), True)

    def set_text(self, text):
        self.buffer.set_text(text)

    def set_position(self, position):
        iterator = self.buffer.get_end_iter() if position < 0 else self.buffer.get_iter_at_offset(position)
        self.buffer.place_cursor(iterator)
        self.view.scroll_mark_onscreen(self.buffer.get_insert())

    def content_height(self):
        iterator = self.buffer.get_end_iter()
        y, height = self.view.get_line_yrange(iterator)
        return y + height + 8


class Composer:
    def __init__(self):
        self.composition = None
        self.archive = Archive()
        self.transcriber = Transcriber(self._utterance)
        self.pool = ThreadPoolExecutor(max_workers=3)
        self.opening_from_wake = False
        self.paused_wake = False
        self.partial_base = None
        self.rendering_partial = False
        self.geometry = None
        self.resize_pending = False
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
        self.transcriber.start(preroll)
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
        self.transcriber.start()
        self.entry.set_text(item["draft"])
        self.entry.set_position(-1)
        self.activity.set_text("")
        self._show("LISTENING", "Recovered")
        self.archive.record(self.composition, "recovered",
                            source=item["composition"], destination=self._destination_data())

    def _audio(self, block):
        if self.composition and self.composition.mode is Mode.RECORDING:
            self.transcriber.feed(block)
        if self.wake and (not self.composition or self.composition.mode is Mode.PAUSED):
            self.wake.feed(block)

    def _wake(self):
        GLib.idle_add(self._activate_wake, self.capture.preroll())

    def _activate_wake(self, preroll):
        if self.composition and self.composition.mode is Mode.PAUSED:
            self.paused_wake = True
            self.transcriber.start(preroll)
        else:
            self.open(preroll, True)
        return False

    def _utterance(self, event):
        GLib.idle_add(self._transcript, event)

    def _transcript(self, event):
        if not self.composition:
            return False
        if event.get("type") == "partial":
            if self.partial_base is None:
                self.partial_base = self.composition.draft
            self.rendering_partial = True
            self.entry.set_text(self._joined(self.partial_base, event["text"]))
            self.entry.set_position(-1)
            self.rendering_partial = False
            self._render_status("TRANSCRIBING")
            return False
        base = self.partial_base
        self.partial_base = None
        if base is not None:
            self.composition = replace(self.composition, draft=base)
            self.rendering_partial = True
            self.entry.set_text(base)
            self.rendering_partial = False
        return self._raw(event["text"])

    @staticmethod
    def _joined(before, text):
        return f"{before.rstrip()} {text}" if before.strip() else text

    def _raw(self, raw):
        if not self.composition:
            return False
        opening = self.opening_from_wake
        self.opening_from_wake = False
        kind, value = classify(raw, opening=opening)
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
            self.transcriber.stop()
            if self.wake:
                self.wake.reset()
            self._render_status("PAUSED")
        elif command == "resume":
            self.composition = self.composition.resume()
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
        self.partial_base = None
        self.transcriber.stop()
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
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        status = Gtk.Label(label="LISTENING")
        target = Gtk.Label(label="NO DESTINATION")
        entry = TextPane("draft")
        activity = TextPane("activity", editable=False)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.pack_start(status, False, False, 2)
        footer.pack_end(target, False, False, 2)
        right.pack_start(activity.widget, True, True, 0)
        right.pack_end(footer, False, False, 0)
        panes = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        panes.set_hexpand(True)
        panes.pack1(entry.widget, True, False)
        panes.pack2(right, True, False)
        box.pack_start(panes, True, True, 0)
        window.add(box)
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            #alan-composer { background: rgba(29,32,33,.94); border: 2px solid #a89984; }
            textview, textview text, scrolledwindow {
                background-color: #282828;
                color: #ebdbb2;
                border: 0;
                font: 13pt 'Source Code Pro';
            }
            #activity, #activity text { background-color: #32302f; color: #d5c4a1; }
            label { color: #ebdbb2; font: 11pt 'Source Code Pro'; padding: 4px; }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.panes = panes
        return window, entry, status, target, activity

    def _entry_changed(self, entry):
        if self.composition and not self.rendering_partial:
            self.partial_base = None
            self.composition = replace(self.composition, draft=entry.get_text())
        self._queue_resize()

    def _show(self, status, activity):
        screen = self.window.get_screen()
        display = screen.get_display()
        monitor = display.get_monitor_at_point(*display.get_default_seat().get_pointer().get_position()[1:])
        geometry = monitor.get_geometry()
        self.geometry = geometry
        self._render_status(status)
        self.target.set_text(self.composition.destination.label if self.composition.destination else "NO DESTINATION")
        self._log(activity)
        self.window.show_all()
        self.window.move(geometry.x, geometry.y)
        self._queue_resize()
        self.window.present()
        self.entry.view.grab_focus()

    def _render_status(self, value):
        if self.composition and self.composition.queued:
            value += f" · {self.composition.queued} QUEUED"
        self.status.set_text(value)
        return False

    def _log(self, message):
        history = self.activity.get_text()
        self.activity.set_text(f"{history}\n{message}" if history else message)
        self.activity.set_position(-1)
        self._queue_resize()
        return False

    def _highlight(self, before, after):
        start = 0
        for old, new in zip(before, after):
            if old != new:
                break
            start += 1
        self.entry.buffer.remove_all_tags(
            self.entry.buffer.get_start_iter(), self.entry.buffer.get_end_iter())
        tag = self.entry.buffer.create_tag(None, background="#504945")
        self.entry.buffer.apply_tag(
            tag,
            self.entry.buffer.get_iter_at_offset(start),
            self.entry.buffer.get_end_iter())
        GLib.timeout_add(1400, self._clear_highlight)

    def _clear_highlight(self):
        self.entry.buffer.remove_all_tags(
            self.entry.buffer.get_start_iter(), self.entry.buffer.get_end_iter())
        return False

    def _queue_resize(self):
        if self.window.get_visible() and not self.resize_pending:
            self.resize_pending = True
            GLib.idle_add(self._resize)

    def _resize(self):
        self.resize_pending = False
        if not self.geometry:
            return False
        self.panes.set_position(round(self.panes.get_allocated_width() * 2 / 3))
        content = self.entry.content_height()
        height = min(max(58, content + 30), max(58, self.geometry.height // 3))
        self.window.resize(self.geometry.width, height)
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
