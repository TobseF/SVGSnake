"""
Out-of-process optimize & render worker
=======================================
The two slow steps behind the live preview are both bad neighbours for a Tk
event loop:

* ``svgo-py`` is pure Python, so it competes for the GIL the whole time it runs.
* ``resvg_py`` is a Rust extension that holds the GIL for the *entire* render —
  roughly a second for a busy drawing — during which the window cannot even
  repaint itself.

So neither of them runs in the UI process. This module is the child side: it
owns the renderers and SVGO, knows nothing about Tk, and speaks a deliberately
tiny protocol over two :mod:`multiprocessing` queues.

Protocol
--------
The parent puts :data:`Job` tuples on the job queue and reads :data:`Result`
tuples off the result queue; ``None`` on the job queue ends the loop. Bitmaps
travel as PNG bytes rather than as PIL images — that is what the renderer hands
us anyway, and it keeps the pickled payload small.

Cancelling a run is the parent's business: it kills the process (see
``PreviewWorker`` in ``svgsnake.py``), which is why nothing in here has to be
interruptible.
"""

from __future__ import annotations

import traceback

import svgo_engine as engine

__all__ = ["Job", "Result", "RENDER_SIZE", "render_svg_to_png", "run_job", "worker_loop"]

#: Long edge the preview bitmap is rasterized at. Zooming scales this bitmap.
RENDER_SIZE = 1400

#: ``(seq, svg_bytes, settings, path, render_original)`` — ``render_original``
#: is False when the parent still holds a usable bitmap of the source file.
Job = tuple

#: ``(seq, optimized_bytes | None, error | None, original_png | None,
#: optimized_png | None)``
Result = tuple


def render_svg_to_png(svg_bytes: bytes | None, target: int = RENDER_SIZE) -> bytes | None:
    """Rasterize an SVG document to PNG bytes for the preview.

    Uses resvg: browser-faithful (gradients, clip-paths, CSS-class styling) and
    shipped as self-contained wheels for every platform we target. Returns
    ``None`` when it is unavailable or the document does not render.
    """
    if not svg_bytes:
        return None

    try:
        import resvg_py  # type: ignore

        png = resvg_py.svg_to_bytes(
            svg_string=svg_bytes.decode("utf-8", "replace"), width=target
        )
        return bytes(png)
    except Exception:
        return None


def run_job(seq: int, svg: bytes, settings: dict, path: str,
            render_original: bool) -> Result:
    """Do one unit of preview work. Never raises — failures ride in the result."""
    try:
        optimized: bytes | None = engine.optimize_svg(svg, settings, path)
        error: str | None = None
    except engine.SvgoError as exc:
        optimized, error = None, str(exc)
    except Exception:  # pragma: no cover - defensive, svgo-py is third party
        optimized, error = None, traceback.format_exc().splitlines()[-1]

    original_png = render_svg_to_png(svg) if render_original else None
    optimized_png = render_svg_to_png(optimized)
    return seq, optimized, error, original_png, optimized_png


def worker_loop(jobs, results) -> None:
    """Child-process entry point: answer jobs until the parent says stop."""
    while True:
        try:
            job = jobs.get()
        except (EOFError, OSError, KeyboardInterrupt):
            return  # the parent went away
        if job is None:
            return
        try:
            results.put(run_job(*job))
        except (EOFError, OSError):
            return
