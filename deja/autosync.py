"""A launchd agent that runs `deja sync` every day, so Chrome's pruning loses nothing."""
from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path
from typing import Tuple

from deja.ingest import DB_PATH

LABEL = "local.deja.sync"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")
LAUNCHER = Path(__file__).resolve().parent.parent / "bin" / "deja"
LOG_PATH = DB_PATH.parent / "sync.log"
HOUR = 12  # if the Mac is asleep at noon, launchd runs the job when it wakes


def plist(launcher: Path = LAUNCHER, log: Path = LOG_PATH) -> dict:
    return {
        "Label": LABEL,
        # Stamp each run with the time, then sync.
        "ProgramArguments": [
            "/bin/sh", "-c", 'date "+%Y-%m-%d %H:%M:%S"; exec "$0" sync', str(launcher),
        ],
        "StartCalendarInterval": {"Hour": HOUR, "Minute": 0},
        "RunAtLoad": True,  # also at login, in case the Mac was off at noon
        "ProcessType": "Background",
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl"] + list(args), capture_output=True, text=True)


def _domain() -> str:
    return "gui/%d" % os.getuid()


def turn_on() -> None:
    LOG_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PLIST_PATH, "wb") as file:
        plistlib.dump(plist(), file)
    _launchctl("bootout", _domain() + "/" + LABEL)  # replace a copy that is already loaded
    result = _launchctl("bootstrap", _domain(), str(PLIST_PATH))
    if result.returncode != 0:
        raise RuntimeError("launchctl could not load the agent: " + result.stderr.strip())


def turn_off() -> None:
    _launchctl("bootout", _domain() + "/" + LABEL)
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()


def status() -> Tuple[bool, str]:
    """(is the agent loaded, the last lines of its log)."""
    loaded = _launchctl("print", _domain() + "/" + LABEL).returncode == 0
    try:
        tail = "\n".join(LOG_PATH.read_text(errors="replace").splitlines()[-6:])
    except FileNotFoundError:
        tail = ""
    return loaded, tail
