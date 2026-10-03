#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

SOURCE = Path(__file__).resolve()
RESOURCES = SOURCE.parent / "resources"
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
DATA_DIR = DATA_HOME / "lazertweaks"
BIN_DIR = Path(os.environ.get("BINDIR") or Path.home() / ".local/bin")
COMMAND = BIN_DIR / "lazer-tweaks"
APPIMAGE = DATA_DIR / "osu.AppImage"
CONFIG = DATA_DIR / "config.cfg"
DESKTOP = DATA_HOME / "applications/lazertweaks.desktop"
GITHUB_API = "https://api.github.com/repos/ppy/osu/releases"
DOWNLOAD_CHUNK = 1024 * 1024

def message(text: str) -> None:
    print(f"[lazer-tweaks] {text}", flush=True)

def open_url(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "lazer-tweaks"})
    return urllib.request.urlopen(request, timeout=60)

def download(asset: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=DATA_DIR) as directory:
        temporary = Path(directory) / "osu.AppImage"
        digest = hashlib.sha256()
        with open_url(asset["browser_download_url"]) as response, temporary.open("wb") as output:
            while chunk := response.read(DOWNLOAD_CHUNK):
                output.write(chunk)
                digest.update(chunk)

        if temporary.stat().st_size != asset["size"] or f"sha256:{digest.hexdigest()}" != asset["digest"]:
            raise ValueError("AppImage integrity check failed. The current file was kept.")

        temporary.chmod(0o755)
        if APPIMAGE.exists():
            shutil.copy2(APPIMAGE, APPIMAGE.with_suffix(".AppImage.bak"))

        temporary.replace(APPIMAGE)

def detect_channel() -> str:
    settings = DATA_HOME / "osu/game.ini"
    if settings.exists():
        for line in settings.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "ReleaseStream":
                channel = value.strip().lower()
                if channel in ("tachyon", "lazer"):
                    return channel

    raise ValueError("Could not detect the release channel. Use --tachyon or --normal.")

def download_osu(channel: str) -> None:
    message(f"Detecting latest {channel} release...")
    with open_url(f"{GITHUB_API}?per_page=100") as response:
        releases = json.load(response)

    for release in releases:
        if release["draft"] or not release["tag_name"].endswith(f"-{channel}"):
            continue

        for asset in release["assets"]:
            if asset["name"] == "osu.AppImage":
                message(f"Downloading osu! {release['tag_name']}...")
                download(asset)
                return

    raise ValueError(f"Could not find a {channel} AppImage.")

def install_link() -> None:
    BIN_DIR.mkdir(parents=True, exist_ok=True)

    if COMMAND.is_symlink():
        if COMMAND.resolve() == SOURCE:
            return
        COMMAND.unlink()
    elif COMMAND.exists():
        raise ValueError(f"{COMMAND} already exists and is not a symlink.")

    SOURCE.chmod(SOURCE.stat().st_mode | 0o111)
    COMMAND.symlink_to(SOURCE)

def install(channel: str | None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if channel or not APPIMAGE.exists():
        download_osu(channel or "lazer")

    for name in ("config.cfg", "osu.png"):
        target = DATA_DIR / name
        if not target.exists():
            shutil.copy2(RESOURCES / name, target)

    install_link()
    DESKTOP.parent.mkdir(parents=True, exist_ok=True)

    desktop = (RESOURCES / "lazer-tweaks.desktop").read_text()

    # resolve desktop file
    DESKTOP.write_text(desktop.replace("@COMMAND@", str(COMMAND)).replace("@ICON@", str(DATA_DIR / "osu.png")))
    message("Installed.")

def read_config() -> dict[str, str]:
    """source shell assignments and expansions, then pass the exported environment to osu!."""
    env = os.environ.copy()
    env["XDG_DATA_HOME"] = str(DATA_HOME)
    env["BINDIR"] = str(BIN_DIR)
    env.setdefault("EDITOR", "nano")
    env.setdefault("OSU_TEMP_TESTING_BASS_CONFIG_DEV_PERIOD", "-128")
    env.setdefault("SDL_VIDEO_DOUBLE_BUFFER", "1")

    if env.get("XDG_SESSION_TYPE") == "wayland" or env.get("WAYLAND_DISPLAY"):
        env.setdefault("SDL_VIDEODRIVER", "wayland")

    if CONFIG.exists():
        result = subprocess.run(
            ["bash", "-c", 'set -a; source "$1" >/dev/null && env -0', "bash", str(CONFIG)],
            env=env, capture_output=True, check=False
        )
        if result.returncode:
            raise ValueError(f"Could not load {CONFIG}: {os.fsdecode(result.stderr).strip()}")

        env.clear()
        for entry in result.stdout.split(b"\0"):
            key, separator, value = entry.partition(b"=")
            if separator:
                env[os.fsdecode(key)] = os.fsdecode(value)

    return env

def launch(arguments: list[str]) -> None:
    if not APPIMAGE.exists():
        install(None)
    else:
        install_link()

    env = read_config()
    # wait a bit for the window before setting the name
    if shutil.which("xdotool") and shutil.which("xprop"):
        subprocess.Popen(
            ["sh", "-c", "sleep 3; for wid in $(xdotool search --all --class 'osu!'); do xprop -id \"$wid\" -f WM_NAME 8s -set WM_NAME 'osu!'; done"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )

    command = shlex.split(env.get("PRE_LAUNCH_ARGS", "")) + [str(APPIMAGE)] + arguments
    message("Launching osu!lazer...")
    os.execvpe(command[0], command, env)

def remove() -> None:
    if input(f"Remove {DATA_DIR} and {COMMAND}? (y/N): ").lower() != "y":
        message("Cancelled.")
        return

    if COMMAND.exists() and not COMMAND.is_symlink():
        raise ValueError(f"{COMMAND} is not a symlink. Refusing to remove it.")

    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)

    COMMAND.unlink(missing_ok=True)
    DESKTOP.unlink(missing_ok=True)
    message("Removed lazer-tweaks.")

def main() -> None:
    parser = argparse.ArgumentParser(description="Install, launch and repair osu!lazer.")

    actions = parser.add_mutually_exclusive_group()
    for action in ("install", "repair", "config", "update", "remove"):
        actions.add_argument(f"--{action}", dest="action", action="store_const", const=action)

    channels = parser.add_mutually_exclusive_group()
    channels.add_argument("--tachyon", dest="channel", action="store_const", const="tachyon")
    channels.add_argument("--normal", dest="channel", action="store_const", const="lazer")

    parser.add_argument("arguments", nargs=argparse.REMAINDER, help="arguments forwarded to osu!lazer")
    args = parser.parse_args()

    if args.channel and args.action not in ("install", "repair"):
        parser.error("--tachyon and --normal require --install or --repair")
    if args.arguments and args.action:
        parser.error("game arguments can only be used when launching")

    if args.action == "install":
        install(args.channel)
    elif args.action == "repair":
        download_osu(args.channel or detect_channel())
        message("Repaired.")
    elif args.action == "config":
        env = read_config()
        command = shlex.split(env["EDITOR"]) + [str(CONFIG)]
        os.execvpe(command[0], command, env)
    elif args.action == "update":
        subprocess.run(["git", "-C", str(SOURCE.parent), "pull", "--ff-only"], check=True)
    elif args.action == "remove":
        remove()
    else:
        arguments = args.arguments
        if arguments[:1] == ["--"]:
            arguments = arguments[1:]
        launch(arguments)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[lazer-tweaks] Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
