"""
SVGPaw — Windows build script
=============================
Compiles the app to a native Windows program with **Nuitka** and, when Inno
Setup is available, wraps the result in an installer.

Nuitka translates the Python sources to C and hands them to a real optimizing
compiler, which is what makes this worth doing over a bytecode bundler: the
interpreter loop disappears from the hot paths, and with ``--lto`` the compiler
inlines across module boundaries.

Usage::

    python build.py              # compile, then build the installer if possible
    python build.py --no-installer
    python build.py --clean      # discard previous build artefacts first
    python build.py --onefile    # single portable .exe instead of a folder

The default is a *standalone folder* rather than ``--onefile``: onefile has to
unpack itself into a temp directory on every launch, which costs a second or two
of start-up. The installer puts the folder in Program Files, so the unpacking
never has to happen.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import svgpaw  # noqa: E402  — for the single source of truth on name/version

APP_NAME = svgpaw.APP_NAME
APP_VERSION = svgpaw.APP_VERSION
APP_SLOGAN = svgpaw.APP_SLOGAN
APP_PUBLISHER = svgpaw.APP_PUBLISHER

ENTRY = ROOT / "svgpaw.py"
ICON = ROOT / "icon" / "icon.ico"
BUILD_DIR = ROOT / "build"
DIST_DIR = ROOT / "dist"
INSTALLER_DIR = ROOT / "installer"
ISS = INSTALLER_DIR / "svgpaw.iss"

#: The renderer that is actually used. PyMuPDF is only a fallback in the source
#: tree and would add ~100 MB of binaries for a code path resvg never reaches,
#: so it is cut from the build; the import sits in a try/except and copes.
EXCLUDED_MODULES = [
    "pymupdf",
    "fitz",
    "pytest",
    "unittest",
    "setuptools",
    "pip",
    "numpy",
    "matplotlib",
]

#: tkinterdnd2 ships the tkdnd Tcl extension for every platform it supports.
#: Windows on Tcl 8.6 is the only one this build can use.
UNUSED_TKDND = [
    "tkinterdnd2/tkdnd/linux-*/**",
    "tkinterdnd2/tkdnd/osx-*/**",
    "tkinterdnd2/tkdnd/win-x86/**",
    "tkinterdnd2/tkdnd/win-arm64/**",
    "tkinterdnd2/tkdnd/*-tcl9/**",
]


def log(message: str) -> None:
    print(f"[build] {message}", flush=True)


def fail(message: str) -> "None":
    print(f"[build] ERROR: {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def check_environment() -> None:
    if sys.platform != "win32":
        fail("this script builds the Windows package and must run on Windows.")
    if not ENTRY.exists():
        fail(f"entry point not found: {ENTRY}")
    if not ICON.exists():
        fail(f"icon not found: {ICON}")
    try:
        import nuitka  # noqa: F401
    except ImportError:
        fail("Nuitka is not installed. Run: pip install nuitka ordered-set zstandard")

    missing = []
    for module in ("customtkinter", "PIL", "tkinterdnd2", "resvg_py", "svgo"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        fail(
            "missing runtime dependencies: "
            + ", ".join(missing)
            + "\n        Run: pip install -r requirements.txt"
        )


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


def nuitka_command(onefile: bool, use_msvc: bool, jobs: int) -> list[str]:
    cmd = [
        sys.executable, "-m", "nuitka",

        # -- packaging mode ------------------------------------------------- #
        "--standalone",
        "--assume-yes-for-downloads",
        f"--output-dir={BUILD_DIR}",
        "--output-filename=SVGPaw.exe",
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

        # -- Windows metadata ----------------------------------------------- #
        "--windows-console-mode=disable",
        f"--windows-icon-from-ico={ICON}",
        f"--company-name={APP_PUBLISHER}",
        f"--product-name={APP_NAME}",
        f"--file-version={APP_VERSION}",
        f"--product-version={APP_VERSION}",
        f"--file-description={APP_NAME} — {APP_SLOGAN}",
        f"--copyright=© {time.strftime('%Y')} {APP_PUBLISHER}",
    ]

    if onefile:
        cmd += ["--onefile", "--onefile-tempdir-spec={CACHE_DIR}/SVGPaw/{VERSION}"]
    if use_msvc:
        cmd.append("--msvc=latest")
    else:
        cmd.append("--mingw64")

    cmd += [f"--nofollow-import-to={name}" for name in EXCLUDED_MODULES]
    cmd += [f"--noinclude-data-files={pattern}" for pattern in UNUSED_TKDND]
    cmd.append(str(ENTRY))
    return cmd


def compile_app(onefile: bool, jobs: int) -> Path:
    use_msvc = find_msvc()
    log(f"compiler: {'MSVC (Visual Studio)' if use_msvc else 'MinGW64 (Nuitka-managed)'}")
    log(f"mode: {'onefile' if onefile else 'standalone folder'}, jobs: {jobs}")
    log("compiling — this takes several minutes on a first run…")

    started = time.time()
    cmd = nuitka_command(onefile, use_msvc, jobs)
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        fail(f"Nuitka exited with code {result.returncode}")
    log(f"compiled in {time.time() - started:.0f}s")

    produced = BUILD_DIR / ("svgpaw.dist" if not onefile else "")
    exe = (produced / "SVGPaw.exe") if not onefile else (BUILD_DIR / "SVGPaw.exe")
    if not exe.exists():
        fail(f"expected binary not found: {exe}")

    DIST_DIR.mkdir(parents=True, exist_ok=True)
    if onefile:
        target = DIST_DIR / "SVGPaw.exe"
        shutil.copy2(exe, target)
    else:
        target_dir = DIST_DIR / "SVGPaw"
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(produced, target_dir)
        target = target_dir / "SVGPaw.exe"

    # Measure what was just produced, not everything sitting in dist/ — an
    # installer from an earlier run lives there too.
    measured = target.parent if not onefile else target
    size = (
        sum(f.stat().st_size for f in measured.rglob("*") if f.is_file())
        if measured.is_dir() else measured.stat().st_size
    )
    log(f"output: {target}  ({size / 1_048_576:.0f} MB)")
    return target


def build_installer() -> Path | None:
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
        [str(iscc), f"/DAppVersion={APP_VERSION}", str(ISS)], cwd=ROOT
    )
    if result.returncode != 0:
        fail(f"ISCC exited with code {result.returncode}")

    setup = DIST_DIR / f"SVGPaw-{APP_VERSION}-Setup.exe"
    if setup.exists():
        log(f"installer: {setup}  ({setup.stat().st_size / 1_048_576:.0f} MB)")
        return setup
    log("installer built, but the expected file name was not produced.")
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=f"Build {APP_NAME} for Windows.")
    parser.add_argument("--clean", action="store_true",
                        help="remove build/ and dist/ before compiling")
    parser.add_argument("--onefile", action="store_true",
                        help="produce one portable .exe (slower to start)")
    parser.add_argument("--no-installer", action="store_true",
                        help="compile only, do not run Inno Setup")
    parser.add_argument("--installer-only", action="store_true",
                        help="skip compilation and only rebuild the installer")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 4,
                        help="parallel compile jobs (default: all cores)")
    args = parser.parse_args()

    check_environment()

    if args.clean:
        for path in (BUILD_DIR, DIST_DIR):
            if path.exists():
                log(f"removing {path}")
                shutil.rmtree(path, ignore_errors=True)

    if not args.installer_only:
        compile_app(onefile=args.onefile, jobs=args.jobs)

    if args.onefile and not args.installer_only:
        log("onefile build done — the .exe is portable, no installer needed.")
        return

    if not args.no_installer:
        build_installer()

    log("done.")


if __name__ == "__main__":
    main()
