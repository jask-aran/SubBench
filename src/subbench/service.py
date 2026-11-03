"""Install the collector as a background service: a systemd user unit on Linux, a launchd
agent on macOS. The service runs this same executable with `watch`, so there is nothing
else to install and nothing to keep in step.
"""
from __future__ import annotations

import os
import platform
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import config

UNIT_NAME = "subbench.service"
LAUNCHD_LABEL = "com.subbench.collector"


def executable() -> list[str]:
    """How to start this program again: the single binary itself, or the installed command."""
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve())]
    found = shutil.which("subbench")
    if found:
        return [str(Path(found).resolve())]
    return [str(Path(sys.executable).resolve()), "-m", "subbench"]


def unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base).expanduser() / "systemd" / "user" / UNIT_NAME


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def log_path() -> Path:
    return Path.home() / "Library" / "Logs" / "subbench.log"


def systemd_unit(command: list[str]) -> str:
    # The collector reads the configuration file itself; EnvironmentFile is kept so a value
    # set there also reaches anything the collector starts (the optional codex fallback).
    return f"""[Unit]
Description=SubBench subscription usage collector
After=network-online.target

[Service]
Type=simple
Environment=PATH={Path.home()}/.local/bin:/home/linuxbrew/.linuxbrew/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=-{config.config_path()}
ExecStart={shlex.join(command)}
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
"""


def launchd_plist(command: list[str]) -> bytes:
    return plistlib.dumps({
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": command,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "EnvironmentVariables": {"PATH": f"{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        "StandardOutPath": str(log_path()),
        "StandardErrorPath": str(log_path()),
    })


def _replace(path: Path, data: bytes) -> None:
    # A unit installed by hand is often a symlink into the checkout. Writing through it would
    # overwrite the tracked file in the repository, so remove the link and write a new file.
    if path.is_symlink():
        path.unlink()
    path.write_bytes(data)


def _system() -> str:
    name = platform.system()
    if name not in ("Linux", "Darwin"):
        raise SystemExit(f"`subbench service` supports Linux (systemd) and macOS (launchd), not {name}. "
                         "Run `subbench watch` under your own supervisor instead.")
    return name


def installed() -> bool:
    return unit_path().exists() or plist_path().exists()


def _run(*command: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(command), check=False, capture_output=True, text=True)


def install(provider: str, *, dry_run: bool = False) -> int:
    system = _system()
    command = [*executable(), "watch", "--provider", provider]
    if system == "Linux":
        text = systemd_unit(command)
        if dry_run:
            print(f"# {unit_path()}\n{text}")
            return 0
        if not shutil.which("systemctl"):
            raise SystemExit("systemctl not found. Run `subbench watch` under your own supervisor instead.")
        unit_path().parent.mkdir(parents=True, exist_ok=True)
        _replace(unit_path(), text.encode())
        _run("systemctl", "--user", "daemon-reload")
        result = _run("systemctl", "--user", "enable", UNIT_NAME)
        restart = _run("systemctl", "--user", "restart", UNIT_NAME)
        if result.returncode or restart.returncode:
            print((result.stderr or restart.stderr).strip(), file=sys.stderr)
            return 1
        print(f"Installed {unit_path()} and started it.")
        if _run("loginctl", "show-user", os.environ.get("USER", ""), "--property=Linger").stdout.strip() == "Linger=no":
            print("It stops when you log out. To keep it running: loginctl enable-linger")
        print("`subbench service logs` shows what it is doing.")
        return 0
    data = launchd_plist(command)
    if dry_run:
        print(f"# {plist_path()}\n{data.decode()}")
        return 0
    domain = f"gui/{os.getuid()}"
    plist_path().parent.mkdir(parents=True, exist_ok=True)
    log_path().parent.mkdir(parents=True, exist_ok=True)
    _run("launchctl", "bootout", domain, str(plist_path()))
    _replace(plist_path(), data)
    result = _run("launchctl", "bootstrap", domain, str(plist_path()))
    if result.returncode:
        print(result.stderr.strip(), file=sys.stderr)
        return 1
    print(f"Installed {plist_path()} and started it. `subbench service logs` shows what it is doing.")
    return 0


def uninstall() -> int:
    system = _system()
    if system == "Linux":
        if unit_path().exists():
            _run("systemctl", "--user", "disable", "--now", UNIT_NAME)
            unit_path().unlink()
            _run("systemctl", "--user", "daemon-reload")
            print(f"Stopped and removed {unit_path()}.")
        else:
            print("Not installed.")
        return 0
    if plist_path().exists():
        _run("launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path()))
        plist_path().unlink()
        print(f"Stopped and removed {plist_path()}.")
    else:
        print("Not installed.")
    return 0


def status() -> int:
    system = _system()
    if not installed():
        print("Not installed. `subbench service install` starts the collector at login.")
        return 1
    if system == "Linux":
        result = _run("systemctl", "--user", "status", "--no-pager", "--lines=0", UNIT_NAME)
        print(result.stdout.strip() or result.stderr.strip())
        return 0 if result.returncode == 0 else 1
    result = _run("launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}")
    state = next((line.strip() for line in result.stdout.splitlines() if line.strip().startswith("state =")), "state = unknown")
    print(f"{plist_path()}\n{state}")
    return 0 if result.returncode == 0 else 1


def logs() -> int:
    system = _system()
    if system == "Linux":
        return subprocess.call(["journalctl", "--user", "-u", UNIT_NAME, "-n", "60", "--no-pager"])
    try:
        lines = log_path().read_text().splitlines()[-60:]
    except OSError:
        print(f"No log yet at {log_path()}.")
        return 1
    print("\n".join(lines))
    return 0


def run(args) -> int:
    if args.service_command == "install":
        return install(args.provider, dry_run=args.dry_run)
    if args.service_command == "uninstall":
        return uninstall()
    if args.service_command == "status":
        return status()
    if args.service_command == "logs":
        return logs()
    raise AssertionError(args.service_command)
