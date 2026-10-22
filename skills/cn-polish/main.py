"""cn-polish Skill：中文论文润色（build / apply 两阶段，纯离线）。

实现统一收敛于 utils/polish_engine.py；本文件仅保留语言侧常量与 run(args) 入口。"""

from __future__ import annotations

from types import SimpleNamespace
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.polish_engine import create_skill  # noqa: E402

CONFIG = {
    "skill_name": "cn-polish",
    "language": "cn",
    "term_language": "cn",
    "none_feedback": '（无）',
    "description": "中文论文润色（build / apply 两阶段，纯离线）。",
}

_IMPL = create_skill(CONFIG)

# 供测试与上层调用的公共入口（契约见 SKILL.md）
build_jobs = _IMPL.build_jobs
apply_responses = _IMPL.apply_responses
run = _IMPL.run
