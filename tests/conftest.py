"""测试公共辅助。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.skill_loader import load_skill  # noqa: E402


def skill_module(name: str):
    return load_skill(name)
