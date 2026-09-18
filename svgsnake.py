"""
SVGSnake — Minimize your SVG footprint
====================================
A desktop front-end for `svgo-py <https://pypi.org/project/svgo-py/>`_ — the
pure-Python port of SVGO. Open (or drag & drop) SVG files, tune every SVGO
plugin with a live preview, flip between *Original* and *Optimized*, and write
the result back with a configurable file-name prefix.

Modelled on SVGOMG (https://svgomg.net) for the settings, and on the
Recraft Vectorizer for the dark look and the preview plumbing, with a venomous python green accent.

Run:  python svgsnake.py
"""

from __future__ import annotations

import io
import json
import multiprocessing as mp
import os
import queue
import re
import sys
import threading
import gzip as _gzip
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk

import svgo_engine as engine
import svgo_worker

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _DND_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency
    DND_FILES = None
    TkinterDnD = None
    _DND_AVAILABLE = False


# --------------------------------------------------------------------------- #
#  Constants / palette
# --------------------------------------------------------------------------- #

APP_NAME = "SVGSnake"
APP_SLOGAN = "Minimize your SVG footprint"
APP_VERSION = "1.0.0"
APP_PUBLISHER = "Tobse"

#: Windows task-bar identity. Without it the shell groups the window under the
#: Python interpreter and shows its icon instead of ours.
APP_ID = "Tobse.SVGSnake.1"

CONFIG_DIR = Path.home() / ".svgsnake"
CONFIG_PATH = CONFIG_DIR / "config.json"

#: Settings location used before the app was renamed to SVGSnake. Read once, on
#: first start, so an existing plugin selection survives the rename.
LEGACY_CONFIG_PATH = Path.home() / ".svgo_ui" / "config.json"

#: Directory the app was started from — the source tree when run with Python,
#: the unpacked program folder in a Nuitka build. Bundled assets sit next to it
#: in both cases, so one helper covers both.
APP_DIR = Path(__file__).resolve().parent

ICON_ICO = APP_DIR / "icon" / "icon.ico"
ICON_PNG = APP_DIR / "icon" / "icon.png"

#: In-app artwork. These are SVGs on purpose — the app already carries a
#: browser-faithful SVG renderer, so the icons stay crisp at any size or DPI
#: instead of being resampled from a fixed bitmap.
ICON_SVG = APP_DIR / "icon" / "snake.svg"
ICON_SETTINGS_SVG = APP_DIR / "icon" / "settings.svg"

# Dark theme with a venomous snake green accent (python viper look).
COL_BG = "#161616"
COL_PANEL = "#1f1f1f"
COL_PANEL_2 = "#262626"
COL_BORDER = "#333333"
COL_TEXT = "#ECECEC"
COL_TEXT_DIM = "#9a9a9a"

# Accent: venomous python snake green ("Giftgrün")
COL_ACCENT = "#00E676"
COL_ACCENT_HOVER = "#00C853"
COL_ACCENT_TEXT = "#0E1A10"

# Semantic colors
COL_GREEN = "#00E676"
COL_ERROR = "#E23744"
COL_ERROR_HOVER = "#c02734"
COL_RED = COL_ERROR  # backwards compatibility alias
COL_RED_HOVER = COL_ERROR_HOVER

SVG_FILETYPES = [("SVG images", "*.svg *.svgz"), ("All files", "*.*")]

#: Long edge the preview bitmap is rasterized at. Zooming scales this bitmap.
#: Defined by the worker, which is what actually rasterizes.
RENDER_SIZE = svgo_worker.RENDER_SIZE

#: Above this many characters the markup view is shown without highlighting.
HIGHLIGHT_LIMIT = 200_000
#: Above this many characters the markup view is truncated.
MARKUP_LIMIT = 800_000


# --------------------------------------------------------------------------- #
#  Small helpers
# --------------------------------------------------------------------------- #

def human_size(num_bytes: int | None) -> str:
    """Return a human readable file size like '243.1 KB'."""
    if num_bytes is None:
        return "–"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def saved_percent(before: int, after: int) -> float:
    """Percentage saved going from ``before`` to ``after`` bytes."""
    return (1 - after / before) * 100 if before else 0.0


def _short_path(path: str, keep: int = 2) -> str:
    """Shorten a long path to '…\\parent\\folder' so it fits in one label."""
    if not path:
        return ""
    parts = Path(path).parts
    if len(parts) <= keep + 1:
        return path
    return "…" + os.sep + os.sep.join(parts[-keep:])


def read_svg(path: Path) -> bytes:
    """Read an .svg file, transparently decompressing .svgz."""
    data = path.read_bytes()
    if data[:2] == b"\x1f\x8b":  # gzip magic -> .svgz
        data = _gzip.decompress(data)
    return data


def scaled_px(widget, value: float) -> int:
    """Pixels for artwork on a raw ``tk.Canvas``, matched to CustomTkinter.

    CustomTkinter scales its own widgets and fonts for the display's DPI, but a
    plain canvas is drawn in raw pixels. Without this, hand-drawn artwork would
    be the one thing in the window that does not grow with everything else.
    """
    try:
        return max(1, round(value * ctk.ScalingTracker.get_widget_scaling(widget)))
    except Exception:  # pragma: no cover - scaling is best-effort
        return max(1, round(value))


def virtual_screen(widget) -> tuple[int, int, int, int]:
    """``(x, y, width, height)`` spanning every monitor, not just the primary.

    Tk only knows about the primary screen, which would make a window restored
    onto a second monitor look out of bounds.
    """
    if sys.platform == "win32":
        try:
            import ctypes

            metric = ctypes.windll.user32.GetSystemMetrics
            return metric(76), metric(77), metric(78), metric(79)
        except Exception:  # pragma: no cover - falls back to the primary screen
            pass
    return 0, 0, widget.winfo_screenwidth(), widget.winfo_screenheight()


def fits_on_screen(widget, geometry: str) -> bool:
    """Would a window with this geometry land somewhere the user can reach?

    This guards against a saved position that points at a monitor which has
    since been unplugged — the window would open into nowhere. The margins are
    deliberately generous: it only has to catch "gone entirely", not "hangs
    over the edge a little", which is a perfectly normal thing for a window.
    """
    parsed = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", geometry.strip())
    if parsed is None:
        return False
    width, height, x, y = (int(value) for value in parsed.groups())
    left, top, span_x, span_y = virtual_screen(widget)
    # The saved string is in CustomTkinter's unscaled coordinates, the screen
    # metrics are in real pixels; one scaling factor is close enough for a
    # sanity check, even on a mixed-DPI desk.
    try:
        scale = ctk.ScalingTracker.get_window_scaling(widget)
    except Exception:  # pragma: no cover
        scale = 1.0
    left, top, span_x, span_y = (v / scale for v in (left, top, span_x, span_y))
    # Enough title bar has to be reachable to drag the window back.
    return (x + width > left + 80 and x < left + span_x - 80
            and y + height > top and y < top + span_y - 40)


def png_to_pil(png: bytes | None) -> Image.Image | None:
    """Decode PNG bytes — the form bitmaps travel in — into a PIL image."""
    if not png:
        return None
    try:
        return Image.open(io.BytesIO(png)).convert("RGBA")
    except Exception:
        return None


def render_svg_to_pil(svg_bytes: bytes, target: int = RENDER_SIZE) -> Image.Image | None:
    """Rasterize an SVG document in *this* process. Returns None on failure.

    Rendering blocks the UI (see :mod:`svgo_worker`), so this is only for the
    small, one-off images the window itself is made of — the preview goes
    through :class:`PreviewWorker` instead.
    """
    return png_to_pil(svgo_worker.render_svg_to_png(svg_bytes, target))


# --------------------------------------------------------------------------- #
#  Branding assets
# --------------------------------------------------------------------------- #

def render_svg_icon(path: Path, box: int, tint: str | None = None) -> "ctk.CTkImage | None":
    """Rasterize a bundled SVG icon so it fits a ``box``x``box`` square.

    Rendering happens at 4x and CustomTkinter scales the result down, which
    keeps the icon sharp on high-DPI displays. ``tint`` recolours the artwork by
    keeping only its alpha channel and filling it with one solid colour — these
    icons are single-colour, so that repaints them to any palette entry without
    having to edit the SVG source.

    Returns ``None`` when the asset is missing or no renderer is available; the
    callers fall back to a text glyph.
    """
    try:
        data = path.read_bytes()
    except Exception:
        return None
    img = render_svg_to_pil(data, target=box * 4)
    if img is None:
        return None
    if tint is not None:
        solid = Image.new("RGBA", img.size, tint)
        solid.putalpha(img.getchannel("A"))
        img = solid
    width, height = img.size
    scale = box / max(width, height, 1)
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return ctk.CTkImage(light_image=img, dark_image=img, size=size)


def _load_logo_image(size: int) -> "ctk.CTkImage | None":
    """The snake mark for the sidebar header, or None if no asset renders."""
    snake = render_svg_icon(ICON_SVG, size, tint=COL_ACCENT)
    if snake is not None:
        return snake
    try:  # the app icon, should the SVG or the renderer be unavailable
        img = Image.open(ICON_PNG).convert("RGBA")
    except Exception:
        return None
    return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))


def bind_hover_image(button: ctk.CTkButton, normal, hovered) -> None:
    """Swap a button's icon while the pointer is over it.

    CustomTkinter buttons are composites, so the pointer only ever reaches a
    child widget — every child has to be bound, the same way Tooltip does it.
    """
    def _enter(_event=None):
        button.configure(image=hovered)

    def _leave(_event=None):
        button.configure(image=normal)

    def _bind(widget):
        try:
            widget.bind("<Enter>", _enter, add="+")
            widget.bind("<Leave>", _leave, add="+")
        except Exception:
            pass
        for child in widget.winfo_children():
            _bind(child)

    _bind(button)


def _ensure_ico() -> Path | None:
    """Ensure a Windows .ico is available from ICON_PNG, creating or updating it if needed."""
    if not ICON_PNG.exists():
        return ICON_ICO if ICON_ICO.exists() else None
    try:
        if ICON_ICO.exists() and ICON_ICO.stat().st_mtime >= ICON_PNG.stat().st_mtime:
            return ICON_ICO
        img = Image.open(ICON_PNG)
        try:
            ICON_ICO.parent.mkdir(parents=True, exist_ok=True)
            img.save(
                ICON_ICO,
                format="ICO",
                sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
            )
            return ICON_ICO
        except (OSError, PermissionError):
            fallback = CONFIG_DIR / "icon.ico"
            fallback.parent.mkdir(parents=True, exist_ok=True)
            img.save(
                fallback,
                format="ICO",
                sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
            )
            return fallback
    except Exception:
        return ICON_ICO if ICON_ICO.exists() else None


def apply_window_icon(window: tk.Misc) -> None:
    """Give a window the snake icon — title bar, task bar and Alt-Tab.

    Uses ``icon/icon.png`` as the primary runtime icon. On Windows, also derives
    or applies a multi-size .ico so the native DWM title bar and task bar receive
    crisp HICONs, and marks ``_iconbitmap_method_called`` to prevent CustomTkinter
    from overwriting the icon with its default asset after 200 ms.
    """
    # Prevent CustomTkinter's delayed _windows_set_titlebar_icon timer from
    # overriding our icon with CustomTkinter_icon_Windows.ico.
    setattr(window, "_iconbitmap_method_called", True)

    if ICON_PNG.exists():
        try:
            photo = ImageTk.PhotoImage(Image.open(ICON_PNG))
            window.iconphoto(True, photo)
            window._snake_icon = photo  # Tk does not keep its own reference
        except Exception:
            pass

    if sys.platform == "win32":
        ico = _ensure_ico()
        if ico and ico.exists():
            try:
                window.iconbitmap(str(ico))
            except Exception:
                pass
            try:
                window.iconbitmap(default=str(ico))
            except Exception:
                pass


