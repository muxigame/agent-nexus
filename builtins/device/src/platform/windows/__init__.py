"""Windows: the common capabilities plus the screen, UIA and input extras.

The extras map onto the existing control tools (design §5.2 table): ``screen``
= list_screens/screenshot/vision, ``uia`` = start_control/stop_control/
control_status/read_ui/element, ``input`` = mouse/keyboard. UIA and input
need an interactive desktop; on a locked workstation, a signed-out or
disconnected remote session, or a service in session 0, ``probe()`` reports
them ``available: false`` with the reason.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from ..common import CAPABILITIES as COMMON, SCREEN_CAPABILITIES

CAPABILITIES: dict[str, dict[str, Any]] = {**COMMON, **SCREEN_CAPABILITIES}
NOTIFY_BACKEND = "windows-toast"
#: Toasts need a registered AppUserModelID; Windows PowerShell's is always there.
TOAST_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
_DESKTOP_SWITCHDESKTOP = 0x0100
_CREATE_NO_WINDOW = 0x08000000


def interactive_desktop() -> tuple[bool, str | None]:
    """Whether this process can see and drive the input desktop."""
    if sys.platform != "win32":
        return False, "not running on Windows"
    try:
        session = ctypes.c_ulong()
        if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)) and session.value == 0:
            return False, "running in session 0 (a service) without an interactive desktop"
        user32 = ctypes.windll.user32
        handle = user32.OpenInputDesktop(0, False, _DESKTOP_SWITCHDESKTOP)
        if not handle:
            return False, "no interactive desktop (the workstation is locked, signed out or a disconnected remote session)"
        user32.CloseDesktop(handle)
    except (AttributeError, OSError) as exc:
        return False, f"cannot query the input desktop ({type(exc).__name__})"
    return True, None


def probe() -> dict[str, dict[str, Any]]:
    available, reason = interactive_desktop()
    state = {"available": available, "reason": reason}
    return {"screen": dict(state), "uia": dict(state), "input": dict(state)}


def form() -> str:
    """``laptop`` when a system battery is present, else ``desktop``."""
    class PowerStatus(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
    try:
        status = PowerStatus()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            # 128 = no system battery, 255 = unknown.
            return "desktop" if status.BatteryFlag in (128, 255) else "laptop"
    except (AttributeError, OSError):
        pass
    return "desktop"


def toast_xml(title: str, body: str, level: str, actions: list[dict[str, Any]]) -> str:
    scenario = ' scenario="urgent"' if level == "urgent" else ""
    parts = [f"<toast{scenario}><visual><binding template=\"ToastGeneric\">",
             f"<text>{escape(title)}</text>", f"<text>{escape(body)}</text>" if body else "",
             "</binding></visual>"]
    if actions:
        parts.append("<actions>")
        for action in actions:
            link = action.get("link")
            if link:
                parts.append(f"<action content={quoteattr(action['label'])} arguments={quoteattr(link)} "
                             "activationType=\"protocol\"/>")
            else:
                parts.append(f"<action content={quoteattr(action['label'])} "
                             f"arguments={quoteattr('anx-action:' + action['id'])} activationType=\"background\"/>")
        parts.append("</actions>")
    parts.append("</toast>")
    return "".join(parts)


_TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
try {
  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
  [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
  $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
  $xml.LoadXml($env:ANX_TOAST_XML)
  $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:ANX_TOAST_APP).Show($toast)
  'toast'
} catch {
  Add-Type -AssemblyName System.Windows.Forms
  Add-Type -AssemblyName System.Drawing
  $icon = New-Object System.Windows.Forms.NotifyIcon
  $icon.Icon = [System.Drawing.SystemIcons]::Information
  $icon.Visible = $true
  $icon.ShowBalloonTip(8000, $env:ANX_TOAST_TITLE, $env:ANX_TOAST_BODY, [System.Windows.Forms.ToolTipIcon]::Info)
  Start-Sleep -Seconds 8
  $icon.Dispose()
  'balloon'
}
"""


def notify(title: str, body: str, level: str, actions: list[dict[str, Any]]) -> dict[str, Any]:
    """Show a Windows toast; fall back to a tray balloon when toasts are unavailable."""
    env = {**os.environ, "ANX_TOAST_XML": toast_xml(title, body, level, actions), "ANX_TOAST_APP": TOAST_APP_ID,
           "ANX_TOAST_TITLE": title, "ANX_TOAST_BODY": body or " "}
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _TOAST_SCRIPT],
        env=env, capture_output=True, text=True, timeout=30, creationflags=_CREATE_NO_WINDOW)
    shown = (completed.stdout or "").strip().splitlines()[-1:] or [""]
    if completed.returncode != 0 or shown[0] not in ("toast", "balloon"):
        raise RuntimeError("Windows notification failed: " + (completed.stderr or "").strip()[:300])
    return {"backend": "windows-toast" if shown[0] == "toast" else "windows-balloon"}
