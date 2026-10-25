"""测试公共辅助。"""

from __future__ import annotations

import contextlib
import copy
import sys
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.skill_loader import load_skill  # noqa: E402


def skill_module(name: str):
    return load_skill(name)


@contextlib.contextmanager
def isolated_web_cache(cache_dir: str | Path) -> Iterator[None]:
    """把 web_fetch 磁盘缓存指向独立目录，测试间互不串味。

    背景：缓存目录来自 literature.yaml（全局 output/.litcache）。同进程内
    多个测试共享它时，先跑的测试会把响应写入缓存，后跑的测试即使换了
    假传输层也会命中旧缓存（cite-intent / refill 测试各踩过一次）。

    同时接管所有直接绑定 utils.config_loader.load_literature_config 的模块
    （含 skill_loader 按路径加载、不在 sys.modules 的 Skill 模块），
    退出时按引用身份精确恢复。
    """
    import utils.web_fetch as web_fetch
    from utils.config_loader import load_literature_config
    from utils.skill_loader import loaded_skills

    original_loader = load_literature_config
    config = copy.deepcopy(load_literature_config())
    config.setdefault("network", {})["cache_dir"] = str(cache_dir)
    patched: dict[str, object] = {}
    candidates = dict(sys.modules)
    for module in loaded_skills().values():
        candidates[f"skill:{id(module)}"] = module
    for name, module in candidates.items():
        if module is None:
            continue
        if getattr(module, "load_literature_config", None) is original_loader:
            module.load_literature_config = lambda: config  # type: ignore[assignment]
            patched[name] = original_loader
    try:
        yield
    finally:
        for name, loader in patched.items():
            module = candidates.get(name)
            if module is not None:
                module.load_literature_config = loader  # type: ignore[assignment]
