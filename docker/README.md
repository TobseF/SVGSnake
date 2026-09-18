# Building the Linux release from Windows

Nuitka cannot cross-compile: it embeds the interpreter and the native extension
modules of the machine it runs on. The container sidesteps that by *being* a
Linux machine — under Docker Desktop on Windows it runs on a real Linux kernel
via WSL2, so the compile is native, not emulated.

## Once

```bash
docker compose build build-linux
```

Pulls Ubuntu 22.04, adds Python 3.13, gcc, Tk and `appimagetool`, and installs
the project's wheels. Takes a few minutes; after that the layer is cached.

## Every time

```bash
docker compose run --rm build-linux
```

Artefacts land in `dist/linux-x64/`:

| File | What it is |
|---|---|
| `SVGSnake/` | the standalone folder |
| `SVGSnake-1.0.0-linux-x64-portable.tar.gz` | unpack and run, no install |
| `SVGSnake-1.0.0-x64.AppImage` | single file, `chmod +x` and double-click |

The Windows build writes to `dist/windows-x64/`, so running both leaves you with
one tree per platform and nothing overwritten.

## Options

The image's default command is `python build.py`; anything else replaces it:

```bash
docker compose run --rm build-linux python build.py --no-installer
```

```bash
docker compose run --rm build-linux python build.py --clean --archive-format zip
```

```bash
docker compose run --rm shell
```

The last one drops you into a bash prompt inside the container with the source
tree mounted at `/src` — useful when a build fails and you want to poke at it.

## Why Ubuntu 22.04 and not something current

glibc is forward compatible but never backward. A binary linked against glibc
2.35 (22.04) starts on every newer distro; one linked against 2.39 (24.04)
refuses to start on 22.04, which is supported into 2027. Building deliberately
old is what makes the AppImage portable.

If you ever need to go older still, the base image is the only line to change —
but 20.04 is out of standard support and deadsnakes no longer builds 3.13 for
it, so that would mean dropping to an older Python too.

## Caches

Nuitka's C output and `ccache` live in named volumes, not in the source tree.
A second build reuses them and is markedly faster. To start clean:

```bash
docker compose down -v
```

## A note for Linux hosts

On Windows, Docker Desktop maps file ownership for you and the artefacts are
yours. On a Linux host the container writes as root, and `dist/` ends up
root-owned. Add your ids to the run if that bites:

```bash
docker compose run --rm --user "$(id -u):$(id -g)" build-linux
```
