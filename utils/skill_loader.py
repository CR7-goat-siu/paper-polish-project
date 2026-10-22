"""Skill 目录装载器。

项目约定：skills/<skill-name>/main.py 暴露 run(args: dict) -> dict，
该函数是 Skill 之间（含 batch-processor）唯一的编程调用契约。
"""

from __future__ import annotations

import importlib.util
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any

from utils.config_loader import PROJECT_ROOT

SKILLS_DIR = PROJECT_ROOT / "skills"


class SkillNotFoundError(FileNotFoundError):
    pass


def skill_path(skill_name: str) -> Path:
    path = SKILLS_DIR / skill_name / "main.py"
    if not path.exists():
        raise SkillNotFoundError(f"Skill 不存在或缺少 main.py: {skill_name}")
    return path


@lru_cache(maxsize=None)
def load_skill(skill_name: str) -> ModuleType:
    path = skill_path(skill_name)
    module_name = f"paper_polish_skill_{skill_name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise SkillNotFoundError(f"无法加载 Skill: {skill_name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_skill(skill_name: str, args: dict[str, Any]) -> dict[str, Any]:
    module = load_skill(skill_name)
    if not hasattr(module, "run"):
        raise SkillNotFoundError(f"Skill {skill_name} 未实现 run(args) 契约")
    return module.run(args)
