"""Prompt 模板渲染器。

模板正文必须位于 prompts/<lang>/ 下，占位符统一为 {{variable_name}}。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from utils.config_loader import PROJECT_ROOT, normalize_language

PROMPTS_DIR = PROJECT_ROOT / "prompts"
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class PromptTemplateError(ValueError):
    pass


def list_templates(language: str) -> list[str]:
    norm = normalize_language(language)
    folder = PROMPTS_DIR / norm
    if not folder.exists():
        return []
    return sorted(p.name for p in folder.iterdir() if p.is_file())


def _format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def render_template(
    template_name: str,
    language: str,
    *,
    extra: dict[str, Any] | None = None,
    missing: str = "raise",
    **kwargs: Any,
) -> str:
    """渲染 prompts/<language>/<template_name>。

    missing 取值：raise（默认，缺失变量抛错）或 keep（保留原占位符）。
    """
    norm = normalize_language(language)
    path = PROMPTS_DIR / norm / template_name
    if not path.exists():
        raise PromptTemplateError(f"模板不存在: {path}")
    template = path.read_text(encoding="utf-8")
    variables = {**(extra or {}), **kwargs}
    used_names = set(_PLACEHOLDER_RE.findall(template))
    missing_names = sorted(name for name in used_names if name not in variables)
    if missing_names:
        if missing == "raise":
            raise PromptTemplateError(
                f"模板 {template_name} 缺少变量: {', '.join(missing_names)}"
            )

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in variables:
            return _format_value(variables[name])
        return match.group(0)

    return _PLACEHOLDER_RE.sub(replace, template)


def render_rules_snapshot(
    rules_config: dict[str, Any],
    strength_key: str,
    domain_pack: dict[str, Any] | None = None,
) -> str:
    """从规则配置抽取“当前强度 + 领域规则”子集，供模板 rules 变量使用。

    domain_pack 传入时注入领域信息与领域级 section_guidance 覆盖。
    """
    strengths = rules_config.get("strengths", {})
    strength = strengths.get(strength_key, {})
    domain_section_guidance: dict[str, Any] = {}
    domain_info: dict[str, Any] | None = None
    if domain_pack:
        candidate = domain_pack.get("section_guidance") or {}
        if isinstance(candidate, dict):
            domain_section_guidance = candidate
        domain_info = {
            "id": domain_pack.get("id"),
            "name": domain_pack.get("name"),
            "description": domain_pack.get("description", ""),
            "terminology_hints": domain_pack.get("terminology_hints", ""),
        }
    snapshot = {
        "language": rules_config.get("language"),
        "domain": domain_info or rules_config.get("domain"),
        "strength": strength,
        "objectivity": rules_config.get("objectivity", {}),
        "coherence": rules_config.get("coherence", {}),
        "conciseness": rules_config.get("conciseness", {}),
        "domain_rules": rules_config.get("domain_rules", {}),
        "section_guidance": {**rules_config.get("section_guidance", {}), **domain_section_guidance},
        "output_constraints": rules_config.get("output_constraints", {}),
    }
    return json.dumps(snapshot, ensure_ascii=False, indent=2)


def render_terms_text(terms_data: dict[str, Any]) -> str:
    return json.dumps(terms_data, ensure_ascii=False, indent=2)


def render_term_corrections(terms_data: dict[str, Any], language: str = "en") -> str:
    """生成聚焦的“非标准写法 → 规范写法”对照表，供润色 Prompt 直接执行。

    相比整份术语 JSON，逐条对照表对模型的可执行性显著更高（提升术语召回率）。
    没有任何非标准条目时返回占位符。
    """
    lines: list[str] = []
    for entry in terms_data.get("entries", []):
        canonical = str(entry.get("canonical", "")).strip()
        if not canonical:
            continue
        for bad in entry.get("nonstandard", []) or []:
            bad = str(bad).strip()
            if bad:
                lines.append(f'- "{bad}" -> "{canonical}"')
    if not lines:
        return "(none)" if language == "en" else "（无）"
    return "\n".join(lines[:60])
