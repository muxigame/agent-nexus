"""Make the frozen legacy package importable as ``legacy_mcpm``.

``legacy_mcpm`` is the baseline ``mcpm`` package (commit f343399) frozen byte
for byte, except for the scrubs listed in ``LEGACY_PROVENANCE.json``. It plays
the "old gateway" and "old agent" in the compatibility matrix (design §3.3),
so no live deployment is ever needed.
"""
import sys
from pathlib import Path

COMPAT = Path(__file__).resolve().parent
if str(COMPAT) not in sys.path:
    sys.path.insert(0, str(COMPAT))
