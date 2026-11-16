"""Security policy: working-directory containment and command screening.

This layer is a *guard rail*, not a sandbox. A shell can reach the whole host
through a thousand paths (interpreters, `$(...)`, base64 payloads, ...). It
stops the obvious catastrophes and keeps the blast radius visible; real
isolation has to come from the OS (a container, a VM, or a dedicated user).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .settings import Config


class PolicyError(ValueError):
    """Raised when a request violates the configured policy."""


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.allowed


class Policy:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._deny: list[tuple[str, re.Pattern[str]]] = []
        for pattern in config.command_deny:
            try:
                self._deny.append((pattern, re.compile(pattern, re.IGNORECASE)))
            except re.error as exc:  # a bad pattern must not take the server down
                raise PolicyError(f"invalid command_deny pattern {pattern!r}: {exc}") from exc

    # ------------------------------------------------------------------
    # working directory
    # ------------------------------------------------------------------
    def resolve_cwd(self, cwd: str | None) -> Path:
        """Resolve ``cwd`` and prove it sits inside an allowed root."""
        roots = self.config.resolved_roots()
        raw = cwd or self.config.default_workdir or (str(roots[0]) if roots else str(Path.home()))

        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            base = Path(self.config.default_workdir).expanduser() if self.config.default_workdir else (roots[0] if roots else Path.cwd())
            candidate = base / candidate
        candidate = candidate.resolve()

        if not candidate.exists():
            raise PolicyError(f"working directory does not exist: {candidate}")
        if not candidate.is_dir():
            raise PolicyError(f"working directory is not a directory: {candidate}")

        if roots and not any(self._is_within(candidate, root) for root in roots):
            allowed = ", ".join(str(r) for r in roots)
            raise PolicyError(f"working directory {candidate} is outside the allowed roots: {allowed}")

        return candidate

    def resolve_path(self, path: str | None) -> Path:
        """Resolve a file path for the ``files`` tools and prove it sits inside an allowed root.

        Relative paths start from ``default_workdir`` (else the first allowed
        root, else the home directory). The path need not exist.
        """
        roots = self.config.resolved_roots()
        if not path:
            raise PolicyError("path is required")
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            base = (Path(self.config.default_workdir).expanduser() if self.config.default_workdir
                    else roots[0] if roots else Path.home())
            candidate = base / candidate
        candidate = candidate.resolve()
        if roots and not any(self._is_within(candidate, root) for root in roots):
            allowed = ", ".join(str(r) for r in roots)
            raise PolicyError(f"path {candidate} is outside the allowed roots: {allowed}")
        return candidate

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    # ------------------------------------------------------------------
    # command screening
    # ------------------------------------------------------------------
    def screen_command(self, command: str) -> PolicyDecision:
        if not command or not command.strip():
            return PolicyDecision(False, "empty command")

        if self.config.allow_dangerous:
            return PolicyDecision(True, "allow_dangerous is enabled; screening skipped")

        for pattern, compiled in self._deny:
            if compiled.search(command):
                return PolicyDecision(
                    False,
                    f"command rejected by deny rule {pattern!r}. "
                    "Set allow_dangerous=true in the device settings to override.",
                )

        return PolicyDecision(True)

    def check_command(self, command: str) -> None:
        decision = self.screen_command(command)
        if not decision.allowed:
            raise PolicyError(decision.reason)