# --------------------------------------------------------------------------- #
#  Hover tooltip
# --------------------------------------------------------------------------- #

class Tooltip:
    """A lightweight hover tooltip for any Tk/CTk widget."""

    def __init__(self, widget, text: str, wraplength: int = 320, delay: int = 450) -> None:
        self.widget = widget
        self.text = text
        self.wraplength = wraplength
        self.delay = delay
        self._tip: tk.Toplevel | None = None
        self._after: str | None = None
        # CustomTkinter widgets are composites; binding the outer frame alone
        # never sees the pointer, so every child is bound as well.
        self._bind(widget)

    def _bind(self, widget) -> None:
        # Some CustomTkinter widgets refuse .bind() outright; their children
        # still accept it, which is all the tooltip needs.
        try:
            widget.bind("<Enter>", self._schedule, add="+")
            widget.bind("<Leave>", self._hide, add="+")
            widget.bind("<ButtonPress>", self._hide, add="+")
        except Exception:
            pass
        for child in widget.winfo_children():
            self._bind(child)

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self) -> None:
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _show(self) -> None:
        if self._tip is not None or not self.text:
            return
        x = self.widget.winfo_rootx() + 18
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        self._tip.configure(bg=COL_BORDER)
        tk.Label(
            self._tip, text=self.text, justify="left", bg=COL_PANEL_2, fg=COL_TEXT,
            wraplength=self.wraplength, font=("Segoe UI", 9), padx=12, pady=9, bd=0,
        ).pack(padx=1, pady=1)

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


# --------------------------------------------------------------------------- #
#  Busy indicator
# --------------------------------------------------------------------------- #

class Spinner(ctk.CTkFrame):
    """A rotating arc plus a caption, shown while the preview is rebuilt.

    Tk cannot composite, so the badge brings its own solid background and
    floats in a corner of the stage rather than dimming the image behind it.
    The arc is turned by ``after`` on the UI thread, so it also doubles as a
    liveness check: if it ever stops mid-run, something is blocking the event
    loop again.
    """

    DIAMETER = 12        # unscaled px of the arc itself
    THICKNESS = 2
    STEP_MS = 40         # ~25 frames a second is plenty for a spinner
    STEP_DEG = -24       # negative: clockwise, like every other spinner

    def __init__(self, master, text: str = "Updating preview…") -> None:
        super().__init__(master, fg_color=COL_PANEL_2, corner_radius=16,
                         border_width=1, border_color=COL_BORDER)
        size = scaled_px(master, self.DIAMETER)
        width = scaled_px(master, self.THICKNESS)
        self.canvas = tk.Canvas(
            self, width=size, height=size, bg=COL_PANEL_2,
            highlightthickness=0, bd=0,
        )
        self.canvas.pack(side="left", padx=(10, 7), pady=7)
        inset = width / 2
        box = (inset, inset, size - inset, size - inset)
        self.canvas.create_oval(*box, outline=COL_BORDER, width=width)
        self._arc = self.canvas.create_arc(
            *box, start=90, extent=105, style="arc", outline=COL_ACCENT,
            width=width,
        )
        self.label = ctk.CTkLabel(self, text=text, text_color=COL_TEXT_DIM,
                                  font=ctk.CTkFont(size=12))
        self.label.pack(side="left", padx=(0, 12))

        self._angle = 90
        self._tick_after: str | None = None

    def start(self, text: str | None = None) -> None:
        """Show the badge and turn the arc. Calling it again only re-labels."""
        if text is not None:
            self.label.configure(text=text)
        self.place(relx=1.0, rely=0.0, anchor="ne", x=-14, y=14)
        self.lift()
        if self._tick_after is None:
            self._tick()

    def stop(self) -> None:
        if self._tick_after is not None:
            try:
                self.after_cancel(self._tick_after)
            except Exception:
                pass
            self._tick_after = None
        self.place_forget()

    def _tick(self) -> None:
        self._angle = (self._angle + self.STEP_DEG) % 360
        self.canvas.itemconfigure(self._arc, start=self._angle)
        self._tick_after = self.after(self.STEP_MS, self._tick)


class SavedPie(tk.Canvas):
    """Pie showing what share of the file the optimization removed.

    The wedge starts at twelve o'clock and grows clockwise, so it reads the
    way a progress dial does. It carries no text of its own — it sits next to
    the percentage and only makes that number graspable at a glance.
    """

    SIZE = 34            # unscaled; scaled_px matches it to the rest

    def __init__(self, master, size: int = SIZE) -> None:
        size = scaled_px(master, size)
        super().__init__(master, width=size, height=size, bg=COL_PANEL,
                         highlightthickness=0, bd=0)
        pad = 1
        box = (pad, pad, size - pad, size - pad)
        self._track = self.create_oval(*box, fill=COL_PANEL_2, outline=COL_BORDER)
        self._wedge = self.create_arc(*box, start=90, extent=0, fill=COL_GREEN,
                                      outline="", state="hidden")

    def set_percent(self, percent: float | None) -> None:
        """``percent`` is what was saved; negative means the file grew."""
        if percent is None:
            self.itemconfigure(self._wedge, state="hidden")
            return
        # A file that grew has no meaningful "share removed", so the wedge then
        # shows how much was *added*, in red. Either way it is capped at a full
        # circle — Tk draws nothing at all for an extent of exactly 360.
        share = min(abs(percent) / 100.0, 1.0)
        self.itemconfigure(
            self._wedge,
            state="normal",
            extent=-359.99 if share >= 0.9999 else -360.0 * share,
            fill=COL_GREEN if percent > 0 else COL_ERROR,
        )


# --------------------------------------------------------------------------- #
#  Persistent configuration
# --------------------------------------------------------------------------- #

class Config:
    DEFAULTS = {
        "use_source_dir": True,
        "output_dir": str(Path.home() / "Downloads"),
        "prefix": "min_",
        "suffix": "",
        "overwrite": False,
        "compare_gzip": True,
        "bg_mode": "checker",
        "collapsed_groups": [],
        "last_open_dir": "",
        #: Where the window was when it was last closed, so it comes back on
        #: the same monitor and at the same size.
        "window_geometry": "",
        "window_maximized": False,
        "svgo_settings": engine.default_settings(),
    }

    def __init__(self) -> None:
        self.data = json.loads(json.dumps(self.DEFAULTS))
        self.load()

    def load(self) -> None:
        path = CONFIG_PATH
        if not path.exists() and LEGACY_CONFIG_PATH.exists():
            path = LEGACY_CONFIG_PATH  # carried over from the SVGO-UI days
        try:
            if path.exists():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                self.data.update({k: loaded[k] for k in loaded if k in self.DEFAULTS})
        except Exception:
            pass  # fall back to defaults on any corruption

    def save(self) -> None:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except Exception as exc:  # pragma: no cover
            messagebox.showerror(APP_NAME, f"Could not save settings:\n{exc}")

    def __getitem__(self, key: str):
        return self.data.get(key, self.DEFAULTS.get(key))

    def __setitem__(self, key: str, value) -> None:
        self.data[key] = value


# --------------------------------------------------------------------------- #
#  One loaded document
# --------------------------------------------------------------------------- #

class SvgDoc:
    """An opened SVG plus whatever has been computed from it so far."""

    def __init__(self, path: Path, data: bytes) -> None:
        self.path = path
        self.original: bytes = data
        self.optimized: bytes | None = None
        self.error: str | None = None
        self.original_pil: Image.Image | None = None
        self.optimized_pil: Image.Image | None = None
        #: settings snapshot the current `optimized` was produced with
        self.stamp: str | None = None
        #: settings snapshot the preview bitmaps were rendered for. A failed
        #: render still sets this, so it is never retried in a loop.
        self.render_stamp: str | None = None

    @property
    def name(self) -> str:
        return self.path.name

    def invalidate(self) -> None:
        self.optimized = None
        self.optimized_pil = None
        self.error = None
        self.stamp = None
        self.render_stamp = None

    def drop_bitmaps(self) -> None:
        """Release the preview bitmaps; they are re-rendered when needed."""
        self.original_pil = None
        self.optimized_pil = None
        self.render_stamp = None


# --------------------------------------------------------------------------- #
#  Preview worker — the whole concurrency story
# --------------------------------------------------------------------------- #

class PreviewWorker:
    """Runs optimize-and-render jobs outside the UI process, one at a time.

    The rules are short enough to keep in your head:

    * **One job at a time.** Every job gets a sequence number; only the newest
      one is ever delivered.
    * **A new job cancels the running one.** Cancelling means killing the child
      process — SVGO and the rasterizer are not interruptible, and waiting for
      a result nobody wants any more is exactly the lag we are getting rid of.
      A fresh child is started for the new job (~a third of a second, paid
      while the spinner turns).
    * **Nothing blocks the UI thread.** Results are picked up by a short
      ``after`` poll, so the window keeps painting, scrolling and reacting
      while a job runs.

    If child processes turn out to be unavailable — some frozen builds, locked
    down machines — the worker quietly falls back to a thread. A thread cannot
    be killed, so a cancelled job then runs to completion in the background and
    its result is dropped on arrival; the window stays usable either way.
    """

    POLL_MS = 40

    def __init__(self, widget: tk.Misc, on_result, on_degraded=None) -> None:
        self._widget = widget            # any widget will do: we need `after`
        self._on_result = on_result
        self._on_degraded = on_degraded
        self._ctx = mp.get_context("spawn")
        self._proc: "mp.process.BaseProcess | None" = None
        self._jobs = None
        self._results = None
        self._seq = 0
        self._pending: tuple | None = None   # the job in flight, for a retry
        self._threaded = False
        self._closed = False
        self._widget.after(self.POLL_MS, self._poll)

    # ---- public API ------------------------------------------------------ #
    def submit(self, svg: bytes, settings: dict, path: str,
               render_original: bool) -> int:
        """Queue a job, cancelling whatever was running. Returns its sequence."""
        self._seq += 1
        if self._pending is not None:
            self._kill()                 # cancel: the old run is thrown away
        job = (self._seq, svg, settings, path, render_original)
        self._pending = job
        self._dispatch(job)
        return self._seq

    def prewarm(self) -> None:
        """Start the child now so the first job does not wait for it."""
        if not self._threaded:
            self._ensure_child()

    def cancel(self) -> None:
        """Drop the running job without starting a new one."""
        self._seq += 1
        if self._pending is not None:
            self._kill()

    def shutdown(self) -> None:
        self._closed = True
        self._kill()

    # ---- plumbing -------------------------------------------------------- #
    def _dispatch(self, job: tuple) -> None:
        if not self._threaded:
            self._ensure_child()
        if self._jobs is not None:
            try:
                self._jobs.put(job)
                return
            except Exception:
                self._degrade()          # the pipe is gone; stop trying
                self._kill(keep_pending=True)
        threading.Thread(target=self._run_in_thread, args=(job,),
                         daemon=True).start()

    def _ensure_child(self) -> None:
        if self._proc is not None and self._proc.is_alive():
            return
        try:
            self._jobs = self._ctx.Queue()
            self._results = self._ctx.Queue()
            self._proc = self._ctx.Process(
                target=svgo_worker.worker_loop, args=(self._jobs, self._results),
                name="svgsnake-preview", daemon=True,
            )
            self._proc.start()
        except Exception:
            self._degrade()
            self._proc = self._jobs = self._results = None

    def _degrade(self) -> None:
        """Give up on child processes for good, and say so once.

        Worth reporting rather than hiding: without a child process the
        rasterizer blocks the event loop again, which is exactly the symptom
        this class exists to remove.
        """
        if self._threaded:
            return
        self._threaded = True
        if self._on_degraded is not None:
            self._widget.after(0, self._on_degraded)

    def _run_in_thread(self, job: tuple) -> None:
        result = svgo_worker.run_job(*job)
        self._widget.after(0, lambda: self._deliver(result))

    def _kill(self, keep_pending: bool = False) -> None:
        """End the current run. The queues go with the process on purpose:
        a child killed mid-``put`` can leave them in an unusable state."""
        proc, self._proc = self._proc, None
        queues, self._jobs, self._results = (self._jobs, self._results), None, None
        if not keep_pending:
            self._pending = None
        if proc is not None:
            try:
                if proc.is_alive():
                    proc.terminate()
            except Exception:
                pass
        for q in queues:
            if q is None:
                continue
            try:
                q.cancel_join_thread()  # do not wait on a pipe nobody reads
                q.close()
            except Exception:
                pass

    def _poll(self) -> None:
        results = self._results
        if results is not None:
            while True:
                try:
                    result = results.get_nowait()
                except queue.Empty:
                    break
                except (OSError, ValueError, EOFError):
                    break
                self._deliver(result)
        # A child that died without answering (a crash, or a build without
        # working process support) must not leave the spinner turning forever.
        if (self._pending is not None and self._proc is not None
                and not self._proc.is_alive()):
            job = self._pending
            self._kill(keep_pending=True)
            self._degrade()
            threading.Thread(target=self._run_in_thread, args=(job,),
                             daemon=True).start()
        if not self._closed:
            self._widget.after(self.POLL_MS, self._poll)

    def _deliver(self, result: tuple) -> None:
        if result[0] != self._seq:
            return                       # superseded: this answer is stale
        self._pending = None
        self._on_result(*result)


