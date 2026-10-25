"""模型家族确定性推导（reviewer 身份契约 F1）。

从模型名推导 model_family——**不信任调用方直接声明的 family**：
响应里回填的 model_family 必须与 derive_family(reviewer_model) 一致，
不一致即"伪造 family"，身份无效（fail-closed）。

支持已知家族：OpenAI / Anthropic / Google / DeepSeek / Moonshot / Qwen /
Zhipu / MiniMax / xAI / Meta；无法唯一识别时返回 "unknown"。

纯标准库；无网络；无副作用。
"""

from __future__ import annotations

# 家族匹配表：按子串匹配（大小写不敏感）。顺序无关，但一个名字命中多个
# 家族时视为歧义 → unknown（无法唯一识别就不识别）。
_FAMILY_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("openai", ("gpt-", "gpt", "o1", "o3", "o4-mini", "openai", "chatgpt", "codex")),
    ("anthropic", ("claude", "anthropic")),
    ("google", ("gemini", "gemma", "google", "palm", "bard")),
    ("deepseek", ("deepseek",)),
    ("moonshot", ("kimi", "moonshot")),
    ("qwen", ("qwen", "qwen2", "qwq", "tongyi")),
    ("zhipu", ("glm", "chatglm", "zhipu", "codegeex")),
    ("minimax", ("minimax", "abab")),
    ("xai", ("grok", "xai")),
    ("meta", ("llama", "meta-llama")),
)

# glm 会误伤 "gpt"？ 不会：gpt 无 "glm" 子串。但 "aglm" 之类脏数据由歧义规则兜底。
_KNOWN_FAMILIES = frozenset(family for family, _ in _FAMILY_PATTERNS)


def derive_family(model_name: str) -> str:
    """从模型名确定性推导家族；无法唯一识别时返回 "unknown"。

    空名 / 空白 → "unknown"。命中多个家族 → "unknown"（歧义即不识别）。
    """
    text = str(model_name or "").strip().lower()
    if not text:
        return "unknown"
    hits = {
        family
        for family, patterns in _FAMILY_PATTERNS
        if any(pattern in text for pattern in patterns)
    }
    if len(hits) == 1:
        return next(iter(hits))
    return "unknown"


def is_known_family(family: str) -> bool:
    """是否为已知家族（unknown 不算已知）。"""
    return str(family or "").strip().lower() in _KNOWN_FAMILIES
