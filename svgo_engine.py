"""
SVGO engine bridge
==================
Thin layer between the UI and the pure-Python `svgo-py
<https://pypi.org/project/svgo-py/>`_ package (a 1:1 port of SVGO 4.1.0).

It owns everything the UI needs to know *about* SVGO but nothing about Tk:

* :data:`PLUGINS`          – ordered plugin catalogue (id, label, default, help).
* :data:`GLOBAL_DEFAULTS`  – global options (precision, multipass, prettify).
* :func:`default_settings` – a fresh, complete settings dict.
* :func:`normalize_settings` – merge a stale/partial dict onto the defaults.
* :func:`optimize_svg`     – run SVGO with a settings dict, return SVG bytes.
* :class:`SvgoError`       – raised when optimization fails.

The plugin catalogue and the way settings are turned into an SVGO plugin list
mirror `SVGOMG <https://svgomg.net>`_ (``src/config.json`` and
``src/js/svgo-worker/index.js``), so identical settings give identical output.
Two ids were renamed in SVGO 4 and are corrected here (``cleanupIDs`` ->
``cleanupIds``, ``removeScriptElement`` -> ``removeScripts``), and the plugins
that SVGO 4 added are appended to the catalogue.
"""

from __future__ import annotations

import gzip
import re
from typing import Any

from svgo import optimize as _svgo_optimize
from svgo.builtin import PRESET_DEFAULT_PLUGINS

__all__ = [
    "PLUGINS",
    "PLUGIN_IDS",
    "CATEGORIES",
    "CATEGORY_KEYS",
    "plugins_by_category",
    "GLOBAL_DEFAULTS",
    "PRECISION_MIN",
    "PRECISION_MAX",
    "SvgoError",
    "default_settings",
    "normalize_settings",
    "optimize_svg",
    "gzip_size",
    "svg_dimensions",
    "svgo_version",
]


# --------------------------------------------------------------------------- #
#  Plugin catalogue
# --------------------------------------------------------------------------- #
#: UI grouping for the plugin list: ``(key, label, blurb)`` in display order.
#: Purely cosmetic — it never affects the order the plugins actually run in.
CATEGORIES: list[tuple[str, str, str]] = [
    ("document", "Document & metadata",
     "Boilerplate and editor leftovers that no renderer needs."),
    ("styles", "Styles & colours",
     "What happens to <style>, style attributes and colour values."),
    ("attributes", "Attributes & numbers",
     "Rounding, defaults and tidying of ordinary attributes."),
    ("paths", "Shapes & paths",
     "Geometry — the biggest savings on drawing-heavy files."),
    ("structure", "Structure & reuse",
     "Groups, containers and repeated elements."),
    ("ids", "IDs & references",
     "ids, classes, and the links between elements."),
    ("strip", "Strip content",
     "Throws real content away. Only switch these on deliberately."),
]
CATEGORY_KEYS = [key for key, _label, _blurb in CATEGORIES]

# (id, category, label, help text).  The default state is *not* stored here: a
# plugin is enabled by default exactly when it is part of SVGO 4.1's
# `preset-default`, which is what `svgo-py` implements.  The order of this list
# is the order the plugins run in — it follows SVGOMG's config.json, with the
# plugins introduced by SVGO 4 slotted in where they run — so it must not be
# reshuffled to match the UI grouping.