# --------------------------------------------------------------------------- #
#  Image viewer (zoom / pan / background)
# --------------------------------------------------------------------------- #

BG_MODES = ["checker", "white", "black", "dark"]
BG_LABELS = {"checker": "Checkerboard", "white": "White", "black": "Black", "dark": "Dark"}


class ImageViewer(ctk.CTkFrame):
    """Canvas that shows a PIL image with wheel-zoom, drag-pan and a backdrop.

    Zoom and pan are *not* reset when the image is swapped, so toggling between
    original and optimized keeps both views perfectly aligned.
    """

    MIN_ZOOM = 0.1
    MAX_ZOOM = 24.0

    def __init__(self, master, bg_mode: str = "checker", **kwargs) -> None:
        super().__init__(master, **kwargs)
        self.image: Image.Image | None = None
        self.placeholder: str = ""
        self.bg_mode = bg_mode
        self.zoom = 1.0          # multiplier on top of "fit to window"
        self.offset = [0.0, 0.0]  # pan, in canvas pixels, from the fitted position

        self._photo: ImageTk.PhotoImage | None = None
        self._checker_cache: tuple[int, int, Image.Image] | None = None
        self._drag_origin: tuple[int, int] | None = None
        self._redraw_after: str | None = None

        self.canvas = tk.Canvas(
            self, bg=COL_PANEL, highlightthickness=0, bd=0, cursor="fleur",
        )
        self.canvas.pack(fill="both", expand=True, padx=1, pady=1)

        self.canvas.bind("<Configure>", lambda _e: self._schedule_redraw())
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", lambda _e: setattr(self, "_drag_origin", None))
        self.canvas.bind("<Double-Button-1>", lambda _e: self.reset_view())
        self.canvas.bind("<MouseWheel>", self._on_wheel)          # Windows / macOS
        self.canvas.bind("<Button-4>", lambda e: self._on_wheel(e, 120))   # X11
        self.canvas.bind("<Button-5>", lambda e: self._on_wheel(e, -120))  # X11

    # ---- public API ----------------------------------------------------- #
    def set_image(self, image: Image.Image | None, placeholder: str = "") -> None:
        self.image = image
        self.placeholder = placeholder
        self._redraw()

    def set_bg_mode(self, mode: str) -> None:
        self.bg_mode = mode if mode in BG_MODES else "checker"
        self._redraw()

    def reset_view(self) -> None:
        self.zoom = 1.0
        self.offset = [0.0, 0.0]
        self._redraw()

    def zoom_by(self, factor: float, anchor: tuple[int, int] | None = None) -> None:
        new_zoom = max(self.MIN_ZOOM, min(self.MAX_ZOOM, self.zoom * factor))
        if new_zoom == self.zoom:
            return
        cw, ch = max(self.canvas.winfo_width(), 1), max(self.canvas.winfo_height(), 1)
        ax, ay = anchor if anchor else (cw / 2, ch / 2)
        # Keep the point under the cursor fixed while zooming.
        ratio = new_zoom / self.zoom
        cx, cy = cw / 2 + self.offset[0], ch / 2 + self.offset[1]
        self.offset[0] += (cx - ax) * (ratio - 1)
        self.offset[1] += (cy - ay) * (ratio - 1)
        self.zoom = new_zoom
        self._redraw()

    @property
    def zoom_percent(self) -> int:
        return int(round(self._display_scale() * 100))

    # ---- events --------------------------------------------------------- #
    def _on_press(self, event) -> None:
        self._drag_origin = (event.x, event.y)

    def _on_drag(self, event) -> None:
        if self._drag_origin is None:
            return
        dx = event.x - self._drag_origin[0]
        dy = event.y - self._drag_origin[1]
        self._drag_origin = (event.x, event.y)
        self.offset[0] += dx
        self.offset[1] += dy
        self._redraw()

    def _on_wheel(self, event, delta: int | None = None) -> None:
        d = delta if delta is not None else event.delta
        self.zoom_by(1.15 if d > 0 else 1 / 1.15, (event.x, event.y))

    # ---- drawing -------------------------------------------------------- #
    def _schedule_redraw(self) -> None:
        if self._redraw_after is not None:
            try:
                self.after_cancel(self._redraw_after)
            except Exception:
                pass
        self._redraw_after = self.after(40, self._redraw)

    def _fit_scale(self) -> float:
        if self.image is None:
            return 1.0
        cw, ch = max(self.canvas.winfo_width(), 1), max(self.canvas.winfo_height(), 1)
        iw, ih = self.image.size
        return min((cw - 24) / iw, (ch - 24) / ih, 1.0)

    def _display_scale(self) -> float:
        return self._fit_scale() * self.zoom

    def _background(self, cw: int, ch: int) -> Image.Image:
        if self.bg_mode == "white":
            return Image.new("RGB", (cw, ch), "#ffffff")
        if self.bg_mode == "black":
            return Image.new("RGB", (cw, ch), "#000000")
        if self.bg_mode == "dark":
            return Image.new("RGB", (cw, ch), COL_PANEL)

        cached = self._checker_cache
        if cached and cached[0] == cw and cached[1] == ch:
            return cached[2].copy()
        tile = 14
        img = Image.new("RGB", (cw, ch), "#3a3a3a")
        draw = ImageDraw.Draw(img)
        for y in range(0, ch, tile):
            for x in range((y // tile) % 2 * tile, cw, tile * 2):
                draw.rectangle([x, y, x + tile - 1, y + tile - 1], fill="#484848")
        self._checker_cache = (cw, ch, img)
        return img.copy()

    def _redraw(self) -> None:
        self._redraw_after = None
        self.canvas.delete("all")
        cw, ch = max(self.canvas.winfo_width(), 1), max(self.canvas.winfo_height(), 1)
        if cw < 10 or ch < 10:
            return

        if self.image is None:
            self.canvas.configure(bg=COL_PANEL)
            for i, line in enumerate(self.placeholder.split("\n")):
                self.canvas.create_text(
                    cw / 2, ch / 2 + (i - 0.5) * 26, text=line,
                    fill=COL_TEXT_DIM, font=("Segoe UI", 14),
                )
            return

        scale = self._display_scale()
        iw, ih = self.image.size
        dw, dh = max(1, int(iw * scale)), max(1, int(ih * scale))
        ox = (cw - dw) / 2 + self.offset[0]
        oy = (ch - dh) / 2 + self.offset[1]

        canvas_img = self._background(cw, ch)

        # Only scale the part of the image that is actually visible, so deep
        # zoom levels stay cheap.
        vx0, vy0 = max(0, int(ox)), max(0, int(oy))
        vx1, vy1 = min(cw, int(ox + dw)), min(ch, int(oy + dh))
        if vx1 > vx0 and vy1 > vy0:
            sx0 = max(0.0, (vx0 - ox) / scale)
            sy0 = max(0.0, (vy0 - oy) / scale)
            sx1 = min(float(iw), (vx1 - ox) / scale)
            sy1 = min(float(ih), (vy1 - oy) / scale)
            if sx1 > sx0 and sy1 > sy0:
                crop = self.image.resize(
                    (vx1 - vx0, vy1 - vy0),
                    Image.LANCZOS if scale < 1 else Image.NEAREST,
                    box=(sx0, sy0, sx1, sy1),
                )
                canvas_img.paste(crop, (vx0, vy0), crop)

        self._photo = ImageTk.PhotoImage(canvas_img)
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)


# --------------------------------------------------------------------------- #
#  Markup view
# --------------------------------------------------------------------------- #

class CodeView(ctk.CTkFrame):
    """Read-only, lightly syntax-highlighted view of the SVG source."""

    def __init__(self, master, **kwargs) -> None:
        super().__init__(master, **kwargs)
        # Minified SVG is one very long line, so it has to wrap to be readable.
        self.text = tk.Text(
            self, wrap="word", bg=COL_PANEL, fg="#d6d6d6", insertbackground=COL_TEXT,
            relief="flat", bd=0, padx=14, pady=12, font=("Consolas", 10),
            selectbackground=COL_ACCENT, selectforeground=COL_ACCENT_TEXT,
        )
        yscroll = ctk.CTkScrollbar(self, command=self.text.yview,
                                   button_color=COL_BORDER, button_hover_color=COL_ACCENT)
        self.text.configure(yscrollcommand=yscroll.set)

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.text.grid(row=0, column=0, sticky="nsew", padx=(1, 0), pady=1)
        yscroll.grid(row=0, column=1, sticky="ns", pady=1)

        self.text.tag_configure("tag", foreground="#e06c75")
        self.text.tag_configure("attr", foreground="#d19a66")
        self.text.tag_configure("value", foreground="#98c379")
        self.text.tag_configure("comment", foreground="#6b7280")
        self.text.configure(state="disabled")
        self._pending: str | None = None

    def set_content(self, content: str) -> None:
        """Remember ``content``; it is only rendered while the tab is visible."""
        self._pending = content
        if self.winfo_ismapped():
            self.flush()

    def flush(self) -> None:
        """Push the pending markup into the widget, if it is not there yet."""
        if self._pending is None:
            return
        content, self._pending = self._pending, None
        self._render(content)

    def _render(self, content: str) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        truncated = len(content) > MARKUP_LIMIT
        shown = content[:MARKUP_LIMIT] if truncated else content
        self.text.insert("1.0", shown)
        if truncated:
            self.text.insert("end", "\n\n… output truncated for display …")
        if len(shown) <= HIGHLIGHT_LIMIT:
            self._highlight(shown)
        self.text.configure(state="disabled")
        self.text.yview_moveto(0)
        self.text.xview_moveto(0)

    # ---- highlighting --------------------------------------------------- #
    def _highlight(self, content: str) -> None:
        import re

        # Offset -> "line.col" without asking Tk for every index.
        starts = [0]
        for i, ch in enumerate(content):
            if ch == "\n":
                starts.append(i + 1)

        def index(pos: int) -> str:
            lo, hi = 0, len(starts) - 1
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if starts[mid] <= pos:
                    lo = mid
                else:
                    hi = mid - 1
            return f"{lo + 1}.{pos - starts[lo]}"

        spans: dict[str, list[str]] = {"tag": [], "attr": [], "value": [], "comment": []}
        pattern = re.compile(
            r"(?P<comment><!--.*?-->)"
            r"|(?P<tag></?[A-Za-z_][\w:.-]*)"
            r"|(?P<attr>[A-Za-z_][\w:.-]*)\s*=\s*(?P<value>\"[^\"]*\"|'[^']*')",
            re.DOTALL,
        )
        for m in pattern.finditer(content):
            for key in ("comment", "tag", "attr", "value"):
                if m.group(key) is not None:
                    spans[key] += [index(m.start(key)), index(m.end(key))]

        for name, pairs in spans.items():
            if pairs:
                self.text.tag_add(name, *pairs)


# --------------------------------------------------------------------------- #
#  One collapsible category in the features list
# --------------------------------------------------------------------------- #

class PluginGroup:
    """A named, collapsible block of plugin switches.

    Each group owns its own container, so collapsing or filtering only ever
    touches widgets inside it — the order of the groups themselves is fixed by
    the order they were packed in.
    """

    def __init__(self, parent, key: str, label: str, blurb: str, app: "App") -> None:
        self.key = key
        self.label = label
        self.app = app
        self.collapsed = False
        #: ``(plugin id, searchable text, row frame)`` in execution order
        self.rows: list[tuple[str, str, ctk.CTkFrame]] = []

        self.container = ctk.CTkFrame(parent, fg_color="transparent")
        self.container.pack(fill="x", padx=8, pady=(6, 0))

        self.header = ctk.CTkFrame(self.container, fg_color=COL_PANEL_2, corner_radius=6)
        self.header.pack(fill="x")
        self.chevron = ctk.CTkLabel(
            self.header, text="▾", text_color=COL_ACCENT, width=16,
            font=ctk.CTkFont(size=12, weight="bold"), cursor="hand2",
        )
        self.chevron.pack(side="left", padx=(8, 2), pady=5)
        self.title = ctk.CTkLabel(
            self.header, text=label, text_color=COL_TEXT, anchor="w",
            font=ctk.CTkFont(size=12, weight="bold"), cursor="hand2",
        )
        self.title.pack(side="left", fill="x", expand=True)
        self.count = ctk.CTkLabel(
            self.header, text="", text_color=COL_TEXT_DIM,
            font=ctk.CTkFont(size=11), cursor="hand2",
        )
        self.count.pack(side="right", padx=(4, 10))

        for widget in (self.header, self.chevron, self.title, self.count):
            widget.bind("<Button-1>", lambda _e: self.toggle())
        Tooltip(self.header, f"{blurb}\n\nClick to collapse or expand.")

        self.body = ctk.CTkFrame(self.container, fg_color="transparent")
        self.body.pack(fill="x")

    # ---- contents ------------------------------------------------------- #
    def add_plugin(self, pid: str, label: str, var, help_text: str) -> None:
        row = self.app._switch(
            self.body, label, var,
            lambda p=pid, v=var: self.app._set_plugin(p, v.get()),
            f"{pid}\n\n{help_text}", small=True, padx=8,
        )
        self.rows.append((pid, f"{label} {pid} {self.label}".lower(), row))

    # ---- collapsing ----------------------------------------------------- #
    def toggle(self) -> None:
        self.set_collapsed(not self.collapsed)
        self.app.persist_group_state()

    def set_collapsed(self, collapsed: bool) -> None:
        self.collapsed = collapsed
        self.chevron.configure(text="▸" if collapsed else "▾")
        if collapsed:
            self.body.pack_forget()
        elif not self.body.winfo_ismapped():
            self.body.pack(fill="x")

    # ---- filtering ------------------------------------------------------ #
    def apply_filter(self, needle: str) -> int:
        """Show only rows matching ``needle``; return how many matched."""
        for _pid, _haystack, row in self.rows:
            row.pack_forget()
        matches = [r for r in self.rows if not needle or needle in r[1]]
        for _pid, _haystack, row in matches:
            row.pack(fill="x", padx=8, pady=1)

        # Unpack unconditionally before re-packing: pack() appends to the end,
        # so a group that kept its slot while the others were hidden would jump
        # ahead of them the moment they came back. The caller walks the groups
        # in catalogue order, which is what restores it.
        self.container.pack_forget()
        if not matches:
            return 0
        self.container.pack(fill="x", padx=8, pady=(6, 0))
        # A search is useless if the hits stay folded away.
        self.set_collapsed(False if needle else self.collapsed)
        return len(matches)

    def update_count(self, settings_plugins: dict) -> tuple[int, int]:
        enabled = sum(1 for pid, _h, _r in self.rows if settings_plugins.get(pid))
        total = len(self.rows)
        self.count.configure(
            text=f"{enabled}/{total}",
            text_color=COL_TEXT_DIM if enabled else COL_BORDER,
        )
        return enabled, total


# --------------------------------------------------------------------------- #
#  Settings dialog (output / file handling)
# --------------------------------------------------------------------------- #

class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, master: "App", config: Config) -> None:
        super().__init__(master)
        self.app = master
        self.config_ref = config

        self.title("Settings")
        apply_window_icon(self)
        self.geometry("580x470")
        self.minsize(520, 430)
        self.configure(fg_color=COL_BG)
        self.transient(master)
        self.after(120, self.grab_set)

        ctk.CTkLabel(
            self, text="Settings", font=ctk.CTkFont(size=20, weight="bold"),
            text_color=COL_TEXT,
        ).pack(anchor="w", padx=22, pady=(18, 2))
        ctk.CTkLabel(
            self, text="Where optimized files go and how they are named.",
            text_color=COL_TEXT_DIM,
        ).pack(anchor="w", padx=22, pady=(0, 8))

        body = ctk.CTkFrame(self, fg_color=COL_PANEL, corner_radius=10)
        body.pack(fill="both", expand=True, padx=18, pady=(0, 8))

        # Output folder ------------------------------------------------------
        self._sub(body, "Output folder")
        self.source_dir_var = ctk.BooleanVar(value=bool(config["use_source_dir"]))
        ctk.CTkCheckBox(
            body, text="Save next to the original file", variable=self.source_dir_var,
            command=self._sync_dir_state, fg_color=COL_ACCENT, hover_color=COL_ACCENT_HOVER,
            text_color=COL_TEXT,
        ).pack(anchor="w", padx=16, pady=(0, 6))

        dir_row = ctk.CTkFrame(body, fg_color="transparent")
        dir_row.pack(fill="x", padx=16)
        self.dir_entry = ctk.CTkEntry(dir_row, fg_color=COL_PANEL_2, border_color=COL_BORDER)
        self.dir_entry.insert(0, config["output_dir"])
        self.dir_entry.pack(side="left", fill="x", expand=True)
        self.dir_btn = ctk.CTkButton(
            dir_row, text="Browse", width=80, fg_color=COL_PANEL_2,
            hover_color=COL_BORDER, command=self._browse_dir,
        )
        self.dir_btn.pack(side="left", padx=(8, 0))

        # File name ----------------------------------------------------------
        self._sub(body, "File name")
        name_row = ctk.CTkFrame(body, fg_color="transparent")
        name_row.pack(fill="x", padx=16)
        name_row.grid_columnconfigure((1, 3), weight=1)

        ctk.CTkLabel(name_row, text="Prefix", text_color=COL_TEXT_DIM).grid(
            row=0, column=0, sticky="w", padx=(0, 8))
        self.prefix_entry = ctk.CTkEntry(name_row, fg_color=COL_PANEL_2, border_color=COL_BORDER)
        self.prefix_entry.insert(0, config["prefix"])
        self.prefix_entry.grid(row=0, column=1, sticky="ew")

        ctk.CTkLabel(name_row, text="Suffix", text_color=COL_TEXT_DIM).grid(
            row=0, column=2, sticky="w", padx=(16, 8))
        self.suffix_entry = ctk.CTkEntry(name_row, fg_color=COL_PANEL_2, border_color=COL_BORDER)
        self.suffix_entry.insert(0, config["suffix"])
        self.suffix_entry.grid(row=0, column=3, sticky="ew")

        self.preview_label = ctk.CTkLabel(
            body, text="", text_color=COL_TEXT_DIM, anchor="w",
            font=ctk.CTkFont(size=11),
        )
        self.preview_label.pack(fill="x", padx=16, pady=(8, 0))
        for entry in (self.prefix_entry, self.suffix_entry):
            entry.bind("<KeyRelease>", lambda _e: self._update_name_preview())

        # Overwriting ---------------------------------------------------------
        self._sub(body, "Overwriting")
        self.overwrite_var = ctk.BooleanVar(value=bool(config["overwrite"]))
        ctk.CTkCheckBox(
            body, text="Overwrite existing files without asking",
            variable=self.overwrite_var, command=self._update_name_preview,
            fg_color=COL_ACCENT, hover_color=COL_ACCENT_HOVER, text_color=COL_TEXT,
        ).pack(anchor="w", padx=16, pady=(0, 4))
        ctk.CTkLabel(
            body,
            text=f"When this is off, {APP_NAME} asks before replacing a file that already exists.",
            text_color=COL_TEXT_DIM, anchor="w", justify="left", wraplength=470,
            font=ctk.CTkFont(size=11),
        ).pack(fill="x", padx=16, pady=(0, 12))

        # Buttons -------------------------------------------------------------
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(fill="x", padx=20, pady=(0, 16))
        ctk.CTkButton(
            btn_row, text="Cancel", fg_color=COL_PANEL_2, hover_color=COL_BORDER,
            command=self.destroy,
        ).pack(side="right")
        ctk.CTkButton(
            btn_row, text="Save", fg_color=COL_ACCENT, hover_color=COL_ACCENT_HOVER,
            text_color=COL_ACCENT_TEXT, command=self._save,
        ).pack(side="right", padx=(0, 10))

        self._sync_dir_state()
        self._update_name_preview()

    def _sub(self, parent, text: str) -> None:
        ctk.CTkLabel(
            parent, text=text.upper(), text_color=COL_ACCENT,
            font=ctk.CTkFont(size=12, weight="bold"), anchor="w",
        ).pack(fill="x", padx=16, pady=(14, 6))

    def _sync_dir_state(self) -> None:
        state = "disabled" if self.source_dir_var.get() else "normal"
        self.dir_entry.configure(state=state)
        self.dir_btn.configure(state=state)
        self._update_name_preview()

    def _browse_dir(self) -> None:
        d = filedialog.askdirectory(
            parent=self, initialdir=self.dir_entry.get() or str(Path.home()))
        if d:
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, d)
        self._update_name_preview()

    def _update_name_preview(self) -> None:
        prefix = self.prefix_entry.get()
        suffix = self.suffix_entry.get()
        name = f"{prefix}drawing{suffix}.svg"
        where = "next to the original" if self.source_dir_var.get() else (
            _short_path(self.dir_entry.get().strip(), 3) or "(no folder selected)")
        text = f"drawing.svg  →  {name}\nSaved in: {where}"
        colour = COL_TEXT_DIM
        if not prefix and not suffix:
            text += "\n⚠ No prefix or suffix — this replaces the original file."
            colour = COL_ERROR
        self.preview_label.configure(text=text, text_color=colour)

    def _save(self) -> None:
        self.config_ref["use_source_dir"] = bool(self.source_dir_var.get())
        self.config_ref["output_dir"] = self.dir_entry.get().strip()
        self.config_ref["prefix"] = self.prefix_entry.get().strip()
        self.config_ref["suffix"] = self.suffix_entry.get().strip()
        self.config_ref["overwrite"] = bool(self.overwrite_var.get())
        self.config_ref.save()
        self.app.refresh_target_hint()
        self.destroy()


