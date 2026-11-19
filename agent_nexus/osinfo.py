"""Which operating system a device runs, for the console's device icons.

The client reports ``distro`` (``/etc/os-release`` ``ID`` on Linux); the gateway
folds what a device reported into one ``os_family`` the console draws an icon
for. Older clients report only ``platform`` and ``os``, so the family is also
guessed from those.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

#: The icons the console has (``static/ui/os-icons.js``).
FAMILIES = ("windows", "macos", "ios", "android", "ubuntu", "debian", "fedora", "arch", "linux")
#: ``/etc/os-release`` IDs (and ID_LIKE words) with an icon of their own.
DISTROS = {"ubuntu": "ubuntu", "kubuntu": "ubuntu", "xubuntu": "ubuntu", "pop": "ubuntu", "linuxmint": "ubuntu",
           "debian": "debian", "raspbian": "debian", "fedora": "fedora", "rhel": "fedora", "centos": "fedora",
           "rocky": "fedora", "almalinux": "fedora", "arch": "arch", "manjaro": "arch", "endeavouros": "arch"}
_ID = re.compile(r"[a-z0-9._-]{1,40}")


def os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    values = {}
    for line in text.splitlines()[:200]:
        key, sep, value = line.partition("=")
        if sep and key.strip().isidentifier():
            values[key.strip()] = value.strip().strip("\"'")[:200]
    return values


def distro() -> str | None:
    """This machine's distribution id (``ubuntu``, ``debian``…); ``None`` off Linux or when unknown."""
    if not sys.platform.startswith("linux"):
        return None
    found = os_release().get("ID", "").lower()
    return found if _ID.fullmatch(found) else None


def family(device: Any) -> str | None:
    """``os_family`` of a reported device block (``platform``, ``distro``, ``os``, ``form``)."""
    if not isinstance(device, dict):
        return None
    text = lambda key: str(device.get(key) or "").strip().lower()  # noqa: E731
    platform, name, form, os_text = text("platform"), text("distro"), text("form"), text("os")
    if platform in ("ios", "ipados", "iphone", "ipad"):
        return "ios"
    if platform == "android":
        return "android"
    if platform in ("windows", "win32") or os_text.startswith("windows"):
        return "windows"
    if platform in ("macos", "darwin", "mac", "osx"):
        return "macos"
    if form in ("phone", "mobile", "tablet") and "android" in os_text:
        return "android"
    if platform == "linux" or name or "linux" in os_text:
        if name in DISTROS:
            return DISTROS[name]
        for word, result in DISTROS.items():
            if word in os_text:
                return result
        if not name and re.search(r"^\d+\.\d+\.\d+-\d+-generic$", os_text):
            return "ubuntu"  # clients before 0.2.5 sent no distro; "-N-generic" kernels are Ubuntu's
        return "linux"
    return None