_CATALOGUE: list[tuple[str, str, str, str]] = [
    ("removeDoctype", "document", "Remove doctype",
     "Drops the <!DOCTYPE> declaration. Browsers ignore it in SVG."),
    ("removeXMLProcInst", "document", "Remove XML instructions",
     "Drops the <?xml …?> processing instruction."),
    ("removeComments", "document", "Remove comments",
     "Drops <!-- … --> comments (legal comments starting with ! are kept)."),
    ("removeDeprecatedAttrs", "document", "Remove deprecated attrs",
     "Drops attributes that are deprecated in the SVG specification."),
    ("removeMetadata", "document", "Remove <metadata>",
     "Drops <metadata> blocks — usually editor bookkeeping such as RDF."),
    ("removeXMLNS", "strip", "Remove xmlns",
     "Drops the xmlns attribute. Only for inline SVG inside HTML — a stand-alone "
     "file will no longer render."),
    ("removeEditorsNSData", "document", "Remove editor data",
     "Drops namespaces and attributes left behind by Inkscape, Illustrator, Sketch…"),
    ("cleanupAttrs", "attributes", "Clean up attr whitespace",
     "Collapses newlines and repeated spaces inside attribute values."),
    ("mergeStyles", "styles", "Merge styles",
     "Merges several <style> elements into one."),
    ("inlineStyles", "styles", "Inline styles",
     "Moves CSS rules from <style> onto the matching elements' style attribute."),
    ("minifyStyles", "styles", "Minify styles",
     "Minifies the CSS inside <style> elements and style attributes."),
    ("convertStyleToAttrs", "styles", "Style to attributes",
     "Converts style=\"fill:red\" into presentation attributes like fill=\"red\"."),
    ("cleanupIds", "ids", "Clean up IDs",
     "Removes unreferenced ids and shortens the ones that are used."),
    ("removeRasterImages", "strip", "Remove raster images",
     "Drops embedded PNG/JPEG <image> elements."),
    ("removeUselessDefs", "structure", "Remove unused defs",
     "Drops <defs> children that nothing references."),
    ("cleanupNumericValues", "attributes", "Round/rewrite numbers",
     "Rounds numbers to the number precision and strips default px units."),
    ("cleanupListOfValues", "attributes", "Round/rewrite number lists",
     "Applies the same rounding to list-valued attributes such as viewBox."),
    ("convertColors", "styles", "Minify colours",
     "Rewrites colours to their shortest form: rgb(255,0,0) → red → #f00."),
    ("removeUnknownsAndDefaults", "attributes", "Remove unknowns & defaults",
     "Drops non-SVG elements/attributes and attributes set to their default value."),
    ("removeNonInheritableGroupAttrs", "attributes", "Remove unneeded group attrs",
     "Drops non-inheritable presentation attributes from <g> elements."),
    ("removeUselessStrokeAndFill", "attributes", "Remove useless stroke/fill",
     "Drops stroke/fill attributes that have no visible effect."),
    ("removeViewBox", "strip", "Remove viewBox",
     "Drops viewBox when it matches width/height. Off by default in SVGO 4 — "
     "removing it breaks responsive scaling."),
    ("cleanupEnableBackground", "attributes", "Tidy enable-background",
     "Removes or fixes the enable-background attribute, which no browser supports."),
    ("removeHiddenElems", "structure", "Remove hidden elements",
     "Drops elements that cannot be seen (display:none, zero size, empty paths…)."),
    ("removeEmptyText", "structure", "Remove empty text",
     "Drops empty <text>, <tspan> and <tref> elements."),
    ("convertShapeToPath", "paths", "Shapes to paths",
     "Converts <rect>, <line>, <polygon>… into <path> when that is shorter."),
    ("convertEllipseToCircle", "paths", "Ellipse to circle",
     "Converts a non-eccentric <ellipse> into a <circle>."),
    ("moveElemsAttrsToGroup", "structure", "Move attrs to parent group",
     "Hoists attributes shared by all children onto their group."),
    ("moveGroupAttrsToElems", "structure", "Move group attrs to elems",
     "Pushes group transforms down onto the children so groups can collapse."),
    ("collapseGroups", "structure", "Collapse useless groups",
     "Removes <g> wrappers that carry no attributes of their own."),
    ("convertPathData", "paths", "Round/rewrite paths",
     "The big one: rounds path coordinates and picks the shortest command form."),
    ("convertTransform", "paths", "Round/rewrite transforms",
     "Collapses transform lists into a single, shortest-form transform."),
    ("convertOneStopGradients", "structure", "Convert one-stop gradients",
     "Replaces gradients that have a single stop with a plain colour."),
    ("removeEmptyAttrs", "attributes", "Remove empty attrs",
     "Drops attributes whose value is an empty string."),
    ("removeEmptyContainers", "structure", "Remove empty containers",
     "Drops container elements (<g>, <defs>, <symbol>…) with no children."),
    ("mergePaths", "paths", "Merge paths",
     "Merges adjacent paths that share all their styling into one."),
    ("removeUnusedNS", "document", "Remove unused namespaces",
     "Drops xmlns:* declarations that nothing in the document uses."),
    ("removeXlink", "ids", "Replace xlink with SVG 2",
     "Rewrites xlink:href to the modern href attribute."),
    ("reusePaths", "structure", "Reuse duplicate elements",
     "Moves repeated paths into <defs> and references them with <use>."),
    ("prefixIds", "ids", "Prefix IDs and classes",
     "Prefixes every id and class with the file name — useful when several SVGs "
     "are inlined into the same page."),
    ("sortAttrs", "attributes", "Sort attrs",
     "Sorts attributes into a stable order. No size win on its own, but it "
     "compresses better with gzip/brotli."),
    ("sortDefsChildren", "structure", "Sort children of <defs>",
     "Orders <defs> children for better compression."),
    ("removeTitle", "document", "Remove <title>",
     "Drops <title>. Off by default in SVGO 4 — it is the SVG's accessible name."),
    ("removeDesc", "document", "Remove <desc>",
     "Drops editor-generated <desc> elements."),
    ("removeDimensions", "strip", "Remove width/height",
     "Removes width/height and keeps viewBox, making the SVG scale to its box."),
    ("removeStyleElement", "styles", "Remove style elements",
     "Drops every <style> element — and with it all CSS styling."),
    ("removeScripts", "strip", "Remove script elements",
     "Drops <script> elements and on… event attributes."),
    ("removeOffCanvasPaths", "paths", "Remove off-canvas paths",
     "Drops paths that lie completely outside the viewBox."),
]