# --------------------------------------------------------------------------- #
#  Main application
# --------------------------------------------------------------------------- #

_AppBase = (ctk.CTk, TkinterDnD.DnDWrapper) if _DND_AVAILABLE else (ctk.CTk,)


class App(*_AppBase):  # type: ignore[misc]

    def __init__(self) -> None:
        super().__init__()
        self.config_ref = Config()

        self.title(f"{APP_NAME} — {APP_SLOGAN}")
        self.geometry("1480x900")
        self.minsize(1180, 700)
        #: Size and position to save on exit. A maximized window must not
        #: overwrite it, or there would be nothing to restore down to.
        self._normal_geometry = self.geometry()
        self._restore_geometry()
        self.configure(fg_color=COL_BG)
        apply_window_icon(self)

        self._dnd_ready = False
        if _DND_AVAILABLE:
            try:
                self.TkdndVersion = TkinterDnD._require(self)
                self._dnd_ready = True
            except Exception:
                self._dnd_ready = False

        # State
        self.docs: list[SvgDoc] = []
        self.current: int = -1
        self.settings: dict = engine.normalize_settings(self.config_ref["svgo_settings"])
        self.showing_optimized = True
        #: document + settings snapshot the job in flight belongs to
        self._job: tuple[SvgDoc, str] | None = None
        self._optimize_after: str | None = None
        self._row_widgets: list[tuple[ctk.CTkFrame, ctk.CTkLabel, ctk.CTkLabel]] = []
        self._plugin_vars: dict[str, ctk.BooleanVar] = {}
        self._groups: list[PluginGroup] = []
        self._busy = False

        self._build_layout()
        self.worker = PreviewWorker(self, self._on_optimized,
                                    self._on_worker_degraded)
        self.worker.prewarm()
        self._update_file_list()
        self._refresh_all()
        self._setup_dnd()
        self._bind_keys()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- window plumbing ------------------------------------------------ #
    def _restore_geometry(self) -> None:
        """Put the window back where it was when it was last closed."""
        saved = str(self.config_ref["window_geometry"] or "")
        if not saved or not fits_on_screen(self, saved):
            return
        try:
            self.geometry(saved)
        except Exception:  # pragma: no cover - a corrupt config must not block
            return
        self._normal_geometry = saved
        if self.config_ref["window_maximized"]:
            # Only once the window is on screen: maximizing an unmapped window
            # makes Tk drop the position we just asked for.
            self.after(0, lambda: self.state("zoomed"))

    def _remember_geometry(self, event=None) -> None:
        # Descendants deliver their own <Configure> here through the bindtags,
        # so the window's own events have to be picked out.
        if event is not None and event.widget is not self:
            return
        if self.state() == "normal":
            self._normal_geometry = self.geometry()

    def _on_close(self) -> None:
        self._remember_geometry()
        self.config_ref["window_geometry"] = self._normal_geometry
        self.config_ref["window_maximized"] = self.state() == "zoomed"
        self.config_ref["svgo_settings"] = self.settings
        self.config_ref.save()
        self.worker.shutdown()
        self.destroy()

    def _bind_keys(self) -> None:
        self.bind("<Configure>", self._remember_geometry, add="+")
        self.bind("<Control-o>", lambda _e: self._choose_files())
        self.bind("<Control-s>", lambda _e: self._save_current())
        self.bind("<Control-Shift-S>", lambda _e: self._save_all())
        self.bind("<space>", self._on_space)

    def _on_space(self, event):
        # Don't steal the space bar from text entry widgets.
        if isinstance(event.widget, (tk.Entry, tk.Text)):
            return None
        value = "Original" if self.showing_optimized else "Optimized"
        self.view_toggle.set(value)
        self._on_view_toggle(value)
        return "break"

    def _setup_dnd(self) -> None:
        if not self._dnd_ready:
            return
        try:
            for target in (self, self.viewer, self.viewer.canvas):
                target.drop_target_register(DND_FILES)
                target.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            pass

    def _on_drop(self, event) -> None:
        try:
            paths = self.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        self._open_paths([str(p).strip().strip("{}") for p in paths])

    # ---- layout --------------------------------------------------------- #
    def _build_layout(self) -> None:
        # Three columns: files + global options | preview | the plugin list.
        # Splitting the plugin list off is what keeps either sidebar short
        # enough that the preview never has to compete with it for height.
        self.grid_columnconfigure(0, weight=0, minsize=360)
        self.grid_columnconfigure(1, weight=1)
        self.grid_columnconfigure(2, weight=0, minsize=400)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar()
        self._build_main()
        self._build_features()

    # ---- sidebar -------------------------------------------------------- #
    def _build_sidebar(self) -> None:
        side = ctk.CTkFrame(self, fg_color=COL_PANEL, corner_radius=0)
        side.grid(row=0, column=0, sticky="nsew")
        side.grid_columnconfigure(0, weight=1)
        side.grid_rowconfigure(4, weight=1)  # trailing spacer

        # Header -------------------------------------------------------------
        header = ctk.CTkFrame(side, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 2))
        logo = _load_logo_image(30)
        if logo is not None:
            ctk.CTkLabel(header, image=logo, text="").pack(side="left")
            self._logo_image = logo  # keep a reference alive
        else:
            ctk.CTkLabel(
                header, text="●", text_color=COL_ACCENT,
                font=ctk.CTkFont(size=22, weight="bold"),
            ).pack(side="left")
        ctk.CTkLabel(
            header, text="  SVG", font=ctk.CTkFont(size=20, weight="bold"),
            text_color=COL_TEXT,
        ).pack(side="left")
        ctk.CTkLabel(
            header, text="Snake", font=ctk.CTkFont(size=20, weight="bold"),
            text_color=COL_ACCENT,
        ).pack(side="left")
        # The gear reads as a normal-weight glyph at this size, so it gets the
        # full text colour rather than the dimmed one, and turns red on hover.
        self._gear_icon = render_svg_icon(ICON_SETTINGS_SVG, 18, tint=COL_TEXT)
        self._gear_icon_hover = render_svg_icon(ICON_SETTINGS_SVG, 18, tint=COL_ACCENT)
        gear = ctk.CTkButton(
            header, text="", width=36, height=36, font=ctk.CTkFont(size=18),
            fg_color="transparent", hover_color=COL_PANEL_2, text_color=COL_TEXT,
            image=self._gear_icon, command=self._open_settings,
        )
        if self._gear_icon is None:  # no renderer — keep a visible glyph
            gear.configure(text="⚙")
        gear.pack(side="right")
        if self._gear_icon is not None and self._gear_icon_hover is not None:
            bind_hover_image(gear, self._gear_icon, self._gear_icon_hover)
        Tooltip(gear, "Settings — output folder, file-name prefix, overwriting")
        ctk.CTkLabel(
            side, text=f"{APP_SLOGAN} · svgo-py {engine.svgo_version()}",
            text_color=COL_TEXT_DIM, font=ctk.CTkFont(size=11), anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 4))

        # Files --------------------------------------------------------------
        files = ctk.CTkFrame(side, fg_color="transparent")
        files.grid(row=2, column=0, sticky="ew", padx=0, pady=0)
        files.grid_columnconfigure(0, weight=1)

        self._section(files, "1 · Files", row=0)
        btn_row = ctk.CTkFrame(files, fg_color="transparent")
        btn_row.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 6))
        ctk.CTkButton(
            btn_row, text="Open SVG files…", fg_color=COL_ACCENT, hover_color=COL_ACCENT_HOVER,
            text_color=COL_ACCENT_TEXT, height=40, font=ctk.CTkFont(size=14, weight="bold"),
            command=self._choose_files,
        ).pack(side="left", fill="x", expand=True)
        clear = ctk.CTkButton(
            btn_row, text="✕", width=40, height=40, fg_color=COL_PANEL_2,
            hover_color=COL_BORDER, command=self._clear_files,
        )
        clear.pack(side="left", padx=(8, 0))
        Tooltip(clear, "Remove all files from the list")

        self.drop_hint = ctk.CTkLabel(
            files, text="…or drag & drop them onto the preview.",
            text_color=COL_TEXT_DIM, font=ctk.CTkFont(size=11), anchor="w",
        )
        self.drop_hint.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 6))

        self.file_list = ctk.CTkScrollableFrame(
            files, fg_color=COL_PANEL_2, corner_radius=8, height=150,
        )
        self.file_list.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 10))
        self.file_list.grid_columnconfigure(0, weight=1)

        # Global settings ------------------------------------------------------
        opts = ctk.CTkFrame(side, fg_color="transparent")
        opts.grid(row=3, column=0, sticky="ew", padx=0, pady=0)
        opts.grid_columnconfigure(0, weight=1)

        self._section(opts, "2 · Global settings", pack=True)

        self.multipass_var = ctk.BooleanVar(value=bool(self.settings["multipass"]))
        self._switch(opts, "Multipass", self.multipass_var,
                     lambda: self._set_global("multipass", self.multipass_var.get()),
                     "Run the whole plugin chain repeatedly until the file stops "
                     "shrinking. Slower, usually a little smaller.")

        self.pretty_var = ctk.BooleanVar(value=bool(self.settings["pretty"]))
        self._switch(opts, "Prettify markup", self.pretty_var,
                     lambda: self._set_global("pretty", self.pretty_var.get()),
                     "Indent the output instead of putting it on one line. "
                     "Readable, but larger.")

        self.gzip_var = ctk.BooleanVar(value=bool(self.config_ref["compare_gzip"]))
        self._switch(opts, "Compare gzipped", self.gzip_var, self._on_gzip_toggle,
                     "Show the sizes after gzip compression — that is what a web "
                     "server actually sends.")

        self.float_slider, self.float_value = self._precision_row(
            opts, "Number precision", "floatPrecision",
            "Decimal places kept for coordinates and lengths. Lower is smaller "
            "but coarser; 3 is a good default.")

        self.transform_slider, self.transform_value = self._precision_row(
            opts, "Transform precision", "transformPrecision",
            "Decimal places kept inside transform() lists. Rounding these too "
            "hard visibly shifts shapes.")

        self._refresh_precision_labels()

    # ---- features panel (right column) ----------------------------------- #
    def _build_features(self) -> None:
        panel = ctk.CTkFrame(self, fg_color=COL_PANEL, corner_radius=0)
        panel.grid(row=0, column=2, sticky="nsew")
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(3, weight=1)

        head = ctk.CTkFrame(panel, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=16, pady=(18, 2))
        ctk.CTkLabel(
            head, text="3 · FEATURES", text_color=COL_ACCENT,
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(side="left")
        reset = ctk.CTkButton(
            head, text="Reset", width=60, height=26, fg_color=COL_PANEL_2,
            hover_color=COL_BORDER, command=self._reset_settings,
        )
        reset.pack(side="right")
        Tooltip(reset, "Back to SVGO's preset-default")

        # Second row: the summary on the left, the fold-all button on the right.
        sub = ctk.CTkFrame(panel, fg_color="transparent")
        sub.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 6))
        self.features_count = ctk.CTkLabel(
            sub, text="", text_color=COL_TEXT_DIM, font=ctk.CTkFont(size=11),
            anchor="w",
        )
        self.features_count.pack(side="left", fill="x", expand=True)
        self.fold_btn = ctk.CTkButton(
            sub, text="Collapse all", width=88, height=24, fg_color="transparent",
            hover_color=COL_PANEL_2, text_color=COL_TEXT_DIM,
            font=ctk.CTkFont(size=11), command=self._toggle_all_groups,
        )
        self.fold_btn.pack(side="right")

        self.filter_entry = ctk.CTkEntry(
            panel, placeholder_text="Filter features…", fg_color=COL_PANEL_2,
            border_color=COL_BORDER, height=30,
        )
        self.filter_entry.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 6))
        self.filter_entry.bind("<KeyRelease>", lambda _e: self._apply_filter())

        body = ctk.CTkScrollableFrame(panel, fg_color="transparent", corner_radius=0)
        body.grid(row=3, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        self._features_body = body

        collapsed = set(self.config_ref["collapsed_groups"] or [])
        for key, label, blurb, members in engine.plugins_by_category():
            group = PluginGroup(body, key, label, blurb, self)
            for pid, plugin_label, _default, help_text, _cat in members:
                var = ctk.BooleanVar(value=bool(self.settings["plugins"].get(pid, False)))
                self._plugin_vars[pid] = var
                group.add_plugin(pid, plugin_label, var, help_text)
            if key in collapsed:
                group.set_collapsed(True)
            self._groups.append(group)

        self._update_group_counts()

    def _section(self, parent, text: str, row: int | None = None, pack: bool = False):
        label = ctk.CTkLabel(
            parent, text=text.upper(), text_color=COL_ACCENT,
            font=ctk.CTkFont(size=12, weight="bold"), anchor="w",
        )
        if pack:
            label.pack(fill="x", padx=18, pady=(16, 6))
        else:
            label.grid(row=row, column=0, sticky="ew", padx=18, pady=(14, 6))
        return label

    def _switch(self, parent, text: str, var, command, tip: str = "",
                small: bool = False, padx: int = 18) -> ctk.CTkFrame:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=padx, pady=1)
        sw = ctk.CTkSwitch(
            row, text=text, variable=var, command=command,
            progress_color=COL_ACCENT, button_color=COL_ACCENT,
            button_hover_color=COL_ACCENT_HOVER, fg_color=COL_BORDER,
            text_color=COL_TEXT, switch_width=38, switch_height=18,
            font=ctk.CTkFont(size=12 if small else 13),
        )
        sw.pack(side="left", fill="x", expand=True)
        if tip:
            Tooltip(sw, tip)
        return row

    def _precision_row(self, parent, label: str, key: str,
                       tip: str) -> tuple[ctk.CTkSlider, ctk.CTkLabel]:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=(10, 0))
        name = ctk.CTkLabel(row, text=label, text_color=COL_TEXT_DIM)
        name.pack(side="left")
        # The value must be a child of `row`: a label parented on `parent` and
        # merely packed `in_=row` ends up *below* the row in the stacking order
        # and is never drawn.
        value_label = ctk.CTkLabel(
            row, text="", text_color=COL_TEXT, width=20,
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        value_label.pack(side="right")
        Tooltip(name, tip)
        slider = ctk.CTkSlider(
            parent, from_=engine.PRECISION_MIN, to=engine.PRECISION_MAX,
            number_of_steps=engine.PRECISION_MAX - engine.PRECISION_MIN,
            progress_color=COL_ACCENT, button_color=COL_ACCENT,
            button_hover_color=COL_ACCENT_HOVER, fg_color=COL_BORDER,
            command=lambda v, k=key, lbl=value_label: self._on_precision(k, lbl, v),
        )
        slider.set(int(self.settings[key]))
        slider.pack(fill="x", padx=18, pady=(2, 2))
        return slider, value_label

    # ---- main panel ----------------------------------------------------- #
    def _build_main(self) -> None:
        right = ctk.CTkFrame(self, fg_color=COL_BG, corner_radius=0)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)

        # Toolbar ------------------------------------------------------------
        bar = ctk.CTkFrame(right, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=22, pady=(18, 8))

        self.view_toggle = ctk.CTkSegmentedButton(
            bar, values=["Original", "Optimized"], command=self._on_view_toggle,
            selected_color=COL_ACCENT, selected_hover_color=COL_ACCENT_HOVER,
            unselected_color=COL_PANEL_2, fg_color=COL_PANEL,
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.view_toggle.set("Optimized")
        self.view_toggle.pack(side="left")
        Tooltip(self.view_toggle, "Flip between the original and the optimized "
                                  "document  ·  shortcut: Space")

        self.mode_toggle = ctk.CTkSegmentedButton(
            bar, values=["Image", "Markup"], command=self._on_mode_toggle,
            selected_color=COL_PANEL_2, selected_hover_color=COL_BORDER,
            unselected_color=COL_PANEL, fg_color=COL_PANEL,
        )
        self.mode_toggle.set("Image")
        self.mode_toggle.pack(side="left", padx=(12, 0))

        self.info_label = ctk.CTkLabel(bar, text="", text_color=COL_TEXT_DIM)
        self.info_label.pack(side="right")

        # Stage --------------------------------------------------------------
        stage = ctk.CTkFrame(
            right, fg_color=COL_PANEL, corner_radius=12, border_width=1,
            border_color=COL_BORDER,
        )
        stage.grid(row=1, column=0, sticky="nsew", padx=22, pady=4)
        stage.grid_columnconfigure(0, weight=1)
        stage.grid_rowconfigure(0, weight=1)

        self.viewer = ImageViewer(
            stage, bg_mode=str(self.config_ref["bg_mode"]), fg_color=COL_PANEL,
            corner_radius=12,
        )
        self.viewer.grid(row=0, column=0, sticky="nsew")

        self.code_view = CodeView(stage, fg_color=COL_PANEL, corner_radius=12)
        # placed on demand by _on_mode_toggle

        # Floats over whichever of the two is showing; hidden while idle.
        self.spinner = Spinner(stage)

        # Viewer controls ----------------------------------------------------
        ctrl = ctk.CTkFrame(right, fg_color="transparent")
        ctrl.grid(row=2, column=0, sticky="ew", padx=22, pady=(8, 0))

        self.bg_menu = ctk.CTkOptionMenu(
            ctrl, values=[BG_LABELS[m] for m in BG_MODES], width=140, height=30,
            command=self._on_bg_change, fg_color=COL_PANEL_2, button_color=COL_PANEL_2,
            button_hover_color=COL_BORDER, text_color=COL_TEXT,
        )
        self.bg_menu.set(BG_LABELS.get(str(self.config_ref["bg_mode"]), "Checkerboard"))
        self.bg_menu.pack(side="left")
        Tooltip(self.bg_menu, "Backdrop behind the image")

        for text, cmd, tip in (
            ("−", lambda: self.viewer.zoom_by(1 / 1.3), "Zoom out"),
            ("Fit", self._fit_view, "Reset zoom and position  ·  double-click the image"),
            ("+", lambda: self.viewer.zoom_by(1.3), "Zoom in"),
        ):
            b = ctk.CTkButton(ctrl, text=text, width=44, height=30, fg_color=COL_PANEL_2,
                              hover_color=COL_BORDER, command=cmd)
            b.pack(side="left", padx=(8, 0))
            Tooltip(b, tip)

        self.copy_btn = ctk.CTkButton(
            ctrl, text="Copy markup", height=30, width=110, fg_color=COL_PANEL_2,
            hover_color=COL_BORDER, command=self._copy_markup,
        )
        self.copy_btn.pack(side="right")
        Tooltip(self.copy_btn, "Copy the shown SVG source to the clipboard")

        # Stats --------------------------------------------------------------
        stats = ctk.CTkFrame(right, fg_color=COL_PANEL, corner_radius=12)
        stats.grid(row=3, column=0, sticky="ew", padx=22, pady=(10, 6))
        stats.grid_columnconfigure((0, 1, 2), weight=1)
        self.head_original, self.stat_original = self._stat(stats, 0, "Original")
        self.head_optimized, self.stat_optimized = self._stat(stats, 1, "Optimized")
        self.head_saved, self.stat_saved, self.saved_pie = self._stat(
            stats, 2, "Saved", pie=True)

        # Actions -------------------------------------------------------------
        actions = ctk.CTkFrame(right, fg_color="transparent")
        actions.grid(row=4, column=0, sticky="ew", padx=22, pady=(0, 6))

        self.save_btn = ctk.CTkButton(
            actions, text="Save", height=42, width=150, fg_color=COL_ACCENT,
            hover_color=COL_ACCENT_HOVER, text_color=COL_ACCENT_TEXT,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._save_current, state="disabled",
        )
        self.save_btn.pack(side="left")
        Tooltip(self.save_btn, "Write the optimized file using the prefix from "
                               "Settings  ·  Ctrl+S")

        self.save_as_btn = ctk.CTkButton(
            actions, text="Save as…", height=42, width=110, fg_color=COL_PANEL_2,
            hover_color=COL_BORDER, command=self._save_as, state="disabled",
        )
        self.save_as_btn.pack(side="left", padx=(10, 0))

        self.save_all_btn = ctk.CTkButton(
            actions, text="Save all", height=42, width=130, fg_color=COL_PANEL_2,
            hover_color=COL_BORDER, command=self._save_all, state="disabled",
        )
        self.save_all_btn.pack(side="left", padx=(10, 0))
        Tooltip(self.save_all_btn, "Optimize every loaded file with the current "
                                   "settings and write them all  ·  Ctrl+Shift+S")

        self.target_hint = ctk.CTkLabel(
            actions, text="", text_color=COL_TEXT_DIM, anchor="e",
            font=ctk.CTkFont(size=11), justify="right",
        )
        self.target_hint.pack(side="right")

        # Status --------------------------------------------------------------
        foot = ctk.CTkFrame(right, fg_color="transparent")
        foot.grid(row=5, column=0, sticky="ew", padx=22, pady=(0, 16))
        self.progress = ctk.CTkProgressBar(foot, progress_color=COL_ACCENT, height=4)
        self.progress.set(0)
        self.progress.pack(fill="x", pady=(0, 4))
        self._show_progress(False)
        self.status = ctk.CTkLabel(
            foot, text="Ready.", text_color=COL_TEXT_DIM, anchor="w",
        )
        self.status.pack(fill="x")

        self.refresh_target_hint()

    def _stat(self, parent, col: int, title: str, pie: bool = False):
        cell = ctk.CTkFrame(parent, fg_color="transparent")
        cell.grid(row=0, column=col, sticky="nsew", padx=18, pady=12)
        heading = ctk.CTkLabel(
            cell, text=title.upper(), text_color=COL_TEXT_DIM,
            font=ctk.CTkFont(size=11, weight="bold"),
        )
        heading.pack(anchor="w")
        # The pie sits beside the number, so that row needs its own container.
        row = ctk.CTkFrame(cell, fg_color="transparent")
        row.pack(anchor="w", fill="x")
        value = ctk.CTkLabel(
            row, text="–", text_color=COL_TEXT,
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        value.pack(side="left")
        chart = None
        if pie:
            chart = SavedPie(row)
            chart.pack(side="left", padx=(14, 0))
        Tooltip(cell, "Compare gzipped decides whether these are the raw file "
                      "sizes or the sizes after gzip compression. The file list "
                      "always shows raw bytes.")
        return (heading, value, chart) if pie else (heading, value)

    # ---- file handling --------------------------------------------------- #
    def _choose_files(self) -> None:
        initial = self.config_ref["last_open_dir"] or str(Path.home())
        paths = filedialog.askopenfilenames(
            title="Open SVG files", filetypes=SVG_FILETYPES, initialdir=initial,
        )
        if paths:
            self._open_paths(list(paths))

    def _open_paths(self, paths: list[str]) -> None:
        added, skipped, first_index = 0, [], None
        for raw in paths:
            p = Path(raw)
            candidates = sorted(p.glob("*.svg")) if p.is_dir() else [p]
            for candidate in candidates:
                if candidate.suffix.lower() not in (".svg", ".svgz"):
                    skipped.append(candidate.name)
                    continue
                index = self._add_doc(candidate)
                if index is not None:
                    added += 1
                    if first_index is None:
                        first_index = index

        if first_index is not None:
            folder = Path(paths[0])
            self.config_ref["last_open_dir"] = str(folder if folder.is_dir() else folder.parent)
            self.config_ref.save()
            self._update_file_list()
            self._select(first_index)
            noun = "file" if added == 1 else "files"
            self.status.configure(text=f"Opened {added} {noun}.", text_color=COL_TEXT_DIM)
        if skipped:
            self.status.configure(
                text=f"Ignored {len(skipped)} non-SVG file(s): {', '.join(skipped[:3])}"
                     + ("…" if len(skipped) > 3 else ""),
                text_color=COL_ERROR,
            )

    def _add_doc(self, path: Path) -> int | None:
        """Load ``path`` into the list and return its index, or None on failure."""
        try:
            data = read_svg(path)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not read:\n{path}\n\n{exc}")
            return None
        if b"<svg" not in data[:5000]:
            messagebox.showwarning(APP_NAME, f"Not an SVG document:\n{path}")
            return None
        for i, existing in enumerate(self.docs):
            if existing.path == path:  # re-opened -> refresh it in place
                existing.original = data
                existing.invalidate()
                existing.drop_bitmaps()
                return i
        self.docs.append(SvgDoc(path, data))
        return len(self.docs) - 1

    def _clear_files(self) -> None:
        self.docs.clear()
        self.current = -1
        self._update_file_list()
        self._refresh_all()
        self.status.configure(text="Ready.", text_color=COL_TEXT_DIM)

    def _remove_doc(self, index: int) -> None:
        if not 0 <= index < len(self.docs):
            return
        self.docs.pop(index)
        if not self.docs:
            self.current = -1
        else:
            self.current = min(self.current, len(self.docs) - 1)
        self._update_file_list()
        self._refresh_all()

    def _update_file_list(self) -> None:
        for child in self.file_list.winfo_children():
            child.destroy()
        self._row_widgets = []

        if not self.docs:
            ctk.CTkLabel(
                self.file_list, text="No files yet.", text_color=COL_TEXT_DIM,
                font=ctk.CTkFont(size=12),
            ).grid(row=0, column=0, sticky="w", padx=12, pady=12)
            return

        for i, doc in enumerate(self.docs):
            row = ctk.CTkFrame(self.file_list, fg_color="transparent", corner_radius=6)
            row.grid(row=i, column=0, sticky="ew", padx=4, pady=1)
            row.grid_columnconfigure(0, weight=1)

            name = ctk.CTkLabel(
                row, text=doc.name, anchor="w", text_color=COL_TEXT,
                font=ctk.CTkFont(size=12), cursor="hand2",
            )
            name.grid(row=0, column=0, sticky="ew", padx=(8, 4), pady=4)
            size = ctk.CTkLabel(
                row, text=human_size(len(doc.original)), anchor="e",
                text_color=COL_TEXT_DIM, font=ctk.CTkFont(size=11), cursor="hand2",
            )
            size.grid(row=0, column=1, sticky="e", padx=(0, 4))
            close = ctk.CTkButton(
                row, text="✕", width=22, height=22, fg_color="transparent",
                hover_color=COL_BORDER, text_color=COL_TEXT_DIM,
                font=ctk.CTkFont(size=11), command=lambda idx=i: self._remove_doc(idx),
            )
            close.grid(row=0, column=2, padx=(0, 4))

            for widget in (row, name, size):
                widget.bind("<Button-1>", lambda _e, idx=i: self._select(idx))
            Tooltip(name, str(doc.path))
            self._row_widgets.append((row, name, size))

        self._highlight_row()

    def _highlight_row(self) -> None:
        for i, (row, name, size) in enumerate(self._row_widgets):
            selected = i == self.current
            row.configure(fg_color=COL_ACCENT if selected else "transparent")
            name.configure(text_color=COL_ACCENT_TEXT if selected else COL_TEXT)
            size.configure(text_color=COL_ACCENT_TEXT if selected else COL_TEXT_DIM)

    def _select(self, index: int) -> None:
        if not 0 <= index < len(self.docs):
            return
        self.current = index
        # Preview bitmaps are large; only the selected document keeps its own.
        # The others are re-rendered on demand when they are selected again.
        for i, doc in enumerate(self.docs):
            if i != index:
                doc.drop_bitmaps()
        self._highlight_row()
        self.viewer.reset_view()
        self._refresh_all()

    @property
    def doc(self) -> SvgDoc | None:
        return self.docs[self.current] if 0 <= self.current < len(self.docs) else None

    # ---- settings changes ------------------------------------------------ #
    def _set_global(self, key: str, value) -> None:
        self.settings[key] = bool(value)
        self._settings_changed()

    def _set_plugin(self, pid: str, value) -> None:
        self.settings["plugins"][pid] = bool(value)
        self._update_group_counts()
        self._settings_changed()

    def _update_group_counts(self) -> None:
        """Refresh the 'n/m' badge on every group and the panel's summary."""
        enabled = total = 0
        for group in self._groups:
            group_on, group_total = group.update_count(self.settings["plugins"])
            enabled += group_on
            total += group_total
        self.features_count.configure(
            text=f"{enabled} of {total} features enabled", text_color=COL_TEXT_DIM)

    def _toggle_all_groups(self) -> None:
        collapse = any(not group.collapsed for group in self._groups)
        for group in self._groups:
            group.set_collapsed(collapse)
        self.fold_btn.configure(text="Expand all" if collapse else "Collapse all")
        self.persist_group_state()

    def persist_group_state(self) -> None:
        collapsed = [g.key for g in self._groups if g.collapsed]
        self.config_ref["collapsed_groups"] = collapsed
        self.config_ref.save()
        self.fold_btn.configure(
            text="Expand all" if len(collapsed) == len(self._groups) else "Collapse all")

    def _on_precision(self, key: str, value_label, value: float) -> None:
        iv = int(round(value))
        if self.settings[key] == iv:
            return
        self.settings[key] = iv
        value_label.configure(text=str(iv))
        self._settings_changed()

    def _refresh_precision_labels(self) -> None:
        self.float_value.configure(text=str(int(self.settings["floatPrecision"])))
        self.transform_value.configure(text=str(int(self.settings["transformPrecision"])))

    def _settings_changed(self) -> None:
        for doc in self.docs:
            doc.invalidate()
        self.config_ref["svgo_settings"] = self.settings
        if self._optimize_after is not None:
            try:
                self.after_cancel(self._optimize_after)
            except Exception:
                pass
        self._optimize_after = self.after(280, self._refresh_all)

    def _reset_settings(self) -> None:
        defaults = engine.default_settings()
        self.settings.clear()
        self.settings.update(defaults)
        self.float_slider.set(int(defaults["floatPrecision"]))
        self.transform_slider.set(int(defaults["transformPrecision"]))
        self.multipass_var.set(defaults["multipass"])
        self.pretty_var.set(defaults["pretty"])
        for pid, var in self._plugin_vars.items():
            var.set(bool(defaults["plugins"][pid]))
        self._refresh_precision_labels()
        self._update_group_counts()
        self._settings_changed()

    def _apply_filter(self) -> None:
        needle = self.filter_entry.get().strip().lower()
        # Groups re-pack themselves so the catalogue order survives repeated
        # filtering; a group with no hits disappears entirely.
        matches = sum(group.apply_filter(needle) for group in self._groups)
        if needle:
            noun = "feature" if matches == 1 else "features"
            self.features_count.configure(text=f"{matches} {noun} match “{needle}”.")
        else:
            self._update_group_counts()

    def _on_gzip_toggle(self) -> None:
        self.config_ref["compare_gzip"] = bool(self.gzip_var.get())
        self.config_ref.save()
        self._update_stats()

    # ---- optimization ---------------------------------------------------- #
    def _stamp(self) -> str:
        return json.dumps(self.settings, sort_keys=True)

    def _show_progress(self, visible: bool) -> None:
        """Show or hide the progress bar without letting the layout jump.

        A bar that sits there at zero claims something is running. Un-packing
        it would shift the status line under it every time a job starts, so it
        keeps its four pixels and is simply painted in the window's own
        background colour while idle.
        """
        self.progress.configure(
            fg_color=COL_PANEL_2 if visible else COL_BG,
            progress_color=COL_ACCENT if visible else COL_BG,
        )

    def _set_working(self, working: bool, text: str = "") -> None:
        """Turn the busy indicators on or off — spinner, progress bar."""
        if working:
            self.spinner.start(text or "Updating preview…")
            self.progress.configure(mode="indeterminate")
            self._show_progress(True)
            self.progress.start()
        else:
            self.spinner.stop()
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.progress.set(0)
            self._show_progress(False)

    def _refresh_all(self) -> None:
        """Re-run SVGO for the selected document and refresh everything.

        The run itself happens in :class:`PreviewWorker`; all this does is hand
        it the job and put the window into its "working" state. Whatever is
        already known — the original and its size — goes on screen right away,
        so switching files never leaves the previous file's numbers up.
        """
        self._optimize_after = None
        doc = self.doc
        if doc is None:
            self._finish_job()
            self.viewer.set_image(
                None, "Open an SVG file,\nor drag & drop one here.")
            self.code_view.set_content("")
            self.info_label.configure(text="")
            for stat in (self.stat_original, self.stat_optimized, self.stat_saved):
                stat.configure(text="–", text_color=COL_TEXT)
            self.saved_pie.set_percent(None)
            self._set_actions_enabled(False)
            return

        stamp = self._stamp()
        if doc.optimized is not None and doc.stamp == stamp and doc.render_stamp == stamp:
            self._finish_job()
            self._show_doc()
            return

        settings = json.loads(json.dumps(self.settings))
        self._job = (doc, stamp)
        # Submitting cancels whatever was still running for the previous
        # settings, so the newest change is always the one being worked on.
        self.worker.submit(doc.original, settings, str(doc.path),
                           render_original=doc.original_pil is None)
        self._set_working(True, f"Optimizing {doc.name}…")
        self.status.configure(text=f"Optimizing {doc.name}…", text_color=COL_TEXT_DIM)
        self._show_doc()

    def _on_worker_degraded(self) -> None:
        """No child process available: the preview is back in this process."""
        self.status.configure(
            text="Preview worker unavailable — the window may stutter while "
                 "large files update.",
            text_color=COL_TEXT_DIM,
        )

    def _finish_job(self) -> None:
        """Drop the job in flight, if any, and leave the working state."""
        if self._job is not None:
            self.worker.cancel()
            self._job = None
        self._set_working(False)

    def _on_optimized(self, _seq: int, optimized: bytes | None, error: str | None,
                      original_png: bytes | None, optimized_png: bytes | None) -> None:
        """A finished job, handed over on the UI thread by the worker."""
        if self._job is None:
            return
        doc, stamp = self._job
        self._job = None
        self._set_working(False)

        if original_png is not None:
            doc.original_pil = png_to_pil(original_png)
        doc.optimized_pil = png_to_pil(optimized_png)
        doc.optimized = optimized
        doc.error = error
        doc.stamp = stamp
        doc.render_stamp = stamp

        if error:
            self.status.configure(text=f"SVGO error: {error}", text_color=COL_ERROR)
        else:
            saved = saved_percent(len(doc.original), len(optimized or b""))
            verb = "smaller" if saved >= 0 else "larger"
            self.status.configure(
                text=f"{doc.name} · {human_size(len(optimized or b''))} "
                     f"· {abs(saved):.1f}% {verb} than the original",
                text_color=COL_TEXT_DIM,
            )
        if self.doc is doc:
            self._show_doc()

    def _show_doc(self) -> None:
        doc = self.doc
        if doc is None:
            return
        optimized_ok = doc.optimized is not None
        showing_opt = self.showing_optimized and optimized_ok

        image = doc.optimized_pil if showing_opt else doc.original_pil
        data = doc.optimized if showing_opt else doc.original
        if image is not None:
            self.viewer.set_image(image)
        elif doc.render_stamp is None:
            self.viewer.set_image(None, "Rendering…")
        else:
            self.viewer.set_image(None, "Preview could not be rendered.\n"
                                        "The file itself is fine — switch to Markup.")

        self.code_view.set_content((data or b"").decode("utf-8", "replace"))

        width, height = engine.svg_dimensions(data or b"")
        dims = f"{width:g} × {height:g}" if width and height else "?"
        label = "Optimized" if showing_opt else "Original"
        self.info_label.configure(
            text=f"{label} · {dims} · {human_size(len(data or b''))}"
        )
        self._update_stats()
        self._set_actions_enabled(optimized_ok)

    def _update_stats(self) -> None:
        doc = self.doc
        if doc is None:
            return
        gzipped = bool(self.gzip_var.get())
        # Say so in the headings: otherwise these numbers silently disagree with
        # the raw sizes in the file list and in the toolbar.
        note = " · GZIPPED" if gzipped else ""
        self.head_original.configure(text="ORIGINAL" + note)
        self.head_optimized.configure(text="OPTIMIZED" + note)
        self.head_saved.configure(text="SAVED" + note)

        before = engine.gzip_size(doc.original) if gzipped else len(doc.original)

        if doc.optimized is None:
            self.stat_original.configure(text=human_size(before), text_color=COL_TEXT)
            self.stat_optimized.configure(text="–", text_color=COL_TEXT)
            self.stat_saved.configure(text="–", text_color=COL_TEXT)
            self.saved_pie.set_percent(None)
            return

        after = engine.gzip_size(doc.optimized) if gzipped else len(doc.optimized)
        saved = saved_percent(before, after)
        self.stat_original.configure(text=human_size(before), text_color=COL_TEXT)
        self.stat_optimized.configure(text=human_size(after), text_color=COL_TEXT)
        self.stat_saved.configure(
            text=f"{saved:.1f}%" if saved >= 0 else f"+{-saved:.1f}%",
            text_color=COL_GREEN if saved > 0 else COL_ERROR,
        )
        self.saved_pie.set_percent(saved)

    def _set_actions_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled and not self._busy else "disabled"
        # A disabled accent button still reads as "press me", so grey it out.
        self.save_btn.configure(
            state=state,
            fg_color=COL_ACCENT if state == "normal" else COL_PANEL_2,
            hover_color=COL_ACCENT_HOVER if state == "normal" else COL_BORDER,
            text_color=COL_ACCENT_TEXT if state == "normal" else COL_TEXT_DIM,
        )
        self.save_as_btn.configure(state=state)
        self.save_all_btn.configure(
            state="normal" if self.docs and not self._busy else "disabled")
        if self.docs:
            self.save_all_btn.configure(text=f"Save all ({len(self.docs)})")
        else:
            self.save_all_btn.configure(text="Save all")

    # ---- view toggles ---------------------------------------------------- #
    def _on_view_toggle(self, value: str) -> None:
        self.showing_optimized = value == "Optimized"
        self._show_doc()

    def _on_mode_toggle(self, value: str) -> None:
        if value == "Markup":
            self.viewer.grid_forget()
            self.code_view.grid(row=0, column=0, sticky="nsew")
            self.code_view.flush()
        else:
            self.code_view.grid_forget()
            self.viewer.grid(row=0, column=0, sticky="nsew")

    def _on_bg_change(self, label: str) -> None:
        mode = next((m for m, lab in BG_LABELS.items() if lab == label), "checker")
        self.viewer.set_bg_mode(mode)
        self.config_ref["bg_mode"] = mode
        self.config_ref.save()

    def _fit_view(self) -> None:
        self.viewer.reset_view()

    def _copy_markup(self) -> None:
        doc = self.doc
        if doc is None:
            return
        data = doc.optimized if (self.showing_optimized and doc.optimized) else doc.original
        self.clipboard_clear()
        self.clipboard_append(data.decode("utf-8", "replace"))
        self.status.configure(text="Markup copied to the clipboard.", text_color=COL_TEXT_DIM)

    # ---- saving ---------------------------------------------------------- #
    def target_path(self, doc: SvgDoc) -> Path:
        """Where ``doc`` would be written given the current output settings."""
        prefix = str(self.config_ref["prefix"])
        suffix = str(self.config_ref["suffix"])
        stem = doc.path.stem
        name = f"{prefix}{stem}{suffix}.svg"
        if self.config_ref["use_source_dir"]:
            folder = doc.path.parent
        else:
            folder = Path(str(self.config_ref["output_dir"]) or doc.path.parent)
        return folder / name

    def refresh_target_hint(self) -> None:
        prefix = str(self.config_ref["prefix"])
        suffix = str(self.config_ref["suffix"])
        if self.config_ref["use_source_dir"]:
            where = "the source folder"
        else:
            where = _short_path(str(self.config_ref["output_dir"])) or "the source folder"
        pattern = f"{prefix}<name>{suffix}.svg"
        overwrite = " · overwrites silently" if self.config_ref["overwrite"] else ""
        self.target_hint.configure(text=f"Saves as  {pattern}\nin {where}{overwrite}")

    def _confirm_overwrite(self, path: Path) -> bool:
        if not path.exists() or self.config_ref["overwrite"]:
            return True
        return messagebox.askyesno(
            APP_NAME,
            f"{path.name} already exists in\n{path.parent}\n\nReplace it?",
            icon="warning",
        )

    def _write(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def _save_current(self) -> None:
        doc = self.doc
        if doc is None or doc.optimized is None or self._busy:
            return
        target = self.target_path(doc)
        if not self._confirm_overwrite(target):
            self.status.configure(text="Save cancelled.", text_color=COL_TEXT_DIM)
            return
        try:
            self._write(target, doc.optimized)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not save:\n{target}\n\n{exc}")
            return
        saved = saved_percent(len(doc.original), len(doc.optimized))
        self.status.configure(
            text=f"Saved {target.name} · {human_size(len(doc.optimized))} "
                 f"({saved:.1f}% smaller) → {_short_path(str(target.parent), 3)}",
            text_color=COL_GREEN,
        )

    def _save_as(self) -> None:
        doc = self.doc
        if doc is None or doc.optimized is None:
            return
        target = self.target_path(doc)
        path = filedialog.asksaveasfilename(
            title="Save optimized SVG as", defaultextension=".svg",
            initialfile=target.name, initialdir=str(target.parent),
            filetypes=[("SVG image", "*.svg"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            self._write(Path(path), doc.optimized)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not save:\n{path}\n\n{exc}")
            return
        self.status.configure(text=f"Saved {Path(path).name}", text_color=COL_GREEN)

    def _save_all(self) -> None:
        if not self.docs or self._busy:
            return

        targets = [(doc, self.target_path(doc)) for doc in self.docs]
        clashes = [t for _doc, t in targets if t.exists()]
        if clashes and not self.config_ref["overwrite"]:
            listed = "\n".join(f"• {p.name}" for p in clashes[:8])
            more = f"\n… and {len(clashes) - 8} more" if len(clashes) > 8 else ""
            if not messagebox.askyesno(
                APP_NAME,
                f"{len(clashes)} file(s) already exist and will be replaced:\n\n"
                f"{listed}{more}\n\nContinue?",
                icon="warning",
            ):
                self.status.configure(text="Save all cancelled.", text_color=COL_TEXT_DIM)
                return

        self._busy = True
        self._set_actions_enabled(False)
        self.spinner.start("Saving optimized files…")
        self.progress.configure(mode="determinate")
        self.progress.set(0)
        self._show_progress(True)
        settings = json.loads(json.dumps(self.settings))
        total = len(targets)

        def worker() -> None:
            written, failures, before_total, after_total = 0, [], 0, 0
            for i, (doc, target) in enumerate(targets):
                self.after(0, lambda n=i, d=doc: self.status.configure(
                    text=f"Optimizing {d.name}  ({n + 1}/{total})…",
                    text_color=COL_TEXT_DIM))
                try:
                    data = doc.optimized
                    if data is None or doc.stamp != json.dumps(settings, sort_keys=True):
                        data = engine.optimize_svg(doc.original, settings, str(doc.path))
                    self._write(target, data)
                    written += 1
                    before_total += len(doc.original)
                    after_total += len(data)
                except Exception as exc:
                    failures.append(f"{doc.name}: {exc}")
                self.after(0, lambda p=(i + 1) / total: self.progress.set(p))

            self.after(0, lambda: self._on_save_all_done(
                written, failures, before_total, after_total))

        threading.Thread(target=worker, daemon=True).start()

    def _on_save_all_done(self, written: int, failures: list[str],
                          before: int, after: int) -> None:
        self._busy = False
        self.spinner.stop()
        self.progress.set(0)
        self._show_progress(False)
        self._set_actions_enabled(self.doc is not None and self.doc.optimized is not None)
        noun = "file" if written == 1 else "files"
        if failures:
            self.status.configure(
                text=f"Saved {written} {noun}, {len(failures)} failed.", text_color=COL_ERROR)
            messagebox.showerror(
                APP_NAME, "Some files could not be saved:\n\n" + "\n".join(failures[:10]))
        else:
            self.status.configure(
                text=f"Saved {written} {noun} · {human_size(before)} → "
                     f"{human_size(after)} ({saved_percent(before, after):.1f}% smaller)",
                text_color=COL_GREEN,
            )
        # Re-run the selected document so the stats match what was written.
        self._refresh_all()

    # ---- settings dialog -------------------------------------------------- #
    def _open_settings(self) -> None:
        SettingsDialog(self, self.config_ref)


# --------------------------------------------------------------------------- #
#  Entry point
# --------------------------------------------------------------------------- #

def _set_windows_app_id() -> None:
    """Detach the task-bar entry from the Python host so it shows the snake."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def main() -> None:
    # A ssnakened worker re-runs this executable; without this it would open a
    # second window instead of answering jobs.
    mp.freeze_support()
    _set_windows_app_id()
    ctk.set_appearance_mode("dark")
    app = App()
    # Open files passed on the command line.
    if len(sys.argv) > 1:
        app.after(200, lambda: app._open_paths(sys.argv[1:]))
    app.mainloop()


if __name__ == "__main__":
    main()
