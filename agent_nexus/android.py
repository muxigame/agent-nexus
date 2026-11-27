"""Android (``sys.platform == "android"``, CPython 3.13+): plugins start without uv.

The Android app (``android/``) carries one CPython build and every project's packages
already installed: the manager's on its own ``PYTHONPATH``, a plugin's in ``<plugin>/.site``
(``scripts/build_android.py``). There is no uv and nothing may be downloaded or built on the
device, so a plugin's ``uv run [flags] python ...`` runs that interpreter directly with the
plugin's own packages. ``sys.executable`` is the app's launcher (``libpyexe.so`` in the APK's
native library folder, the one place Android lets an app execute files from).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ANDROID = sys.platform == "android"
#: A plugin's preinstalled packages, relative to its working directory.
SITE = ".site"
#: ``uv run`` options that take a value (they select what uv installs; nothing is installed here).
VALUE_FLAGS = {"--python", "--project", "--directory", "--with", "--package", "--extra", "--group",
               "--only-group", "--no-group", "--env-file"}


def device_name() -> str | None:
    """The phone's or headset's own name (the app passes the user-set device name, else maker and
    model): on Android ``platform.node()`` is always ``localhost``."""
    if not ANDROID:
        return None
    return os.environ.get("ANX_ANDROID_DEVICE_NAME", "").strip()[:255] or None


def plugin_command(argv: list[str], cwd: Path, env: dict[str, str],
                   executable: str | None = None) -> tuple[list[str], dict[str, str]]:
    """``uv run [flags] python <args>`` -> ``<executable> <args>`` with ``<cwd>/.site`` first on the
    path. Anything else is returned unchanged."""
    if len(argv) < 3 or Path(argv[0]).stem != "uv" or argv[1] != "run":
        return argv, env
    rest = list(argv[2:])
    while rest and rest[0].startswith("-"):
        flag = rest.pop(0)
        if flag in VALUE_FLAGS and rest:
            rest.pop(0)
    if not rest or rest[0] not in ("python", "python3"):
        return argv, env
    env = dict(env)
    # Replace, not extend: the manager's own packages (other versions) must not leak in.
    env["PYTHONPATH"] = os.pathsep.join([str(Path(cwd) / SITE), str(cwd)])
    return [executable or sys.executable, *rest[1:]], env