_PRESET_DEFAULT = set(PRESET_DEFAULT_PLUGINS)

#: Ordered catalogue of ``(id, label, enabled_by_default, help)`` tuples.
PLUGINS: list[tuple[str, str, bool, str, str]] = [
    (pid, label, pid in _PRESET_DEFAULT, help_text, category)
    for pid, category, label, help_text in _CATALOGUE
]
#: Plugin ids **in execution order**. This is what drives an SVGO run.
PLUGIN_IDS: list[str] = [p[0] for p in PLUGINS]


def plugins_by_category() -> list[tuple[str, str, str, list[tuple[str, str, bool, str, str]]]]:
    """Group :data:`PLUGINS` for display: ``(key, label, blurb, members)``.

    Members keep their catalogue (execution) order inside each group, and every
    plugin appears in exactly one group.
    """
    grouped: list[tuple[str, str, str, list]] = []
    for key, label, blurb in CATEGORIES:
        members = [p for p in PLUGINS if p[4] == key]
        if members:
            grouped.append((key, label, blurb, members))
    return grouped

#: Global options and their defaults (values taken from SVGOMG's index.html).
GLOBAL_DEFAULTS: dict[str, Any] = {
    "floatPrecision": 3,      # 0..8
    "transformPrecision": 5,  # 0..8
    "multipass": False,
    "pretty": False,
    "indent": 2,
}
PRECISION_MIN = 0
PRECISION_MAX = 8


class SvgoError(Exception):
    """Raised when SVGO could not optimize a document."""


def default_settings() -> dict[str, Any]:
    """Return a fresh, complete settings dict (globals + every plugin)."""
    return {
        **GLOBAL_DEFAULTS,
        "plugins": {pid: enabled for pid, _label, enabled, _help, _cat in PLUGINS},
    }


