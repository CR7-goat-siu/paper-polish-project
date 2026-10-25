"""统一配置加载器。

优先使用 PyYAML；未安装时回退到内置的轻量 YAML 解析器。回退实现覆盖本项目
YAML 用到的全部语法：缩进块映射 / 块列表、行内流式列表 `[a, b]` 与流式映射
`{k: v}`、列表内联映射（`- key: value` + 缩进续行）、引号键与引号标量、注释。
不支持锚点 / 别名 / 多行折叠标量。两套实现的输出在本项目配置语法子集上保持
一致（tests/test_yaml_parser.py 含逐语法回归 + 全仓库配置冒烟）。
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
DOMAIN_DIR = CONFIG_DIR / "domains"
LEXICON_DIR = DATA_DIR / "lexicon"
EVAL_DIR = DATA_DIR / "eval"

try:  # pragma: no cover - 依赖可用性分支
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None


class ConfigError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# 轻量 YAML 解析（回退实现）
# --------------------------------------------------------------------------- #


def _strip_inline_comment(text: str) -> str:
    quote: str | None = None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
        elif ch == "#" and i > 0 and text[i - 1] in (" ", "\t"):
            return text[:i].rstrip()
    return text.rstrip()


def _indent_of(raw: str) -> int:
    leading = raw[: len(raw) - len(raw.lstrip(" "))]
    if "\t" in raw[: len(raw) - len(raw.lstrip())]:
        raise ConfigError("轻量 YAML 解析器不支持制表符缩进，请改用空格")
    return len(leading)


def _unquote(text: str) -> str:
    """剥掉成对的引号（键名与流式项共用；与 PyYAML 键处理一致）。"""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        return text[1:-1]
    return text


def _split_key_value(text: str) -> tuple[str | None, str]:
    """按第一个“冒号+空格”（或行尾冒号）切分键值；引号内的冒号不计。

    与 YAML 规范一致：`a:1` 是标量而非映射，URL 中的 `://` 不会误切。
    """
    quote: str | None = None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
        elif ch == ":":
            if i + 1 == len(text) or text[i + 1] in (" ", "\t"):
                key = _unquote(text[:i])
                if not key:
                    raise ConfigError(f"无效的映射行: {text!r}")
                return key, text[i + 1 :].strip()
    return None, ""


def _split_flow_key_value(text: str) -> tuple[str | None, str]:
    """流式映射项 `k: v`：按第一个引号外冒号切分（流上下文不强制冒号后空格）。"""
    quote: str | None = None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
        elif ch == ":":
            return text[:i].strip(), text[i + 1 :].strip()
    return None, ""


def _parse_scalar(raw: str) -> Any:
    raw = raw.strip()
    if not raw:
        return None
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        return raw[1:-1]
    lowered = raw.lower()
    if lowered in ("null", "~", "none"):
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if re.fullmatch(r"[-+]?\d+", raw):
        return int(raw)
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)[eE][-+]?\d+", raw) or re.fullmatch(
        r"[-+]?(?:\d+\.\d*|\.\d+)", raw
    ):
        return float(raw)
    return raw


def _split_flow_items(text: str) -> list[str]:
    """流式序列按顶层逗号切分；引号内与嵌套 `[]`/`{}` 内的逗号不计。"""
    items: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    depth = 0
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
            buf.append(ch)
        elif ch in "[{":
            depth += 1
            buf.append(ch)
        elif ch in "]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            items.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        items.append(tail)
    return items


def _parse_flow_value(text: str) -> Any:
    """解析行内值：流式列表 / 流式映射 / 标量。"""
    raw = text.strip()
    if not raw:
        return None
    if raw[0] in "[{" and raw[-1] in "]}":
        if raw[0] == "[" and raw[-1] != "]" or raw[0] == "{" and raw[-1] != "}":
            raise ConfigError(f"行内流式值括号不配对: {text!r}")
        inner = raw[1:-1].strip()
        if raw[0] == "[":
            return [_parse_flow_value(item) for item in _split_flow_items(inner)] if inner else []
        if not inner:
            return {}
        mapping: dict[str, Any] = {}
        for item in _split_flow_items(inner):
            key, value = _split_flow_key_value(item)
            if key is None:
                raise ConfigError(f"行内映射项缺少冒号: {item!r}")
            mapping[_unquote(key)] = _parse_flow_value(value)
        return mapping
    if raw[0] in "[{":
        raise ConfigError(f"行内流式值括号不配对: {text!r}")
    return _parse_scalar(raw)


def _is_list_item(text: str) -> bool:
    return text == "-" or text.startswith("- ")


def _parse_list(tokens: list[tuple[int, str]], index: int, indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    while index < len(tokens):
        cur_indent, text = tokens[index]
        if cur_indent != indent or not _is_list_item(text):
            break
        rest = "" if text == "-" else text[2:].strip()
        index += 1
        if not rest:
            # 空列表项：更深缩进 = 嵌套块；否则为 null 项
            if index < len(tokens) and tokens[index][0] > indent:
                nested_indent = tokens[index][0]
                if _is_list_item(tokens[index][1]):
                    value, index = _parse_list(tokens, index, nested_indent)
                else:
                    value, index = _parse_map(tokens, index, nested_indent)
                items.append(value)
            else:
                items.append(None)
            continue
        if _split_key_value(rest)[0] is not None:
            # 列表内联映射：rest 是首个键值对，后面所有更深缩进行同属该映射
            body: list[tuple[int, str]] = [(indent + 2, rest)]
            while index < len(tokens) and tokens[index][0] > indent:
                body.append(tokens[index])
                index += 1
            if len(body) > 1:
                # 对齐到实际子键缩进（规范 YAML 中续行与首键同列）
                body[0] = (body[1][0], rest)
            value, consumed = _parse_map(body, 0, body[0][0])
            if consumed != len(body):
                raise ConfigError(f"列表项内出现无法归组的内容: {body[consumed][1]!r}")
            items.append(value)
            continue
        items.append(_parse_flow_value(rest))
    return items, index


def _parse_map(tokens: list[tuple[int, str]], index: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(tokens):
        cur_indent, text = tokens[index]
        if cur_indent < indent:
            break
        if cur_indent != indent:
            raise ConfigError(f"意外的缩进层级: {text!r}")
        if _is_list_item(text):
            raise ConfigError(f"映射位置出现了列表项: {text!r}")
        key, rest = _split_key_value(text)
        if key is None:
            raise ConfigError(f"无法解析的 YAML 行: {text!r}")
        index += 1
        if rest == "":
            if index < len(tokens) and tokens[index][0] > indent:
                child_indent = tokens[index][0]
                if _is_list_item(tokens[index][1]):
                    result[key], index = _parse_list(tokens, index, child_indent)
                else:
                    result[key], index = _parse_map(tokens, index, child_indent)
            else:
                result[key] = None
        else:
            result[key] = _parse_flow_value(rest)
    return result, index


def _minimal_yaml_load(text: str) -> Any:
    tokens: list[tuple[int, str]] = []
    for raw in text.splitlines():
        stripped = _strip_inline_comment(raw)
        if not stripped.strip() or stripped.lstrip().startswith("#"):
            continue
        indent = _indent_of(stripped)
        tokens.append((indent, stripped.strip()))
    if not tokens:
        return {}
    first_indent = tokens[0][0]
    if first_indent != 0:
        raise ConfigError("YAML 顶层内容必须从第 0 列开始")
    if _is_list_item(tokens[0][1]):
        raise ConfigError("本项目 YAML 顶层必须是映射")
    value, index = _parse_map(tokens, 0, first_indent)
    if index < len(tokens):
        raise ConfigError(f"存在无法归组的 YAML 内容: {tokens[index][1]!r}")
    return value


# --------------------------------------------------------------------------- #
# 公共读取函数
# --------------------------------------------------------------------------- #


def load_yaml_file(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if yaml is not None:
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    data = _minimal_yaml_load(text)
    return data if isinstance(data, dict) else {}


@lru_cache(maxsize=8)
def load_models_config(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else (CONFIG_DIR / "models.yaml")
    data = load_yaml_file(target)
    if "models" not in data or "v4-pro" not in data["models"] or "v4-flash" not in data["models"]:
        raise ConfigError(f"{target} 缺少 models.v4-pro / models.v4-flash 配置")
    return data


@lru_cache(maxsize=4)
def load_rules_config(language: str) -> dict[str, Any]:
    norm = normalize_language(language)
    path = CONFIG_DIR / f"{norm}-rules.yaml"
    data = load_yaml_file(path)
    if "strengths" not in data:
        raise ConfigError(f"{path} 缺少 strengths 配置")
    for strength in ("light", "deep", "rewrite"):
        if strength not in data["strengths"]:
            raise ConfigError(f"{path} 的 strengths 缺少 {strength} 档")
    return data


@lru_cache(maxsize=2)
def load_literature_config(path: str | Path | None = None) -> dict[str, Any]:
    """加载 config/literature.yaml（文献检索 / 核验 / 引用格式化配置）。

    文件缺失或字段不全时回退到内置默认值：本函数只提供"可运行"的兜底，
    真正的参数来源始终是 config/literature.yaml。
    """
    target = Path(path) if path else (CONFIG_DIR / "literature.yaml")
    data = load_yaml_file(target) if target.exists() else {}
    network = data.get("network") or {}
    data.setdefault("schema_version", "1.0")
    data["network"] = {
        "enabled": bool(network.get("enabled", True)),
        "timeout_seconds": float(network.get("timeout_seconds", 20)),
        "max_retries": int(network.get("max_retries", 2)),
        "backoff_seconds": float(network.get("backoff_seconds", 1.5)),
        "min_interval_seconds": float(network.get("min_interval_seconds", 1.0)),
        "user_agent": str(network.get("user_agent") or "paper-polish-project/1.0"),
        "cache_dir": str(network.get("cache_dir") or "output/.litcache"),
        "cache_ttl_days": int(network.get("cache_ttl_days", 30)),
        "offline": bool(network.get("offline", False)),
        "contact_email": str(network.get("contact_email") or ""),
    }
    data.setdefault("sources", {})
    data.setdefault("verification", {})
    data.setdefault("ranking", {})
    data.setdefault("keywords", {})
    data.setdefault("gap_detection", {})
    data.setdefault("styles", {"default": "ieee", "supported": ["ieee", "apa", "gbt7714", "bibtex"]})
    return data


@lru_cache(maxsize=8)
def load_terms(language: str, domain: str | None = None) -> dict[str, Any]:
    """加载术语库。

    domain 指定且存在 data/terms/<domain>-<lang>.json 时优先使用，
    否则回退到历史路径 data/<lang>-terms.json。
    """
    norm = normalize_language(language)
    if domain and domain not in ("", "auto", "generic-or-legacy"):
        domain = normalize_domain(domain)
        candidate = DATA_DIR / "terms" / f"{domain}-{norm}.json"
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8"))
    path = DATA_DIR / f"{norm}-terms.json"
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=16)
def load_lexicon(kind: str, language: str) -> dict[str, Any]:
    """加载词库（hype / informal 等）；缺失时返回空结构而非报错。"""
    norm = normalize_language(language)
    path = LEXICON_DIR / f"{kind}-{norm}.json"
    if not path.exists():
        return {"language": norm, "kind": kind, "entries": []}
    return json.loads(path.read_text(encoding="utf-8"))


def list_domains() -> list[str]:
    if not DOMAIN_DIR.exists():
        return []
    return sorted(p.stem for p in DOMAIN_DIR.glob("*.yaml"))


def normalize_domain(domain: str) -> str:
    value = (domain or "").strip().lower().replace(" ", "-").replace("_", "-")
    if not value:
        return "generic"
    return value


@lru_cache(maxsize=16)
def load_domain_pack(domain: str) -> dict[str, Any]:
    """加载领域包 config/domains/<domain>.yaml；unknown 抛 ConfigError。"""
    key = normalize_domain(domain)
    path = DOMAIN_DIR / f"{key}.yaml"
    if not path.exists():
        raise ConfigError(
            f"未知领域包: {key!r}，可选 {list_domains() or ['(config/domains 为空)']}"
        )
    data = load_yaml_file(path)
    if "id" not in data:
        raise ConfigError(f"{path} 缺少 id 字段")
    return data


def domain_terms_file(domain: str, language: str) -> Path | None:
    """解析领域包声明的术语文件路径；未声明或文件不存在时返回 None。"""
    pack = load_domain_pack(domain)
    pattern = str(pack.get("terms_file", "")).strip()
    if not pattern:
        return None
    norm = normalize_language(language)
    path = DATA_DIR / pattern.replace("{lang}", norm)
    return path if path.exists() else None


def detect_domain(text: str, language: str, min_hits: int = 2) -> str:
    """按术语命中数自动识别领域；命中不足时返回 generic。

    命中统计同时计入 canonical / aliases / nonstandard——含大量退化写法的
    文本（正是最需要术语校对的场景）也能被识别到正确领域。
    """
    candidates = [d for d in list_domains() if d != "generic"]
    best_key, best_score = "generic", 0
    for domain in candidates:
        try:
            terms_data = load_terms(language, domain)
        except (ConfigError, FileNotFoundError):
            continue
        haystack = (text or "").lower()
        score = 0
        for entry in terms_data.get("entries", []):
            forms = [str(entry.get("canonical", ""))]
            forms += [str(a) for a in entry.get("aliases", [])]
            forms += [str(n) for n in entry.get("nonstandard", [])]
            for form in forms:
                form = form.strip().lower()
                if form and form in haystack:
                    score += 1
        if score > best_score:
            best_key, best_score = domain, score
    return best_key if best_score >= min_hits else "generic"


def normalize_language(language: str) -> str:
    value = language.strip().lower().replace("_", "-")
    if value in ("cn", "zh", "zh-cn", "zh-cn-", "chinese"):
        return "cn"
    if value in ("en", "en-us", "en-gb", "english"):
        return "en"
    raise ConfigError(f"不支持的语言: {language!r}，可选 cn / en")


def get_routing_table() -> dict[str, Any]:
    return load_models_config().get("routing", {})


def resolve_model_key(skill_name: str, strength: str | None = None, operation: str | None = None) -> str:
    """按 Skill + 强度（或操作）解析模型别名，例如 v4-pro / v4-flash。"""
    routing = get_routing_table()
    by_skill = routing.get("by_skill", {})
    table = by_skill.get(skill_name, {}) if isinstance(by_skill, dict) else {}
    candidate = None
    if strength:
        candidate = table.get(strength)
    if candidate is None and operation:
        candidate = table.get(operation)
    if candidate is None:
        candidate = routing.get("default_model", "v4-flash")
    return str(candidate)


def get_model_config(model_key: str | None = None) -> dict[str, Any]:
    """返回单个模型配置；model_key 是 models 下的别名（v4-pro / v4-flash）。"""
    cfg = load_models_config()
    key = model_key or cfg.get("routing", {}).get("default_model", "v4-flash")
    models = cfg["models"]
    if key not in models:
        raise ConfigError(f"未知模型别名: {key!r}，可选 {sorted(models)}")
    return models[key]


def get_provider_config() -> dict[str, Any]:
    return load_models_config().get("provider", {})
