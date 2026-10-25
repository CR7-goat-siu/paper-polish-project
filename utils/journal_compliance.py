"""期刊合规网关（对标 Penelope.ai / Paperpal Preflight 的可配置检查）。

输入一篇论文文本 + `config/journals/<id>.yaml` 期刊档案，逐项输出
PASS / FAIL / WARN 明细：字数上限、关键词数量、必需章节与顺序、图像格式与
真实 DPI（纯标准库解析 PNG/JPEG/TIFF 文件头）、参考文献编号风格、
五大声明区完整性（利益冲突 / 伦理 / 数据可用性 / 作者贡献 / 基金）。

设计约束（遵循 AGENTS.md）：
- 纯标准库、零网络、零模型；全部阈值来自期刊档案配置；
- 确定性判定：正则 + 文件头字节解析，不做任何"猜测"；
- 图像 DPI 未知（头里无分辨率信息）时报 WARN 而非 FAIL——不能核验不等于违规。
"""

from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import Any

from utils.config_loader import PROJECT_ROOT, load_yaml_file

JOURNALS_DIR = PROJECT_ROOT / "config" / "journals"

MARKDOWN_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*$")
NUMBERED_HEADING_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*|[一二三四五六七八九十]+)[\s、.．]\s*(.{2,80})$"
)
REFERENCE_HEADING_RE = re.compile(
    r"^\s*(?:#{1,6}\s+)?(?:references?|bibliography|参\s*考\s*文\s*献|文\s*献)\s*[:：]?\s*$",
    re.IGNORECASE,
)
REFERENCE_LINE_RE = re.compile(r"^\s*\[(\d{1,3})\]\s*(.*)$")
KEYWORDS_HEADING_RE = re.compile(r"^\s*(?:#{1,6}\s+)?\*{0,2}(?:keywords?|key words|index terms|关键词|关键字)\*{0,2}\s*[:：]?\s*(.*)$", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


# --------------------------------------------------------------------------- #
# 图像文件头解析（PNG / JPEG / TIFF，纯 struct）
# --------------------------------------------------------------------------- #


def image_header_info(source: str | Path | bytes) -> dict[str, Any]:
    """从文件头字节解析图像格式、像素尺寸与 DPI。

    返回 {format, width, height, dpi_x, dpi_y, dpi_source}；
    无法解析时 format="unknown"，各字段 None。不读取密钥、不发网络请求。
    """
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return _png_info(data)
    if data[:2] == b"\xff\xd8":
        return _jpeg_info(data)
    if data[:2] in (b"II", b"MM") and len(data) >= 8 and struct.unpack(data[:2] == b"MM" and ">H" or "<H", data[2:4])[0] == 42:
        return _tiff_info(data)
    return {
        "format": "unknown",
        "width": None,
        "height": None,
        "dpi_x": None,
        "dpi_y": None,
        "dpi_source": "none",
    }


def _png_info(data: bytes) -> dict[str, Any]:
    info = {
        "format": "png",
        "width": None,
        "height": None,
        "dpi_x": None,
        "dpi_y": None,
        "dpi_source": "none",
    }
    if len(data) >= 24 and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        info["width"], info["height"] = int(width), int(height)
    # 扫描 pHYs chunk（物理分辨率）
    offset = 8
    while offset + 8 <= len(data):
        try:
            length = struct.unpack(">I", data[offset : offset + 4])[0]
        except struct.error:
            break
        chunk_type = data[offset + 4 : offset + 8]
        chunk_data = data[offset + 8 : offset + 8 + length]
        if chunk_type == b"pHYs" and len(chunk_data) >= 9:
            x_ppu, y_ppu, unit = struct.unpack(">IIB", chunk_data[:9])
            if unit == 1:  # meter
                info["dpi_x"] = round(x_ppu * 0.0254, 1)
                info["dpi_y"] = round(y_ppu * 0.0254, 1)
                info["dpi_source"] = "pHYs"
            break
        if chunk_type == b"IDAT" or chunk_type == b"IEND":
            break  # pHYs 必须在图像数据前
        offset += 8 + length + 4  # len + type + data + crc
    return info


def _jpeg_info(data: bytes) -> dict[str, Any]:
    info = {
        "format": "jpeg",
        "width": None,
        "height": None,
        "dpi_x": None,
        "dpi_y": None,
        "dpi_source": "none",
    }
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            break
        marker = data[offset + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # 无长度段
            offset += 2
            continue
        if marker == 0xDA:  # SOS：扫描数据开始，头信息结束
            break
        try:
            seg_len = struct.unpack(">H", data[offset + 2 : offset + 4])[0]
        except struct.error:
            break
        payload = data[offset + 4 : offset + 2 + seg_len]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if len(payload) >= 5:
                height, width = struct.unpack(">HH", payload[1:5])
                info["width"], info["height"] = int(width), int(height)
        elif marker == 0xE0 and payload[:5] == b"JFIF\x00" and len(payload) >= 9:
            units, x_density, y_density = struct.unpack(">BHH", payload[5:10] if len(payload) >= 10 else payload[5:9] + b"\x00\x00\x00")
            if units == 1:  # dpi
                info["dpi_x"], info["dpi_y"] = float(x_density), float(y_density)
                info["dpi_source"] = "jfif"
            elif units == 2:  # dpcm
                info["dpi_x"] = round(x_density * 2.54, 1)
                info["dpi_y"] = round(y_density * 2.54, 1)
                info["dpi_source"] = "jfif"
        offset += 2 + seg_len
    return info


def _tiff_info(data: bytes) -> dict[str, Any]:
    little = data[:2] == b"II"
    endian = "<" if little else ">"
    info = {
        "format": "tiff",
        "width": None,
        "height": None,
        "dpi_x": None,
        "dpi_y": None,
        "dpi_source": "none",
    }
    try:
        ifd_offset = struct.unpack(endian + "I", data[4:8])[0]
        entry_count = struct.unpack(endian + "H", data[ifd_offset : ifd_offset + 2])[0]
        unit_code = 2  # 默认英寸
        resolutions: dict[int, tuple[int, int]] = {}
        for index in range(entry_count):
            entry = data[ifd_offset + 2 + index * 12 : ifd_offset + 2 + (index + 1) * 12]
            if len(entry) < 12:
                break
            tag, ftype, count = struct.unpack(endian + "HHI", entry[:8])
            value_bytes = entry[8:12]
            if tag == 256 and ftype in (3, 4):  # ImageWidth
                fmt = endian + ("H" if ftype == 3 else "I")
                info["width"] = struct.unpack(fmt, value_bytes[: struct.calcsize(fmt)])[0]
            elif tag == 257 and ftype in (3, 4):  # ImageLength
                fmt = endian + ("H" if ftype == 3 else "I")
                info["height"] = struct.unpack(fmt, value_bytes[: struct.calcsize(fmt)])[0]
            elif tag in (282, 283) and ftype == 5:  # X/YResolution（RATIONAL）
                offset = struct.unpack(endian + "I", value_bytes)[0]
                if offset + 8 <= len(data):
                    num, den = struct.unpack(endian + "II", data[offset : offset + 8])
                    if den:
                        resolutions[tag] = (num, den)
            elif tag == 296 and ftype == 3:  # ResolutionUnit
                unit_code = struct.unpack(endian + "H", value_bytes[:2])[0]
        factor = 1.0 if unit_code == 2 else 2.54 if unit_code == 3 else 0.0
        if 282 in resolutions and factor:
            info["dpi_x"] = round(resolutions[282][0] / resolutions[282][1] * factor, 1)
            info["dpi_source"] = "tiff_tag"
        if 283 in resolutions and factor:
            info["dpi_y"] = round(resolutions[283][0] / resolutions[283][1] * factor, 1)
            info["dpi_source"] = "tiff_tag"
    except (struct.error, OSError):
        pass
    return info


# --------------------------------------------------------------------------- #
# 期刊档案与文本切分
# --------------------------------------------------------------------------- #


def load_journal_profile(journal: str | Path) -> dict[str, Any]:
    """按 id（config/journals/<id>.yaml）或直接路径加载期刊档案。"""
    path = Path(journal)
    if not path.exists():
        path = JOURNALS_DIR / f"{journal}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"期刊档案不存在: {journal}（{JOURNALS_DIR} 下无该 id）")
    profile = load_yaml_file(path)
    profile["_profile_path"] = str(path.resolve())
    profile.setdefault("id", path.stem)
    return profile


def _split_sections(text: str) -> list[dict[str, str]]:
    """按 Markdown / 数字标题切分章节（与 review-sim 同思路的本地实现）。"""
    sections: list[dict[str, str]] = []
    current_heading = ""
    buffer: list[str] = []

    def _flush() -> None:
        content = "\n".join(buffer).strip()
        if content:
            sections.append({"heading": current_heading or "Front matter", "text": content})
        buffer.clear()

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        heading_match = MARKDOWN_HEADING_RE.match(line)
        numbered = NUMBERED_HEADING_RE.match(line)
        if heading_match:
            _flush()
            current_heading = heading_match.group(2).strip()
            buffer.append(line)
            continue
        if numbered and len(line) <= 100 and not line.endswith((".", "。", "!", "！", "?", "？")):
            _flush()
            current_heading = numbered.group(1).strip()
            buffer.append(line)
            continue
        buffer.append(raw_line)
    _flush()
    return sections


def _count_words(text: str, language: str) -> int:
    if language == "cn":
        return len(_CJK_RE.findall(text or ""))
    return len(re.findall(r"\S+", text or ""))


def _section_matches(heading: str, patterns: list[str]) -> bool:
    normalized = re.sub(r"^\s*(?:\d+(?:\.\d+)*\s*|[一二三四五六七八九十]+\s*)", "", heading.strip()).strip().lower()
    normalized = re.sub(r"[:：.。]\s*$", "", normalized)
    for pattern in patterns:
        if re.search(pattern, normalized, flags=re.IGNORECASE):
            return True
    return False


def _find_keywords_count(text: str, separators: list[str]) -> tuple[int, str]:
    """返回 (关键词数量, 原始关键词行)。找不到关键词行返回 (-1, "")。

    支持两种写法：关键词与标题同行（"**Keywords**: a; b"），
    以及标题行后紧跟的下一行列表。
    """
    lines = (text or "").splitlines()
    for index, line in enumerate(lines):
        match = KEYWORDS_HEADING_RE.match(line.strip())
        if not match:
            continue
        raw = match.group(1).strip()
        if not raw:
            # 标题行无内容：找下一个非空行
            for follower in lines[index + 1 :]:
                stripped = follower.strip()
                if not stripped:
                    continue
                if MARKDOWN_HEADING_RE.match(stripped) or NUMBERED_HEADING_RE.match(stripped):
                    break  # 已进入下一个章节
                raw = stripped
                break
        if not raw:
            continue
        if separators:
            pattern = "|".join(re.escape(sep) for sep in separators)
            count = len([item for item in re.split(pattern, raw) if item.strip()])
        else:
            count = len([item for item in raw.split(";") if item.strip()])
        return count, raw
    return -1, ""


# --------------------------------------------------------------------------- #
# 逐项合规检查
# --------------------------------------------------------------------------- #


def check_journal_compliance(
    text: str,
    profile: dict[str, Any],
    *,
    figures_dir: str | Path | None = None,
    figure_files: list[str | Path] | None = None,
) -> dict[str, Any]:
    """执行期刊档案定义的全部确定性检查，返回逐项明细与汇总。

    每项检查：{check, status: PASS|FAIL|WARN|SKIP, detail, expected, actual}；
    findings 以 issue-registry 可归一的格式附在 result["findings"]。
    """
    language = str(profile.get("language") or "en")
    sections = _split_sections(text)
    body_text = "\n".join(
        section["text"]
        for section in sections
        if not REFERENCE_HEADING_RE.match(section["heading"])
    )
    items: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []

    def _add(check: str, status: str, detail: str, *, expected: Any = "", actual: Any = "", severity: str = "error") -> None:
        items.append(
            {
                "check": check,
                "status": status,
                "detail": detail,
                "expected": expected,
                "actual": actual,
            }
        )
        if status == "FAIL":
            findings.append(
                {
                    "check": check,
                    "found": str(actual)[:120],
                    "severity": severity,
                    "message": detail,
                    "context_snippet": str(actual)[:200],
                    "suggestion": f"按期刊 {profile.get('name') or profile.get('id')} 要求修正 {check}",
                }
            )
        elif status == "WARN":
            findings.append(
                {
                    "check": check,
                    "found": str(actual)[:120],
                    "severity": "warning",
                    "message": detail,
                    "context_snippet": str(actual)[:200],
                    "suggestion": f"人工确认 {check}",
                }
            )

    # 1) 摘要与正文字数
    word_limits = profile.get("word_limits") or {}
    abstract_max = word_limits.get("abstract_max_words")
    body_min = word_limits.get("body_min_words")
    body_max = word_limits.get("body_max_words")
    abstract_text = ""
    for section in sections:
        if _section_matches(section["heading"], ["^abstract", "^摘要"]):
            abstract_text = re.sub(r"^\s*#{1,6}\s+.*$", "", section["text"], flags=re.MULTILINE).strip()
            break
    if abstract_max is None:
        _add("word_limit_abstract", "SKIP", "档案未定义摘要字数上限")
    elif not abstract_text:
        _add("word_limit_abstract", "FAIL", "未找到摘要章节，无法核字数", expected=f"≤ {abstract_max} 词")
    else:
        count = _count_words(abstract_text, language)
        if count > int(abstract_max):
            _add(
                "word_limit_abstract",
                "FAIL",
                f"摘要 {count} 词超出上限 {abstract_max}",
                expected=f"≤ {abstract_max}",
                actual=count,
            )
        else:
            _add("word_limit_abstract", "PASS", f"摘要 {count} 词（上限 {abstract_max}）", expected=f"≤ {abstract_max}", actual=count)
    body_count = _count_words(body_text, language)
    if body_max is not None:
        if body_count > int(body_max):
            _add("word_limit_body", "FAIL", f"正文 {body_count} 词超出上限 {body_max}", expected=f"≤ {body_max}", actual=body_count)
        elif body_min is not None and body_count < int(body_min):
            _add("word_limit_body", "WARN", f"正文 {body_count} 词低于建议下限 {body_min}", expected=f"{body_min}–{body_max}", actual=body_count)
        else:
            _add("word_limit_body", "PASS", f"正文 {body_count} 词在允许区间", expected=f"{body_min}–{body_max}", actual=body_count)
    else:
        _add("word_limit_body", "SKIP", "档案未定义正文字数区间")

    # 2) 关键词数量
    keywords_cfg = profile.get("keywords") or {}
    k_min = keywords_cfg.get("min")
    k_max = keywords_cfg.get("max")
    separators = [str(item) for item in keywords_cfg.get("separators") or [";", "；"]]
    k_count, k_raw = _find_keywords_count(text, separators)
    if k_min is None or k_max is None:
        _add("keywords_range", "SKIP", "档案未定义关键词数量区间")
    elif k_count < 0:
        _add("keywords_range", "FAIL", "未找到关键词行", expected=f"{k_min}–{k_max} 个")
    elif not int(k_min) <= k_count <= int(k_max):
        _add("keywords_range", "FAIL", f"关键词 {k_count} 个超出区间 [{k_min}, {k_max}]", expected=f"{k_min}–{k_max}", actual=k_count)
    else:
        _add("keywords_range", "PASS", f"关键词 {k_count} 个在区间内", expected=f"{k_min}–{k_max}", actual=k_count)

    # 3) 必需章节与顺序（档案用命名映射：章节类型 → {patterns}）
    sections_cfg = profile.get("sections") or {}
    required = sections_cfg.get("required") or {}
    enforce_order = bool(sections_cfg.get("enforce_order", False))
    heading_list = [section["heading"] for section in sections]
    type_positions: dict[str, int] = {}
    for section_type, entry in required.items():
        entry = entry if isinstance(entry, dict) else {}
        patterns = [str(item) for item in entry.get("patterns") or []]
        if not patterns:
            continue
        matched_at = -1
        for index, heading in enumerate(heading_list):
            if _section_matches(heading, patterns):
                matched_at = index
                break
        if matched_at < 0:
            _add(f"sections_missing::{section_type}", "FAIL", f"缺少必需章节 {section_type}", expected=str(patterns))
        else:
            type_positions[section_type] = matched_at
            _add(f"sections_missing::{section_type}", "PASS", f"章节 {section_type} 存在（{heading_list[matched_at]}）")
    if enforce_order and type_positions:
        ordered_types = [str(item) for item in required]
        present = [item for item in ordered_types if item in type_positions]
        positions = [type_positions[item] for item in present]
        if positions != sorted(positions):
            _add(
                "sections_order",
                "FAIL",
                f"章节顺序不符合档案要求: {present}",
                expected=str(ordered_types),
                actual=str(present),
            )
        else:
            _add("sections_order", "PASS", f"章节顺序符合要求: {present}")

    # 4) 图像格式与 DPI（figure_files / figures_dir 提供时检查文件头）
    figures_cfg = profile.get("figures") or {}
    allowed_ext = {str(item).lower().lstrip(".") for item in figures_cfg.get("allowed_extensions") or []}
    min_dpi = figures_cfg.get("min_dpi")
    image_paths: list[Path] = []
    if figure_files:
        image_paths = [Path(item) for item in figure_files if Path(item).exists()]
    elif figures_dir is not None:
        fig_dir = Path(figures_dir)
        if fig_dir.exists():
            image_paths = sorted(
                path
                for path in fig_dir.iterdir()
                if path.is_file() and not path.name.startswith(".")
            )
    if figures_cfg and image_paths:
        for path in image_paths:
            ext = path.suffix.lower().lstrip(".")
            if allowed_ext and ext not in allowed_ext:
                _add(
                    "figure_format",
                    "FAIL",
                    f"{path.name} 格式 {ext} 不在允许列表 {sorted(allowed_ext)}",
                    expected=str(sorted(allowed_ext)),
                    actual=path.name,
                )
                continue
            info = image_header_info(path)
            if info["format"] == "unknown":
                _add("figure_format", "WARN", f"{path.name} 无法识别为 PNG/JPEG/TIFF（如为 EPS/PDF 请人工确认）", actual=path.name)
                continue
            dpi_value = info.get("dpi_x") or info.get("dpi_y")
            if min_dpi is not None and dpi_value is None:
                _add("figure_dpi", "WARN", f"{path.name} 头部无分辨率信息（{info['width']}×{info['height']} px），请人工确认", expected=f"≥ {min_dpi}", actual=path.name)
            elif min_dpi is not None and dpi_value < float(min_dpi):
                _add("figure_dpi", "FAIL", f"{path.name} 分辨率 {dpi_value:g} dpi 低于阈值 {min_dpi}", expected=f"≥ {min_dpi}", actual=dpi_value)
            else:
                _add("figure_dpi", "PASS", f"{path.name} {dpi_value:g} dpi（{info['width']}×{info['height']} px）", expected=f"≥ {min_dpi}", actual=dpi_value)
    elif figures_cfg and not image_paths:
        _add("figure_files", "WARN", "未提供图像文件，无法核验格式与 DPI")

    # 5) 参考文献编号风格
    ref_style = str(profile.get("reference_style") or "").lower()
    references_text = ""
    in_refs = False
    for section in sections:
        if REFERENCE_HEADING_RE.match(section["heading"]):
            in_refs = True
            references_text = section["text"]
            break
    if not ref_style:
        _add("reference_style", "SKIP", "档案未定义参考文献风格")
    elif not in_refs:
        _add("reference_style", "WARN", "未找到参考文献章节", expected=ref_style)
    elif ref_style in {"numbered", "ieee", "numeric"}:
        lines = [line for line in references_text.splitlines() if line.strip()]
        numbered_lines = sum(1 for line in lines if REFERENCE_LINE_RE.match(line))
        if lines and numbered_lines == 0:
            _add("reference_style", "FAIL", "档案要求顺序编号 [n]，但参考文献行未使用 [n] 编号", expected="numbered [n]", actual=references_text[:80])
        else:
            _add("reference_style", "PASS", f"参考文献为顺序编号（{numbered_lines} 条）", expected="numbered [n]", actual=numbered_lines)
    elif ref_style in {"author-year", "apa", "harvard"}:
        year_pattern = re.compile(r"\(\s*(?:19|20)\d{2}[a-z]?\s*\)")
        lines = [line for line in references_text.splitlines() if line.strip()]
        if lines and not any(year_pattern.search(line) for line in lines):
            _add("reference_style", "FAIL", "档案要求作者-年份制，但参考文献行缺少 (Year)", expected="author-year", actual=references_text[:80])
        else:
            _add("reference_style", "PASS", "参考文献为作者-年份制", expected="author-year")
    else:
        _add("reference_style", "SKIP", f"未支持的参考文献风格检查: {ref_style}")

    # 6) 声明区完整性（五大核心声明；档案用命名映射：声明 id → {label, severity, patterns}）
    for decl_id, declaration in (profile.get("required_declarations") or {}).items():
        if not isinstance(declaration, dict):
            continue
        decl_id = str(decl_id)
        label = str(declaration.get("label") or decl_id)
        patterns = [str(item) for item in declaration.get("patterns") or []]
        severity = str(declaration.get("severity") or "fail")
        if not patterns:
            _add(f"declaration_missing::{decl_id}", "SKIP", "档案未定义声明区正则")
            continue
        lowered = (text or "").lower()
        hit = next((pattern for pattern in patterns if pattern.lower() in lowered), None)
        if hit:
            _add(f"declaration_missing::{decl_id}", "PASS", f"{label} 声明存在（命中 {hit!r}）")
        elif severity == "fail":
            _add(
                f"declaration_missing::{decl_id}",
                "FAIL",
                f"缺少必需声明区：{label}",
                expected=str(patterns[:3]),
            )
        else:
            _add(f"declaration_missing::{decl_id}", "WARN", f"未找到声明区：{label}")

    fail_count = sum(1 for item in items if item["status"] == "FAIL")
    warn_count = sum(1 for item in items if item["status"] == "WARN")
    checked = [item for item in items if item["status"] != "SKIP"]
    pass_gate = bool(checked) and fail_count == 0
    return {
        "schema_version": 1,
        "journal_id": str(profile.get("id") or ""),
        "journal_name": str(profile.get("name") or ""),
        "profile_path": str(profile.get("_profile_path") or ""),
        "items": items,
        "fail_count": fail_count,
        "warn_count": warn_count,
        "checked_count": len(checked),
        "pass": pass_gate,
        "findings": findings,
    }
