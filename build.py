"""
SVGSnake — build script (Windows and Linux)
=========================================
Compiles the app to a native program with **Nuitka** and packages the result
two ways: a portable archive that runs from any folder, and a platform
installer (Inno Setup on Windows, AppImage on Linux).

Nuitka translates the Python sources to C and hands them to a real optimizing
compiler, which is what makes this worth doing over a bytecode bundler: the
interpreter loop disappears from the hot paths, and with ``--lto`` the compiler
inlines across module boundaries.

Usage::

    python build.py                   # compile, then archive and installer
    python build.py --no-installer    # skip Inno Setup / appimagetool
    python build.py --no-archive      # skip the portable .zip / .tar.gz
    python build.py --clean           # discard previous build artefacts first
    python build.py --onefile         # single portable binary instead of a folder

The default is a *standalone folder* rather than ``--onefile``: onefile has to
unpack itself into a temp directory on every launch, which costs a second or two
of start-up. The installer puts the folder in place once, so the unpacking never
has to happen; the portable archive trades that away for not needing an install.

Cross-compiling is not possible — Nuitka embeds the running interpreter and the
native extension modules of the build machine. To produce the Linux artefacts
from a Windows host, run this script inside the container (see docker/README.md):

    docker compose run --rm build-linux
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import svgsnake  # noqa: E402  — for the single source of truth on name/version

APP_NAME = svgsnake.APP_NAME
APP_VERSION = svgsnake.APP_VERSION
APP_SLOGAN = svgsnake.APP_SLOGAN
APP_PUBLISHER = svgsnake.APP_PUBLISHER

# --------------------------------------------------------------------------- #
#  Platform
#
#  Everything that differs between targets is decided here once, so the rest of
#  the script can stay free of branching.
# --------------------------------------------------------------------------- #

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

#: The binary name. Windows keeps the branded spelling because it is what the
#: user sees in the Start menu; on Linux an executable on $PATH is lowercase.
EXE_NAME = "SVGSnake.exe" if IS_WINDOWS else "svgsnake"

PLATFORM_TAG = "windows" if IS_WINDOWS else "linux"

#: Normalised architecture for artefact file names. ``platform.machine()``
#: answers "AMD64" on Windows and "x86_64" on Linux for the same CPU.
ARCH_TAG = {
    "amd64": "x64", "x86_64": "x64",
    "aarch64": "arm64", "arm64": "arm64",
}.get(platform.machine().lower(), platform.machine().lower())

#: Archives: ZIP is what Windows can open without extra tooling. On Linux it is
#: the wrong choice — the format's Unix permission bits survive `unzip` but not
#: every graphical extractor, and a portable build whose binary arrives without
#: its executable bit is a support ticket waiting to happen. tar.gz keeps the
#: mode for certain. Override with --archive-format if you need to.
DEFAULT_ARCHIVE_FORMAT = "zip" if IS_WINDOWS else "gztar"
ARCHIVE_SUFFIX = {"zip": ".zip", "gztar": ".tar.gz"}

ENTRY = ROOT / "svgsnake.py"
ICON_DIR = ROOT / "icon"
ICON_ICO = ICON_DIR / "icon.ico"          # Windows executable resource
ICON_PNG = ICON_DIR / "icon.png"          # Linux .desktop / AppImage
#: Windows and Linux artefacts have to coexist — a container build must not
#: wipe the folder the host build just produced, so each target gets its own
#: subtree. The archives and installers inside are named per platform anyway;
#: this keeps the intermediate standalone folder from colliding too.
BUILD_DIR = ROOT / "build" / f"{PLATFORM_TAG}-{ARCH_TAG}"
DIST_ROOT = ROOT / "dist"
OUT_DIR = DIST_ROOT / f"{PLATFORM_TAG}-{ARCH_TAG}"
INSTALLER_DIR = ROOT / "installer"
ISS = INSTALLER_DIR / "svgsnake.iss"

#: Test frameworks and build tooling that Nuitka would otherwise drag in behind
#: a stray import. resvg is the only renderer, so nothing renderer-side is cut.
EXCLUDED_MODULES = [
    "pytest",
    "unittest",
    "setuptools",
    "pip",
    "numpy",
    "matplotlib",
]

#: tkinterdnd2 ships the tkdnd Tcl extension for every platform it supports and
#: for both Tcl 8.6 and 9. CPython 3.13 still links Tk 8.6, so exactly one of
#: these directories is useful per build and the other eleven are dead weight.
TKDND_VARIANTS = [
    "linux-arm64", "linux-arm64-tcl9", "linux-x64", "linux-x64-tcl9",
    "osx-arm64", "osx-arm64-tcl9", "osx-x64",
    "win-arm64", "win-x64", "win-x64-tcl9", "win-x86", "win-x86-tcl9",
]
TKDND_KEEP = f"{'win' if IS_WINDOWS else 'linux'}-{ARCH_TAG}"
UNUSED_TKDND = [
    f"tkinterdnd2/tkdnd/{name}/**"
    for name in TKDND_VARIANTS
    if name != TKDND_KEEP
]


def log(message: str) -> None:
    print(f"[build] {message}", flush=True)


def fail(message: str) -> "None":
    print(f"[build] ERROR: {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def check_environment() -> None:
    if not (IS_WINDOWS or IS_LINUX):
        fail(f"unsupported platform: {sys.platform} (this script builds Windows and Linux)")
    if not ENTRY.exists():
        fail(f"entry point not found: {ENTRY}")

    if IS_WINDOWS and (not ICON_ICO.exists() or (ICON_PNG.exists() and ICON_PNG.stat().st_mtime > ICON_ICO.stat().st_mtime)):
        if ICON_PNG.exists():
            from PIL import Image

            log(f"generating {ICON_ICO.name} from {ICON_PNG.name}")
            img = Image.open(ICON_PNG)
            img.save(
                ICON_ICO,
                format="ICO",
                sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
            )

    icon = ICON_ICO if IS_WINDOWS else ICON_PNG
    if not icon.exists():
        fail(f"icon not found: {icon}")

    if TKDND_KEEP not in TKDND_VARIANTS:
        fail(
            f"no tkdnd build for {TKDND_KEEP} — drag & drop would be missing.\n"
            f"        Known variants: {', '.join(TKDND_VARIANTS)}"
        )

    try:
        import nuitka  # noqa: F401
    except ImportError:
        fail("Nuitka is not installed. Run: pip install nuitka ordered-set zstandard")

    if IS_LINUX and not shutil.which("gcc"):
        fail("gcc not found. Nuitka needs a C compiler: apt-get install gcc patchelf")

    missing = []
    for module in ("customtkinter", "PIL", "tkinterdnd2", "resvg_py", "svgo"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        hint = "pip install -r requirements.txt"
        if IS_LINUX and "tkinter" in str(missing):
            hint += "  (and apt-get install python3-tk)"
        fail("missing runtime dependencies: " + ", ".join(missing) + f"\n        Run: {hint}")

    # Tkinter is a system package on Linux, not a wheel — its absence is the
    # single most common reason a fresh container fails halfway through.
    try:
        import tkinter  # noqa: F401
    except ImportError:
        fail("tkinter is missing. On Debian/Ubuntu: apt-get install python3-tk")


def find_msvc() -> bool:
    """True when Visual Studio's C++ toolchain is installed.

    MSVC is the compiler to prefer here — on Windows it consistently produces
    faster binaries than MinGW for CPython-shaped code, and Nuitka's LTO support
    is best tested against it.
    """
    vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / (
        "Microsoft Visual Studio/Installer/vswhere.exe"
    )
    if not vswhere.exists():
        return False
    try:
        out = subprocess.run(
            [
                str(vswhere), "-latest", "-products", "*",
                "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                "-property", "installationPath",
            ],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:
        return False
    return bool(out.stdout.strip())


def find_iscc() -> Path | None:
    """Locate the Inno Setup command-line compiler."""
    found = shutil.which("ISCC")
    if found:
        return Path(found)
    bases = [
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("ProgramFiles", r"C:\Program Files"),
    ]
    # winget installs Inno Setup per-user by default, which is not on PATH.
    local = os.environ.get("LOCALAPPDATA")
    if local:
        bases.append(str(Path(local) / "Programs"))
    for base in bases:
        for version in ("6", "5"):
            candidate = Path(base) / f"Inno Setup {version}" / "ISCC.exe"
            if candidate.exists():
                return candidate
    return None


def nuitka_command(onefile: bool, jobs: int) -> list[str]:
    cmd = [
        sys.executable, "-m", "nuitka",

        # -- packaging mode ------------------------------------------------- #
        "--standalone",
        "--assume-yes-for-downloads",
        f"--output-dir={BUILD_DIR}",
        f"--output-filename={EXE_NAME}",
        "--remove-output",          # drop the generated C once it is linked

        # -- optimization --------------------------------------------------- #
        "--lto=yes",                # let the linker optimize across objects
        f"--jobs={jobs}",
        "--python-flag=-OO",        # no asserts, no docstrings

        # -- the preview worker --------------------------------------------- #
        # The preview runs in a child process, which on Windows means the .exe
        # re-launches itself; the plugin is what makes that relaunch land in
        # the worker instead of in a second window.
        "--enable-plugin=multiprocessing",
        "--include-module=svgo_worker",

        # -- the GUI stack -------------------------------------------------- #
        "--enable-plugin=tk-inter",
        "--include-package=customtkinter",
        "--include-package-data=customtkinter",   # themes + bundled assets
        "--include-package=tkinterdnd2",
        "--include-package-data=tkinterdnd2",     # the tkdnd Tcl extension
        "--include-package=resvg_py",
        "--include-package=svgo",
        "--include-package-data=svgo",            # data/collections.json

        # -- our own assets ------------------------------------------------- #
        "--include-data-dir=icon=icon",
    ]

    if IS_WINDOWS:
        cmd += [
            # -- Windows metadata ------------------------------------------- #
            "--windows-console-mode=disable",
            f"--windows-icon-from-ico={ICON_ICO}",
            f"--company-name={APP_PUBLISHER}",
            f"--product-name={APP_NAME}",
            f"--file-version={APP_VERSION}",
            f"--product-version={APP_VERSION}",
            f"--file-description={APP_NAME} — {APP_SLOGAN}",
            f"--copyright=© {time.strftime('%Y')} {APP_PUBLISHER}",
        ]
        cmd.append("--msvc=latest" if find_msvc() else "--mingw64")
    else:
        # ELF binaries carry no version resource, so there is no Linux analogue
        # to the block above. The icon only matters for a onefile AppImage.
        if onefile:
            cmd.append(f"--linux-icon={ICON_PNG}")

    if onefile:
        cmd += ["--onefile", "--onefile-tempdir-spec={CACHE_DIR}/SVGSnake/{VERSION}"]

    cmd += [f"--nofollow-import-to={name}" for name in EXCLUDED_MODULES]
    cmd += [f"--noinclude-data-files={pattern}" for pattern in UNUSED_TKDND]
    cmd.append(str(ENTRY))
    return cmd


def compile_app(onefile: bool, jobs: int) -> Path:
    if IS_WINDOWS:
        log(f"compiler: {'MSVC (Visual Studio)' if find_msvc() else 'MinGW64 (Nuitka-managed)'}")
    else:
        log("compiler: gcc")
    log(f"target: {PLATFORM_TAG}-{ARCH_TAG}, tkdnd: {TKDND_KEEP}")
    log(f"mode: {'onefile' if onefile else 'standalone folder'}, jobs: {jobs}")
    log("compiling — this takes several minutes on a first run…")

    started = time.time()
    result = subprocess.run(nuitka_command(onefile, jobs), cwd=ROOT)
    if result.returncode != 0:
        fail(f"Nuitka exited with code {result.returncode}")
    log(f"compiled in {time.time() - started:.0f}s")

    produced = BUILD_DIR / ("svgsnake.dist" if not onefile else "")
    exe = (produced / EXE_NAME) if not onefile else (BUILD_DIR / EXE_NAME)
    if not exe.exists():
        fail(f"expected binary not found: {exe}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if onefile:
        target = OUT_DIR / EXE_NAME
        shutil.copy2(exe, target)
    else:
        target_dir = OUT_DIR / APP_NAME
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(produced, target_dir)
        target = target_dir / EXE_NAME

    if IS_LINUX:
        target.chmod(0o755)

    # Measure what was just produced, not everything sitting in dist/ — an
    # installer from an earlier run lives there too.
    measured = target.parent if not onefile else target
    size = (
        sum(f.stat().st_size for f in measured.rglob("*") if f.is_file())
        if measured.is_dir() else measured.stat().st_size
    )
    log(f"output: {target}  ({size / 1_048_576:.0f} MB)")
    return target


# --------------------------------------------------------------------------- #
#  Portable archive
# --------------------------------------------------------------------------- #

def build_archive(fmt: str) -> Path | None:
    """Pack dist/<AppName>/ into an archive that runs without installing."""
    folder = OUT_DIR / APP_NAME
    if not folder.is_dir():
        log("no standalone folder to archive — skipping.")
        return None

    stem = f"{APP_NAME}-{APP_VERSION}-{PLATFORM_TAG}-{ARCH_TAG}-portable"
    archive = OUT_DIR / f"{stem}{ARCHIVE_SUFFIX[fmt]}"
    if archive.exists():
        archive.unlink()

    log(f"packing the portable archive ({fmt})…")
    # root_dir/base_dir make the archive expand into one folder instead of
    # spraying 500 files into whatever directory the user unpacked it in.
    produced = shutil.make_archive(
        base_name=str(OUT_DIR / stem),
        format=fmt,
        root_dir=str(OUT_DIR),
        base_dir=APP_NAME,
    )
    produced = Path(produced)
    log(f"portable: {produced}  ({produced.stat().st_size / 1_048_576:.0f} MB)")
    if fmt == "zip" and IS_LINUX:
        log("  note: ZIP does not reliably carry the executable bit —")
        log(f"        users may need  chmod +x {APP_NAME}/{EXE_NAME}")
    return produced


# --------------------------------------------------------------------------- #
#  Installers
# --------------------------------------------------------------------------- #

def build_installer_windows() -> Path | None:
    iscc = find_iscc()
    if iscc is None:
        log("Inno Setup not found — skipping the installer.")
        log("  Install it with:  winget install -e --id JRSoftware.InnoSetup")
        log(f"  Then run:         \"{ISS}\"  (or this script again)")
        return None
    if not ISS.exists():
        fail(f"installer script not found: {ISS}")

    log(f"building the installer with {iscc}")
    result = subprocess.run(
        [
            str(iscc),
            f"/DAppVersion={APP_VERSION}",
            f"/DSourceDir={OUT_DIR / APP_NAME}",
            f"/DOutputDir={OUT_DIR}",
            str(ISS),
        ],
        cwd=ROOT,
    )
    if result.returncode != 0:
        fail(f"ISCC exited with code {result.returncode}")

    setup = OUT_DIR / f"{APP_NAME}-{APP_VERSION}-Setup.exe"
    if setup.exists():
        log(f"installer: {setup}  ({setup.stat().st_size / 1_048_576:.0f} MB)")
        return setup
    log("installer built, but the expected file name was not produced.")
    return None


DESKTOP_ENTRY = f"""[Desktop Entry]
Type=Application
Name={APP_NAME}
Comment={APP_SLOGAN}
Exec={EXE_NAME} %f
Icon=svgsnake
Categories=Graphics;Development;
MimeType=image/svg+xml;
Terminal=false
"""

# AppRun is the entry point every AppImage must have at its root. resolve the
# symlink first: the runtime mounts us at a path that differs on every launch.
APPRUN = f"""#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/{EXE_NAME}" "$@"
"""


def build_appimage() -> Path | None:
    """Wrap dist/<AppName>/ into a self-contained .AppImage."""
    tool = shutil.which("appimagetool")
    if tool is None:
        log("appimagetool not found — skipping the AppImage.")
        log("  Get it from https://github.com/AppImage/appimagetool/releases")
        log("  (the container image in docker/ already has it)")
        return None

    folder = OUT_DIR / APP_NAME
    if not folder.is_dir():
        log("no standalone folder to wrap — skipping the AppImage.")
        return None

    app_dir = BUILD_DIR / f"{APP_NAME}.AppDir"
    if app_dir.exists():
        shutil.rmtree(app_dir)
    (app_dir / "usr").mkdir(parents=True)

    shutil.copytree(folder, app_dir / "usr" / "bin")
    (app_dir / "usr" / "bin" / EXE_NAME).chmod(0o755)

    (app_dir / f"{EXE_NAME}.desktop").write_text(DESKTOP_ENTRY, encoding="utf-8")
    apprun = app_dir / "AppRun"
    apprun.write_text(APPRUN, encoding="utf-8")
    apprun.chmod(0o755)
    shutil.copy2(ICON_PNG, app_dir / "svgsnake.png")

    out = OUT_DIR / f"{APP_NAME}-{APP_VERSION}-{ARCH_TAG}.AppImage"
    if out.exists():
        out.unlink()

    log("building the AppImage…")
    env = dict(os.environ)
    # Containers rarely have FUSE, and appimagetool is itself an AppImage.
    env["APPIMAGE_EXTRACT_AND_RUN"] = "1"
    env.setdefault("ARCH", platform.machine())
    result = subprocess.run([tool, str(app_dir), str(out)], cwd=ROOT, env=env)
    if result.returncode != 0:
        fail(f"appimagetool exited with code {result.returncode}")

    if out.exists():
        out.chmod(0o755)
        log(f"installer: {out}  ({out.stat().st_size / 1_048_576:.0f} MB)")
        return out
    log("appimagetool finished, but the expected file name was not produced.")
    return None


def build_installer() -> Path | None:
    return build_installer_windows() if IS_WINDOWS else build_appimage()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"Build {APP_NAME} for {PLATFORM_TAG}-{ARCH_TAG}."
    )
    parser.add_argument("--clean", action="store_true",
                        help="remove this platform's build/ and dist/ subtree first")
    parser.add_argument("--onefile", action="store_true",
                        help="produce one portable binary (slower to start)")
    parser.add_argument("--no-installer", action="store_true",
                        help="compile only, do not build the installer/AppImage")
    parser.add_argument("--no-archive", action="store_true",
                        help="do not build the portable archive")
    parser.add_argument("--archive-format", choices=sorted(ARCHIVE_SUFFIX),
                        default=DEFAULT_ARCHIVE_FORMAT,
                        help=f"portable archive format (default: {DEFAULT_ARCHIVE_FORMAT})")
    parser.add_argument("--installer-only", action="store_true",
                        help="skip compilation and only rebuild the installer")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 4,
                        help="parallel compile jobs (default: all cores)")
    args = parser.parse_args()

    check_environment()

    if args.clean:
        for path in (BUILD_DIR, OUT_DIR):
            if path.exists():
                log(f"removing {path}")
                shutil.rmtree(path, ignore_errors=True)

    if not args.installer_only:
        compile_app(onefile=args.onefile, jobs=args.jobs)

    if args.onefile and not args.installer_only:
        log("onefile build done — the binary is portable, no packaging needed.")
        return

    if not args.no_archive:
        build_archive(args.archive_format)

    if not args.no_installer:
        build_installer()

    log("done.")


if __name__ == "__main__":
    main()
