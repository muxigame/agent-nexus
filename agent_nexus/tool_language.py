"""English presentation metadata for the current built-ins and connected MCP tools.

Only documentation fields are translated. Tool names, schemas, defaults,
enumerated API values, annotations, and runtime results retain their contracts.
Unknown descriptions are preserved rather than replaced with vague fallback text.
"""
from __future__ import annotations

import functools
import json
import logging
import os
from pathlib import Path
from typing import Any

from .device_contract import schema_path

#: Extra translation tables merged over the bundled one, separated by os.pathsep.
#: The bundled table covers public tools; entries for private plugins and a
#: deployment's own connectors live in a file kept beside that deployment.
EXTRA_ENV = "ANX_TOOL_METADATA_EXTRA"

log = logging.getLogger(__name__)

# These schema members hold actual values, not documentation. In particular,
# a default object may legitimately have a business field named "description".
_VALUE_FIELDS = frozenset({"default", "const", "enum", "examples", "example", "_meta", "meta"})


@functools.lru_cache(maxsize=1)
def translations() -> dict[str, str]:
    table = json.loads(schema_path("tool-metadata.en.json").read_text(encoding="utf-8"))
    for name in filter(None, os.environ.get(EXTRA_ENV, "").split(os.pathsep)):
        try:
            extra = json.loads(Path(name).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            log.warning("%s: cannot read %s: %s", EXTRA_ENV, name, exc)
            continue
        if isinstance(extra, dict):
            table.update({str(key): str(value) for key, value in extra.items()})
    return table


def english_metadata(value: Any) -> Any:
    """Copy a descriptor/schema, translating known titles and descriptions only."""
    if isinstance(value, list):
        return [english_metadata(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in ("title", "description") and isinstance(item, str):
            result[key] = translations().get(item, item)
        elif key in _VALUE_FIELDS:
            result[key] = item
        else:
            result[key] = english_metadata(item)
    return result
