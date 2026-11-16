"""``anx device conformance <mcp-url>``: check a device MCP against ``anx.device/1`` (design §5.2 d).

Checks, in order:

1. the core tools exist (``device_info``, ``notify``, ``file_*``, ``curr_time``, ``sleep``);
2. every tool has annotations;
3. every tool carries ``_meta`` ``anx/capability`` and ``anx/contract = anx.device/1``;
4. ``device_info`` validates against ``schemas/device-contract.v1.json`` and declares the core capabilities;
5. each declared, available capability has all of its standard tools, tagged with that capability;
   each unavailable one gives a reason; no tool claims an undeclared capability;
6. ``notify(dry_run=true)`` -- with and without actions -- succeeds without showing anything.

Only stdlib, FastMCP and jsonschema are used, so the device plugin's own test
suite runs the same checks in process (CI runs them on windows and ubuntu).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any

from . import device_contract as contract

CORE_TOOLS = ("device_info", "notify", "file_list", "file_read", "file_write", "file_stat", "curr_time", "sleep")


@dataclass
class Report:
    checks: list[dict[str, Any]] = field(default_factory=list)
    device: dict | None = None

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})

    @property
    def ok(self) -> bool:
        return all(item["ok"] for item in self.checks)

    def failures(self) -> list[dict[str, Any]]:
        return [item for item in self.checks if not item["ok"]]

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": self.checks,
                "platform": (self.device or {}).get("platform"),
                "capabilities": sorted((self.device or {}).get("capabilities") or {})}


def _payload(result: Any) -> Any:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured.get("result", structured) if set(structured) == {"result"} else structured
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except ValueError:
                return text
    return None


async def check_client(client) -> Report:
    """Run every check through an entered FastMCP ``Client``."""
    report = Report()
    tools = {tool.name: tool.model_dump(mode="json", by_alias=True, exclude_none=True)
             for tool in await client.list_tools()}

    missing = [name for name in CORE_TOOLS if name not in tools]
    report.add("core tools present", not missing, "missing: " + ", ".join(missing) if missing else "")

    unannotated = sorted(name for name, tool in tools.items() if not tool.get("annotations"))
    report.add("every tool has annotations", not unannotated, ", ".join(unannotated))

    untagged = sorted(name for name, tool in tools.items() if not contract.is_internal(tool) and (
        not contract.tool_meta(tool).get(contract.META_CAPABILITY)
        or contract.tool_meta(tool).get(contract.META_CONTRACT) != contract.CONTRACT))
    report.add("every tool has _meta anx/capability and anx/contract", not untagged, ", ".join(untagged))

    device = None
    if "device_info" in tools:
        result = await client.call_tool("device_info", {}, raise_on_error=False)
        device = _payload(result)
        if getattr(result, "is_error", False) or not isinstance(device, dict):
            report.add("device_info returns a descriptor", False, "device_info failed")
            device = None
    report.device = device
    if device is not None:
        problems = contract.validate(device)
        report.add("device_info matches device-contract.v1.json", not problems, "; ".join(problems[:5]))
        declared = device.get("capabilities") if isinstance(device.get("capabilities"), dict) else {}
        absent = [name for name in contract.core_capabilities() if name not in declared]
        report.add("core capabilities declared", not absent, ", ".join(absent))
        report.add("device_info reports a hostname", isinstance(device.get("hostname"), str) and bool(device["hostname"]))

        table = contract.standard_tools()
        gaps, reasons = [], []
        for name, spec in declared.items():
            if not isinstance(spec, dict):
                continue
            if spec.get("available") is False:
                if not spec.get("reason"):
                    reasons.append(name)
                continue
            for tool in table.get(name, []):
                tagged = contract.tool_meta(tools.get(tool) or {}).get(contract.META_CAPABILITY)
                if tool not in tools or tagged != name:
                    gaps.append(f"{name}:{tool}")
        report.add("declared capabilities have their tools", not gaps, ", ".join(gaps))
        report.add("unavailable capabilities give a reason", not reasons, ", ".join(reasons))
        claimed = sorted({contract.tool_meta(tool).get(contract.META_CAPABILITY) for tool in tools.values()
                          if not contract.is_internal(tool)} - set(declared) - {None})
        report.add("tools claim only declared capabilities", not claimed, ", ".join(claimed))

    if "notify" in tools:
        for label, arguments in (("notify(dry_run=true)", {"title": "conformance", "body": "dry run", "dry_run": True}),
                                 ("notify(actions, dry_run=true)",
                                  {"title": "conformance", "body": "dry run", "level": "urgent", "dry_run": True,
                                   "actions": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}]})):
            result = await client.call_tool("notify", arguments, raise_on_error=False)
            payload = _payload(result)
            good = (not getattr(result, "is_error", False) and isinstance(payload, dict)
                    and payload.get("status") == "ok" and payload.get("shown") is False)
            report.add(label, good, "" if good else json.dumps(payload, ensure_ascii=False)[:300])
    return report


async def check_url(url: str, token: str | None = None, timeout: float = 30.0) -> Report:
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    async with Client(StreamableHttpTransport(url, auth=token), timeout=timeout) as client:
        return await check_client(client)


def print_report(report: Report, stream=None) -> None:
    stream = stream or sys.stdout
    for item in report.checks:
        mark = "ok  " if item["ok"] else "FAIL"
        detail = f" -- {item['detail']}" if item["detail"] else ""
        print(f"  {mark} {item['check']}{detail}", file=stream)
    platform = (report.device or {}).get("platform") or "?"
    print(f"anx.device/1 conformance: {'passed' if report.ok else 'FAILED'} (platform={platform})", file=stream)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("url", help="the device MCP endpoint, e.g. http://127.0.0.1:8400/mcp")
    parser.add_argument("--token-env", help="environment variable holding a bearer token")
    parser.add_argument("--token-file", help="file holding a bearer token")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")


def run(args: argparse.Namespace) -> int:
    token = None
    if args.token_env:
        token = os.environ.get(args.token_env)
        if not token:
            print(f"conformance: environment variable {args.token_env} is not set", file=sys.stderr)
            return 2
    elif args.token_file:
        with open(args.token_file, encoding="utf-8-sig") as handle:
            token = handle.read().strip()
    try:
        report = asyncio.run(check_url(args.url, token))
    except Exception as exc:  # unreachable endpoint, refused auth, protocol error
        print(f"conformance: cannot check {args.url} ({type(exc).__name__})", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print_report(report)
    return 0 if report.ok else 1
