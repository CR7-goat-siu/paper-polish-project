"""cite-manager Skill：引用编号管理（纯离线的确定性规则处理）。

覆盖论文修订中高频的引用操作：
- audit：核对正文引用标记与参考文献列表（漏引 / 漏列 / 重复）；
- apply：按映射 {旧编号: 新编号} 重写正文引用标记（含区间、全角逗号，
  组内去重排序、可选区间压缩），可选同步重编号文献列表条目；
- renumber：删除指定参考文献后，幸存条目按原顺序连续重排 1..N，
  正文与列表同步，返回实际使用的映射；
- pending：在指定锚点后插入“待补文献”占位标记（不虚构引用），
  以及列出全文所有待补标记。

docx 联动：传入 paragraphs（段落文本列表）时，token 级改动会输出
docx_plan = {段落位置: {"token_replacements": [(old, new), ...]}}，
可直接组装为 write_polished_docx 的 replacements 参数做外科式回写
（含公式段落安全改号）。注意：条目的物理增删无法用 token 表达；
docx 侧新增条目请用 write_polished_docx 的 insertions。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.citation_check import format_all_styles, format_reference  # noqa: E402
from utils.config_loader import normalize_language  # noqa: E402
from utils.logger import get_logger  # noqa: E402

logger = get_logger("cite-manager")

VALID_MODES = ("audit", "apply", "renumber", "pending", "insert")
VALID_ACTIONS = ("list", "mark")

# 正文引用标记：[1] / [1,2] / [1-3] / [1，2] / [1–3] / [1,3-5,8]
IN_TEXT_RE = re.compile(r"\[(\d{1,3}(?:\s*[,，]\s*\d{1,3}|\s*[-–—]\s*\d{1,3})*)\]")
REF_HEADING_RE = re.compile(
    r"^\s*(references|bibliography|参\s*考\s*文\s*献|文\s*献)\s*[:：]?\s*$",
    re.IGNORECASE,
)
REF_ENTRY_RE = re.compile(r"^\s*\[(\d{1,3})\]\s*(.*)$")
# 与 format-adapter 的参考文献行特征保持同义的本地副本（跨 Skill 禁止 import 实现细节）
REF_HINT_RE = re.compile(
    r"(?i)\bpp?\.\s|\bvol\.\s|\bno\.\s|\bdoi\b|\bet al\b|第\s*\d+\s*[页卷期]"
)
PENDING_TOKENS = {"en": "[CITE-TBD]", "cn": "[待补文献]"}
PENDING_FIND_RE = re.compile(r"\[CITE-TBD\]|\[待补文献\]")


# --------------------------------------------------------------------------- #
# 标记解析与格式化
# --------------------------------------------------------------------------- #


def _split_group(inner: str) -> list[int] | None:
    """把标记内的编号串解析为升序列表；区间 a-b 展开为闭区间。无法解析返回 None。"""
    numbers: list[int] = []
    for part in re.split(r"[,，]", inner):
        part = part.strip()
        if not part:
            return None
        range_match = re.fullmatch(r"(\d{1,3})\s*[-–—]\s*(\d{1,3})", part)
        if range_match:
            start, end = int(range_match.group(1)), int(range_match.group(2))
            if end < start or end - start > 200:
                return None
            numbers.extend(range(start, end + 1))
        elif part.isdigit():
            numbers.append(int(part))
        else:
            return None
    return sorted(set(numbers))


def _expand_run(start: int, end: int) -> str:
    return ", ".join(str(n) for n in range(start, end + 1))


def _has_compressible_run(numbers: list[int]) -> bool:
    """是否存在长度 ≥3 的连续编号跑（compress_ranges 下可写成 a-b）。"""
    run_length = 1
    for prev, cur in zip(numbers, numbers[1:]):
        run_length = run_length + 1 if cur == prev + 1 else 1
        if run_length >= 3:
            return True
    return False


def _format_numbers(numbers: list[int], compress: bool) -> str:
    """编号列表格式化；compress 时把跨度 ≥3 的连续编号压缩为 a-b 区间。"""
    if not numbers:
        return ""
    if not compress:
        return ", ".join(str(n) for n in numbers)
    parts: list[str] = []
    run_start = prev = numbers[0]
    for num in numbers[1:]:
        if num == prev + 1:
            prev = num
            continue
        parts.append(f"{run_start}-{prev}" if prev - run_start >= 2 else _expand_run(run_start, prev))
        run_start = prev = num
    parts.append(f"{run_start}-{prev}" if prev - run_start >= 2 else _expand_run(run_start, prev))
    return ", ".join(parts)


# --------------------------------------------------------------------------- #
# 文本结构分析
# --------------------------------------------------------------------------- #


def _analyze_lines(text: str) -> tuple[list[str], set[int], list[tuple[int, str, int]]]:
    """切行并识别参考文献条目行。

    返回 (lines, 条目行号集合, 条目列表[(编号, 条目正文, 行号)])。
    优先按 References/参考文献 标题划定条目区（标题允许 Markdown # 前缀与
    编号前缀，如 "## 参考文献" / "2 参考文献"，GB/T 7714 风格条目不含
    pp./vol./et al 时也只在该区内识别）；无标题时退化为
    “行首 [n] + 参考文献行特征（pp./vol./doi 等）”识别。
    LaTeX 的 \\bibitem 条目按出现顺序编号（thebibliography 语义）。
    """
    lines = text.split("\n")
    entry_line_indexes: set[int] = set()
    entries: list[tuple[int, str, int]] = []
    in_references = False
    bibitem_counter = 0
    for index, line in enumerate(lines):
        stripped = re.sub(r"^(?:#{1,6}|\d+(?:\.\d+)*)\s*", "", line.strip())
        if REF_HEADING_RE.match(stripped) or stripped.startswith("\\begin{thebibliography}"):
            in_references = True
            continue
        bibitem_match = re.match(r"\\bibitem(?:\[[^\]]*\])?\{[^}]*\}\s*(.*)$", line.strip())
        if bibitem_match and in_references:
            bibitem_counter += 1
            entry_line_indexes.add(index)
            entries.append((bibitem_counter, bibitem_match.group(1).rstrip(), index))
            continue
        match = REF_ENTRY_RE.match(line)
        if match and (in_references or REF_HINT_RE.search(line)):
            entry_line_indexes.add(index)
            entries.append((int(match.group(1)), match.group(2).rstrip(), index))
    return lines, entry_line_indexes, entries


def _rewrite_marker_line(
    line: str,
    mapping: dict[int, int],
    compress: bool,
    token_pairs: list[tuple[str, str]],
    forbid_numbers: set[int] | None = None,
) -> str:
    """对单行做正文引用标记重写；token_pairs 记录实际发生的 (旧标记, 新标记)。

    零改动原则：编号集合未变且无可压缩跑（compress_ranges）的标记原样保留，
    不做逗号/空格风格归一，避免外科式 docx 回写产生计划外的文本漂移。
    """

    def replace(match: re.Match[str]) -> str:
        numbers = _split_group(match.group(1))
        if numbers is None:
            return match.group(0)
        if forbid_numbers is not None:
            hit = forbid_numbers.intersection(numbers)
            if hit:
                raise ValueError(
                    f"引用编号 {sorted(hit)} 已从文献列表删除但仍被正文引用"
                    f"（标记 {match.group(0)}）；请先用 apply 把其映射到幸存条目"
                )
        new_numbers = sorted({mapping.get(n, n) for n in numbers})
        if new_numbers == numbers and not (compress and _has_compressible_run(numbers)):
            return match.group(0)
        new_marker = f"[{_format_numbers(new_numbers, compress)}]"
        if new_marker != match.group(0):
            token_pairs.append((match.group(0), new_marker))
        return new_marker

    return IN_TEXT_RE.sub(replace, line)


def _transform_text(
    text: str,
    mapping: dict[int, int],
    *,
    compress: bool,
    entry_renumber: dict[int, int],
    remove_entries: set[int] | None = None,
) -> tuple[str, list[tuple[str, str]]]:
    """全文变换：正文标记按映射重写，条目行原位重编号（不做物理重排）。

    remove_entries 中的条目行整行清空（文本模式删除；docx 侧物理删除
    无法用 token 表达，见模块 docstring）。
    """
    remove_entries = remove_entries or set()
    lines, entry_line_indexes, entries = _analyze_lines(text)
    token_pairs: list[tuple[str, str]] = []
    forbid = remove_entries or None
    for index, line in enumerate(lines):
        if index in entry_line_indexes:
            continue
        lines[index] = _rewrite_marker_line(line, mapping, compress, token_pairs, forbid)
    for num, rest, line_index in entries:
        if num in remove_entries:
            lines[line_index] = ""
            continue
        new_num = entry_renumber.get(num, num)
        if new_num == num:
            continue
        indent = re.match(r"^\s*", lines[line_index]).group(0)
        lines[line_index] = f"{indent}[{new_num}] {rest}"
        token_pairs.append((f"[{num}]", f"[{new_num}]"))
    return "\n".join(lines), token_pairs


def _build_docx_plan(
    paragraphs: list[str] | None,
    text: str,
    mapping: dict[int, int],
    *,
    compress: bool,
    entry_renumber: dict[int, int],
    remove_entries: set[int] | None = None,
) -> dict[int, dict[str, list[tuple[str, str]]]]:
    """按段落位置产出外科式替换计划，键为 paragraphs 列表中的下标。

    段落是否为条目行，以全文分析得到的条目行文本（strip 后）精确匹配，
    避免“正文以 [n] 开头的句子”被误判为条目。
    """
    if paragraphs is None:
        return {}
    remove_entries = remove_entries or set()
    _, _, entries = _analyze_lines(text)
    entry_line_texts = {f"[{num}] {rest}".strip() for num, rest, _ in entries}
    plan: dict[int, dict[str, list[tuple[str, str]]]] = {}
    skipped_removals: list[int] = []
    for position, paragraph in enumerate(paragraphs):
        pairs: list[tuple[str, str]] = []
        stripped = paragraph.strip()
        if stripped in entry_line_texts:
            match = REF_ENTRY_RE.match(paragraph)
            if match:
                num = int(match.group(1))
                if num in remove_entries:
                    # 物理删除段落无法用 token 表达：跳过并提示人工处理
                    skipped_removals.append(num)
                else:
                    new_num = entry_renumber.get(num, num)
                    if new_num != num:
                        pairs.append((f"[{num}]", f"[{new_num}]"))
        else:
            _rewrite_marker_line(paragraph, mapping, compress, pairs, remove_entries or None)
        if pairs:
            plan[position] = {"token_replacements": pairs}
    if skipped_removals:
        logger.warning(
            "docx_plan 跳过被删除的条目段落 %s：物理删除需人工或 insertions 处理",
            sorted(set(skipped_removals)),
        )
    return plan


# --------------------------------------------------------------------------- #
# 各模式实现
# --------------------------------------------------------------------------- #


def _audit(text: str) -> dict[str, Any]:
    lines, entry_line_indexes, entries = _analyze_lines(text)
    cited: dict[int, int] = {}
    marker_count = 0
    unparsable: list[str] = []
    for index, line in enumerate(lines):
        if index in entry_line_indexes:
            continue
        for match in IN_TEXT_RE.finditer(line):
            numbers = _split_group(match.group(1))
            if numbers is None:
                unparsable.append(match.group(0))
                continue
            if min(numbers) == 0:
                # 引用编号从 1 起：含 0 的方括号组是数学区间记号
                # （如归一化区间 "[0, 1]"、像素范围 "[0-255]"），不是引用
                continue
            marker_count += 1
            for num in numbers:
                cited[num] = cited.get(num, 0) + 1
    listed = [num for num, _, _ in entries]
    seen: set[int] = set()
    duplicated = [num for num in listed if num in seen or seen.add(num)]
    return {
        "mode": "audit",
        "text": text,
        "changes": [],
        "audit": {
            "in_text_marker_count": marker_count,
            "cited": {str(num): cited[num] for num in sorted(cited)},
            "ref_entry_count": len(entries),
            "listed": sorted(set(listed)),
            "cited_not_listed": sorted(set(cited) - set(listed)),
            "listed_not_cited": sorted(set(listed) - set(cited)),
            "duplicated_entries": sorted(set(duplicated)),
            "unparsable_markers": unparsable,
        },
    }


def _coerce_mapping(raw: Any) -> dict[int, int]:
    if not isinstance(raw, dict):
        raise ValueError("mapping 需为 {旧编号: 新编号} 的 JSON 对象")
    mapping: dict[int, int] = {}
    for old, new in raw.items():
        old_num, new_num = int(old), int(new)
        if not (1 <= old_num <= 999 and 1 <= new_num <= 999):
            raise ValueError(f"引用编号超出范围: {old} -> {new}")
        mapping[old_num] = new_num
    return mapping


def _load_mapping_arg(args: dict[str, Any]) -> dict[int, int]:
    raw = args.get("mapping")
    if raw is None:
        mapping_file = args.get("mapping_file")
        if not mapping_file:
            raise ValueError("apply 模式需要 mapping（JSON 对象）或 mapping_file")
        raw = json.loads(Path(mapping_file).read_text(encoding="utf-8-sig"))
    if isinstance(raw, str):
        raw = json.loads(raw)
    return _coerce_mapping(raw)


def _load_int_list(raw: Any) -> list[int]:
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [p.strip() for p in re.split(r"[,，\s]+", raw) if p.strip()]
    else:
        parts = [str(p).strip() for p in raw]
    values = [int(p) for p in parts if p]
    if any(v < 1 or v > 999 for v in values):
        raise ValueError(f"引用编号超出范围: {values}")
    return values


def _apply_mode(args: dict[str, Any], text: str, language: str) -> dict[str, Any]:
    mapping = _load_mapping_arg(args)
    compress = bool(args.get("compress_ranges", True))
    reorder_list = bool(args.get("reorder_list", False))
    entry_renumber: dict[int, int] = {}
    if reorder_list:
        entry_renumber = dict(mapping)
        _, _, entries = _analyze_lines(text)
        new_nums = [entry_renumber.get(num, num) for num, _, _ in entries]
        duplicated = sorted({n for n in new_nums if new_nums.count(n) > 1})
        if duplicated:
            raise ValueError(
                f"列表重编号后出现重复编号 {duplicated}；若为合并条目，"
                "请先 apply 映射正文标记，再以 remove+renumber 收敛列表"
            )
    new_text, token_pairs = _transform_text(
        text, mapping, compress=compress, entry_renumber=entry_renumber
    )
    changes = [
        f"正文引用标记按映射重写: {json.dumps({str(k): v for k, v in mapping.items()}, ensure_ascii=False)}",
        f"共重写 {len(token_pairs)} 处标记",
    ]
    if reorder_list:
        changes.append("参考文献条目按映射原位重编号")
    if not token_pairs:
        changes.append("映射未命中任何标记，文本未变化")
    result: dict[str, Any] = {
        "mode": "apply",
        "text": new_text,
        "changes": changes,
        "mapping": {str(k): v for k, v in mapping.items()},
        "token_pairs": token_pairs,
    }
    docx_plan = _build_docx_plan(
        args.get("paragraphs"), text, mapping,
        compress=compress, entry_renumber=entry_renumber,
    )
    if docx_plan:
        result["docx_plan"] = docx_plan
    return result


def _renumber_mode(args: dict[str, Any], text: str) -> dict[str, Any]:
    remove = set(_load_int_list(args.get("remove")))
    if not remove:
        raise ValueError("renumber 模式需要 remove（要删除的引用编号列表）")
    compress = bool(args.get("compress_ranges", True))
    _, _, entries = _analyze_lines(text)
    listed = {num for num, _, _ in entries}
    unknown = sorted(remove - listed)
    if unknown:
        raise ValueError(f"remove 中的编号不在参考文献列表内: {unknown}")
    survivors = sorted(num for num in listed if num not in remove)
    num_map = {old: position for position, old in enumerate(survivors, start=1)}
    new_text, token_pairs = _transform_text(
        text, num_map, compress=compress, entry_renumber=num_map, remove_entries=remove
    )
    result: dict[str, Any] = {
        "mode": "renumber",
        "text": new_text,
        "changes": [
            f"删除参考文献 {sorted(remove)}，幸存条目连续重排 1..{len(survivors)}",
            f"共重写 {len(token_pairs)} 处标记/条目",
        ],
        "mapping_used": {str(k): v for k, v in num_map.items()},
        "removed": sorted(remove),
        "token_pairs": token_pairs,
    }
    docx_plan = _build_docx_plan(
        args.get("paragraphs"), text, num_map,
        compress=compress, entry_renumber=num_map, remove_entries=remove,
    )
    if docx_plan:
        result["docx_plan"] = docx_plan
    return result


def _pending_mode(args: dict[str, Any], text: str, language: str) -> dict[str, Any]:
    action = str(args.get("action", "list")).lower()
    if action not in VALID_ACTIONS:
        raise ValueError(f"未知 action: {action!r}，可选 {VALID_ACTIONS}")
    placeholder = str(args.get("placeholder") or PENDING_TOKENS[language])
    if action == "list":
        findings = []
        for match in PENDING_FIND_RE.finditer(text):
            start = max(0, match.start() - 30)
            end = min(len(text), match.end() + 30)
            findings.append(
                {"token": match.group(0), "offset": match.start(), "context": text[start:end]}
            )
        return {
            "mode": "pending",
            "action": "list",
            "text": text,
            "changes": [],
            "pending": findings,
            "pending_count": len(findings),
        }
    anchors = args.get("anchors") or []
    if isinstance(anchors, str):
        anchors = [a.strip() for a in anchors.split("\n") if a.strip()]
    if not anchors:
        raise ValueError("pending mark 需要 anchors（锚点文本列表，每个锚点须在文中恰好出现一次）")
    new_text = text
    changes: list[str] = []
    positions: list[dict[str, Any]] = []
    for anchor in anchors:
        count = new_text.count(anchor)
        if count != 1:
            raise ValueError(f"锚点 {anchor!r} 出现 {count} 次（要求恰好 1 次），请补充上下文使其唯一")
        pos = new_text.find(anchor)
        insert = placeholder if language == "cn" else f" {placeholder}"
        new_text = new_text[: pos + len(anchor)] + insert + new_text[pos + len(anchor) :]
        positions.append({"anchor": anchor, "offset": pos + len(anchor), "token": placeholder})
        changes.append(f"在锚点后插入待补标记 {placeholder}: {anchor[:40]}")
    return {
        "mode": "pending",
        "action": "mark",
        "text": new_text,
        "changes": changes,
        "inserted": positions,
        "placeholder": placeholder,
    }


def _load_entry_list(args: dict[str, Any]) -> list[dict[str, Any]]:
    """读取待插入条目：entries（列表）/ entries_file（JSON 文件）二选一。

    只接受经过 lit-verify 核验的条目（verdict=verified）；缺少 verdict 或
    非 verified 的条目一律拒绝。这里不提供人工绕过入口。
    """
    entries = args.get("entries")
    if entries is None and args.get("entries_file"):
        path = Path(str(args["entries_file"]))
        if not path.exists():
            raise ValueError(f"entries_file 不存在: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            entries = payload.get("verified_entries") or payload.get("entries") or []
        else:
            entries = payload
    entries = entries or []
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list) or not entries:
        raise ValueError("insert 模式需要 entries 或 entries_file（lit-verify 的 verified_entries）")
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"条目必须是对象: {entry!r}")
        verdict = str(entry.get("verdict") or "").strip().lower()
        if verdict != "verified":
            raise ValueError(
                f"条目未通过核验（verdict={verdict}），拒绝写入正文：{str(entry.get('title'))[:60]}；"
                "请先用 lit-verify 核验，只有 verdict=verified 的条目才允许写入"
            )
        normalized.append(entry)
    return normalized


def _next_reference_number(text: str) -> int:
    _, _, entries = _analyze_lines(text)
    return max((num for num, _rest, _line in entries), default=0) + 1


def _build_insert_docx_plan(
    paragraphs: list[str] | None,
    anchors_with_markers: list[tuple[str, str]],
    entry_lines: list[str],
) -> tuple[dict[int, dict[str, list[tuple[str, str]]]], dict[int, list[str]]]:
    """正文标记用 token_replacements；新增条目行用 insertions（追加到文末段落之后）。"""
    plan: dict[int, dict[str, list[tuple[str, str]]]] = {}
    insertions: dict[int, list[str]] = {}
    if paragraphs is None:
        return plan, insertions
    for anchor, marker in anchors_with_markers:
        owners = [i for i, paragraph in enumerate(paragraphs) if paragraph.count(anchor) == 1]
        if len(owners) == 1:
            index = owners[0]
            plan.setdefault(index, {"token_replacements": []})["token_replacements"].append((anchor, anchor + marker))
    if entry_lines:
        tail = max((i for i, paragraph in enumerate(paragraphs) if paragraph.strip()), default=len(paragraphs) - 1)
        insertions[tail] = list(entry_lines)
    return plan, insertions


def _insert_mode(args: dict[str, Any], text: str, language: str) -> dict[str, Any]:
    """把已核验文献插入正文标记并追加参考文献条目（默认 IEEE 数字体系）。"""
    entries = _load_entry_list(args)
    style = str(args.get("style") or "ieee").lower()
    anchors = args.get("anchors")
    if isinstance(anchors, str):
        anchors = [item.strip() for item in anchors.split("\n") if item.strip()]
    anchors = list(anchors or [])
    start_number = args.get("start_number")
    next_number = int(start_number) if start_number else _next_reference_number(text)

    new_text = text
    changes: list[str] = []
    inserted: list[dict[str, Any]] = []
    token_pairs: list[tuple[str, str]] = []
    entry_lines: list[str] = []
    entry_formats: list[dict[str, Any]] = []
    anchors_with_markers: list[tuple[str, str]] = []

    for offset, entry in enumerate(entries):
        number = next_number + offset
        anchor = str(entry.get("anchor") or "").strip()
        if not anchor and offset < len(anchors):
            anchor = anchors[offset].strip()
        if not anchor:
            raise ValueError(f"条目缺少 anchor（第 {offset + 1} 条）：{str(entry.get('title'))[:60]}")
        count = new_text.count(anchor)
        if count != 1:
            raise ValueError(f"锚点 {anchor[:40]!r} 出现 {count} 次（要求恰好 1 次），请补充上下文使其唯一")
        marker = f"[{number}]"
        position = new_text.find(anchor) + len(anchor)
        new_text = new_text[:position] + marker + new_text[position:]
        token_pairs.append((anchor, anchor + marker))
        anchors_with_markers.append((anchor, marker))
        inserted.append({"anchor": anchor, "marker": marker, "number": number, "offset": position})
        changes.append(f"正文锚点后插入 {marker}: {anchor[:40]}")

    for offset, entry in enumerate(entries):
        number = next_number + offset
        line = format_reference(entry, style, number)
        entry_lines.append(line)
        entry_formats.append(
            {
                "number": number,
                "title": entry.get("title") or "",
                "formats": format_all_styles(entry),
            }
        )
        changes.append(f"追加参考文献条目 {line[:80]}")

    if entry_lines:
        new_text = new_text.rstrip("\n") + "\n" + "\n".join(entry_lines) + "\n"

    result: dict[str, Any] = {
        "mode": "insert",
        "text": new_text,
        "changes": changes,
        "token_pairs": token_pairs,
        "inserted": inserted,
        "entry_lines": entry_lines,
        "entry_formats": entry_formats,
        "style": style,
        "start_number": next_number,
    }
    formats_out = args.get("formats_out")
    if formats_out and entry_formats:
        lines = ["# 插入文献的多规范引用格式", ""]
        for item in entry_formats:
            lines.append(f"## [{item['number']}] {item['title']}")
            lines.append("")
            for name, formatted in item["formats"].items():
                lines.append(f"- {name}: {formatted}")
            lines.append("")
        Path(str(formats_out)).parent.mkdir(parents=True, exist_ok=True)
        Path(str(formats_out)).write_text("\n".join(lines), encoding="utf-8")
        result["formats_file"] = str(formats_out)
    paragraphs = args.get("paragraphs")
    if paragraphs is not None:
        plan, insertions = _build_insert_docx_plan(list(paragraphs), anchors_with_markers, entry_lines)
        result["docx_plan"] = plan
        result["docx_insertions"] = insertions
    return result


# --------------------------------------------------------------------------- #
# run(args) 契约入口
# --------------------------------------------------------------------------- #


def run(args: dict[str, Any]) -> dict[str, Any]:
    language = normalize_language(str(args.get("language", "en")))
    text = args.get("text")
    text_file = args.get("text_file")
    if text is None and text_file:
        text = Path(text_file).read_text(encoding="utf-8-sig")
    if text is None:
        raise ValueError("cite-manager run() 需要 text_file 或 text")
    mode = str(args.get("mode", "audit")).lower()
    if mode not in VALID_MODES:
        raise ValueError(f"未知 mode: {mode!r}，可选 {VALID_MODES}")
    if mode == "audit":
        result = _audit(text)
    elif mode == "apply":
        result = _apply_mode(args, text, language)
    elif mode == "renumber":
        result = _renumber_mode(args, text)
    elif mode == "insert":
        result = _insert_mode(args, text, language)
    else:
        result = _pending_mode(args, text, language)
    result["skill"] = "cite-manager"
    result["language"] = language
    output = args.get("output")
    if output and result.get("text") is not None and mode != "audit":
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(result["text"], encoding="utf-8")
        result["output"] = str(output)
    return result