def normalize_settings(settings: dict | None) -> dict[str, Any]:
    """Merge a possibly partial or stale settings dict onto the defaults.

    Unknown plugin ids are dropped and missing ones filled in, so a config
    written by an older build stays usable.
    """
    base = default_settings()
    if not isinstance(settings, dict):
        return base
    for key in GLOBAL_DEFAULTS:
        if key in settings:
            base[key] = settings[key]
    plugins = settings.get("plugins")
    if isinstance(plugins, dict):
        for pid in base["plugins"]:
            if pid in plugins:
                base["plugins"][pid] = bool(plugins[pid])
    return base


def _plugin_list(settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn the settings dict into an SVGO plugin list (port of svgo-worker)."""
    float_precision = int(settings["floatPrecision"])
    transform_precision = int(settings["transformPrecision"])

    plugins: list[dict[str, Any]] = []
    for pid in PLUGIN_IDS:
        if not settings["plugins"].get(pid):
            continue
        # A precision of 0 almost always breaks the image when applied to
        # cleanupNumericValues, so SVGOMG bumps that one plugin to 1.
        precision = 1 if (pid == "cleanupNumericValues" and float_precision == 0) else float_precision
        plugins.append({
            "name": pid,
            "params": {
                "floatPrecision": precision,
                "transformPrecision": transform_precision,
            },
        })
    return plugins


def optimize_svg(svg: bytes | str, settings: dict[str, Any],
                 path: str | None = None) -> bytes:
    """Optimize ``svg`` with ``settings`` and return the optimized bytes.

    ``path`` is only passed through to SVGO so that plugins which look at the
    file name (``prefixIds``) behave the way the CLI would.

    Raises :class:`SvgoError` on any failure.
    """
    text = svg.decode("utf-8", "replace") if isinstance(svg, bytes) else svg
    settings = normalize_settings(settings)

    config: dict[str, Any] = {
        "multipass": bool(settings["multipass"]),
        "plugins": _plugin_list(settings),
        "js2svg": {
            "indent": int(settings.get("indent", 2)),
            "pretty": bool(settings["pretty"]),
        },
    }
    if path:
        config["path"] = path

    try:
        result = _svgo_optimize(text, config)
    except Exception as exc:  # svgo-py raises plain exceptions
        raise SvgoError(f"{type(exc).__name__}: {exc}") from exc

    data = result.get("data")
    if not isinstance(data, str):
        raise SvgoError("SVGO returned no data.")
    return data.encode("utf-8")


def gzip_size(data: bytes) -> int:
    """Size of ``data`` after gzip -9, the way SVGOMG's 'compare gzipped' does."""
    return len(gzip.compress(data, 9))


_WIDTH_RE = re.compile(r'\bwidth\s*=\s*"([^"]+)"')
_HEIGHT_RE = re.compile(r'\bheight\s*=\s*"([^"]+)"')
_VIEWBOX_RE = re.compile(r'\bviewBox\s*=\s*"([^"]+)"')
_NUM_RE = re.compile(r"-?[\d.]+")


def svg_dimensions(svg: bytes) -> tuple[float | None, float | None]:
    """Best-effort width/height of an SVG document, mirroring SVGOMG's extractor."""
    head = svg[:4000].decode("utf-8", "ignore")

    def _num(text: str) -> float | None:
        m = _NUM_RE.search(text)
        try:
            return float(m.group()) if m else None
        except ValueError:
            return None

    w_match, h_match = _WIDTH_RE.search(head), _HEIGHT_RE.search(head)
    if w_match and h_match:
        w, h = _num(w_match.group(1)), _num(h_match.group(1))
        if w and h:
            return w, h

    vb = _VIEWBOX_RE.search(head)
    if vb:
        parts = re.split(r"[,\s]+", vb.group(1).strip())
        if len(parts) == 4:
            try:
                return float(parts[2]), float(parts[3])
            except ValueError:
                pass
    return None, None


def svgo_version() -> str:
    """The SVGO version that `svgo-py` was ported from."""
    try:
        from svgo import VERSION
        return str(VERSION)
    except Exception:
        return "?"
