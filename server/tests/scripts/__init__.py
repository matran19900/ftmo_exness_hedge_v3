"""Test package for the operational CLI under ``scripts/``.

The CLI lives at the repository root (``/scripts/init_account.py``), one
level above the ``server/`` Python package that pytest treats as its
root. Adding the repo root to ``sys.path`` here makes ``from scripts
import init_account`` resolve from this test subpackage without
modifying ``pyproject.toml`` or the global conftest.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
