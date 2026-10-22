"""Zotero Word 域交叉引用：审计、清单提取、重新绑定与本地 API 探测。

核心输出保持 Zotero Word 插件可识别的两类域：
- `ADDIN ZOTERO_ITEM CSL_CITATION {...}`：正文引用域；
- `ADDIN ZOTERO_BIBL {...} CSL_BIBLIOGRAPHY`：动态文献表域。

本模块不修改 Zotero 数据库，不读取账号密钥；在线能力仅访问本机
127.0.0.1:23119 的 Zotero 本地 API。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from utils.docx_io import serialize_document_xml

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
CUSTOM_NS = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
VT_NS = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
CSL_CITATION_SCHEMA = (
    "https://github.com/citation-style-language/schema/raw/master/csl-citation.json"
)

CITATION_RE = re.compile(r"\[(\d{1,3}(?:\s*[,，\-–—]\s*\d{1,3})*)\]")
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b")
REFERENCE_HEADING_RE = re.compile(
    r"^\s*(?:references?|bibliography|参\s*考\s*文\s*献|文\s*献)\s*[:：]?\s*$",
    re.IGNORECASE,
)
REFERENCE_LINE_RE = re.compile(r"^\s*\[(\d{1,3})\]")


def _w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def _paragraph_text(paragraph: ET.Element) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        if node.tag == _w("t"):
            parts.append(node.text or "")
        elif node.tag == _w("tab"):
            parts.append("\t")
        elif node.tag in (_w("br"), _w("cr")):
            parts.append("\n")
    return "".join(parts)


def _run_text(run: ET.Element) -> str:
    return "".join(node.text or "" for node in run.iter(_w("t")))


def _run_field_type(run: ET.Element) -> str:
    field = run.find(_w("fldChar"))
    if field is None:
        return ""
    return str(field.get(_w("fldCharType")) or "")


def _run_instruction(run: ET.Element) -> str:
    return "".join(node.text or "" for node in run.iter(_w("instrText")))


def _parse_zotero_instruction(instruction: str) -> dict[str, Any]:
    upper = instruction.upper()
    if "ZOTERO_ITEM" in upper and "CSL_CITATION" in upper:
        kind = "citation"
    elif "ZOTERO_BIBL" in upper and "CSL_BIBLIOGRAPHY" in upper:
        kind = "bibliography"
    else:
        return {"kind": "", "payload": {}}
    brace = instruction.find("{")
    if brace < 0:
        return {"kind": kind, "payload": {}, "parse_error": "缺少 JSON 对象"}
    try:
        payload, _ = json.JSONDecoder().raw_decode(instruction[brace:])
    except json.JSONDecodeError as exc:
        return {"kind": kind, "payload": {}, "parse_error": str(exc)}
    return {"kind": kind, "payload": payload}


def _scan_fields(root: ET.Element) -> dict[str, Any]:
    paragraphs = list(root.iter(_w("p")))
    run_paragraph: dict[int, int] = {}
    for paragraph_index, paragraph in enumerate(paragraphs):
        for run in paragraph.iter(_w("r")):
            run_paragraph[id(run)] = paragraph_index

    fields: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []
    runs = list(root.iter(_w("r")))
    for run_index, run in enumerate(runs):
        field_type = _run_field_type(run)
        if field_type == "begin":
            stack.append(
                {
                    "begin_run_index": run_index,
                    "begin_paragraph": run_paragraph.get(id(run)),
                    "instruction": "",
                    "separate": False,
                }
            )
            continue
        instruction = _run_instruction(run)
        if instruction and stack:
            stack[-1]["instruction"] += instruction
            continue
        if field_type == "separate" and stack:
            stack[-1]["separate"] = True
            continue
        if field_type == "end" and stack:
            field = stack.pop()
            field["end_run_index"] = run_index
            field["end_paragraph"] = run_paragraph.get(id(run))
            parsed = _parse_zotero_instruction(field["instruction"])
            field.update(parsed)
            fields.append(field)
    return {
        "fields": fields,
        "unclosed": stack,
        "paragraphs": paragraphs,
        "runs": runs,
    }


def _custom_properties(payloads: dict[str, bytes]) -> dict[str, str]:
    raw = payloads.get("docProps/custom.xml")
    if not raw:
        return {}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return {}
    values: dict[str, str] = {}
    for prop in root.findall(f"{{{CUSTOM_NS}}}property"):
        name = str(prop.get("name") or "")
        if not name:
            continue
        values[name] = "".join(prop.itertext())
    return values


def _style_from_preferences(preferences: dict[str, str]) -> dict[str, str]:
    raw = preferences.get("ZOTERO_PREF_1", "") + preferences.get("ZOTERO_PREF_2", "")
    style = re.search(r'<style\s+id="([^"]+)"', raw)
    locale = re.search(r'<style[^>]*\blocale="([^"]+)"', raw)
    field_type = re.search(r'<pref\s+name="fieldType"\s+value="([^"]+)"', raw)
    return {
        "style_id": style.group(1) if style else "",
        "locale": locale.group(1) if locale else "",
        "preferred_field_type": field_type.group(1) if field_type else "",
    }


def audit_zotero_docx(path: str | Path) -> dict[str, Any]:
    """审计 DOCX 中 Zotero 引用域、文献表域和文档偏好。"""
    source = Path(path)
    with zipfile.ZipFile(source) as archive:
        payloads = {name: archive.read(name) for name in archive.namelist()}
    xml_bytes = payloads.get("word/document.xml")
    if not xml_bytes:
        raise ValueError("DOCX 缺少 word/document.xml")
    root = ET.fromstring(xml_bytes)
    scan = _scan_fields(root)
    citation_fields = [item for item in scan["fields"] if item.get("kind") == "citation"]
    bibliography_fields = [
        item for item in scan["fields"] if item.get("kind") == "bibliography"
    ]
    preferences = _custom_properties(payloads)
    item_keys: set[str] = set()
    citation_ids: list[str] = []
    parse_errors: list[str] = []
    for field in citation_fields:
        payload = field.get("payload") or {}
        citation_ids.append(str(payload.get("citationID") or ""))
        if field.get("parse_error"):
            parse_errors.append(str(field["parse_error"]))
        for item in payload.get("citationItems") or []:
            if not isinstance(item, dict):
                continue
            uris = [str(value) for value in item.get("uris") or []]
            key = ""
            for uri in uris:
                match = re.search(r"/items/([A-Z0-9]+)$", uri, re.IGNORECASE)
                if match:
                    key = match.group(1)
                    break
            if not key:
                key = str((item.get("itemData") or {}).get("id") or "")
            if key:
                item_keys.add(key)
    malformed = [
        item
        for item in citation_fields + bibliography_fields
        if not item.get("separate")
        or item.get("end_run_index") is None
        or item.get("parse_error")
    ]
    has_preferences = bool(
        preferences.get("ZOTERO_PREF_1") and preferences.get("ZOTERO_PREF_2")
    )
    pass_gate = (
        bool(citation_fields)
        and len(bibliography_fields) == 1
        and not scan["unclosed"]
        and not malformed
        and has_preferences
    )
    return {
        "schema_version": "1.0",
        "path": str(source.resolve()),
        "field_count": len(scan["fields"]),
        "citation_field_count": len(citation_fields),
        "unique_item_count": len(item_keys),
        "unique_item_keys": sorted(item_keys),
        "citation_ids": citation_ids,
        "bibliography_field_count": len(bibliography_fields),
        "unclosed_field_count": len(scan["unclosed"]),
        "malformed_field_count": len(malformed),
        "parse_errors": parse_errors,
        "has_document_preferences": has_preferences,
        "preferences": _style_from_preferences(preferences),
        "pass": pass_gate,
    }


def _expand_number_group(raw: str) -> list[int]:
    numbers: list[int] = []
    for part in re.split(r"\s*[,，]\s*", raw):
        range_match = re.fullmatch(r"(\d{1,3})\s*[\-–—]\s*(\d{1,3})", part)
        if range_match:
            start, end = int(range_match.group(1)), int(range_match.group(2))
            if end < start:
                start, end = end, start
            numbers.extend(range(start, end + 1))
        elif part.strip().isdigit():
            numbers.append(int(part.strip()))
    return numbers


def _numbers_from_display(display: str) -> list[int]:
    numbers: list[int] = []
    for match in CITATION_RE.finditer(display):
        numbers.extend(_expand_number_group(match.group(1)))
    return numbers


def extract_reference_manifest(
    source: str | Path,
    *,
    output: str | Path | None = None,
) -> dict[str, Any]:
    """从已有 Zotero 域 DOCX 提取编号到 Zotero 条目的稳定清单。"""
    source = Path(source)
    with zipfile.ZipFile(source) as archive:
        payloads = {name: archive.read(name) for name in archive.namelist()}
    root = ET.fromstring(payloads["word/document.xml"])
    scan = _scan_fields(root)
    by_number: dict[int, dict[str, Any]] = {}
    conflicts: list[str] = []
    for field in scan["fields"]:
        if field.get("kind") != "citation":
            continue
        payload = field.get("payload") or {}
        properties = payload.get("properties") or {}
        display = str(
            properties.get("formattedCitation")
            or properties.get("plainCitation")
            or ""
        )
        numbers = _numbers_from_display(display)
        items = [
            item for item in payload.get("citationItems") or [] if isinstance(item, dict)
        ]
        if len(numbers) != len(items):
            continue
        for number, item in zip(numbers, items):
            item_data = item.get("itemData") or {}
            uris = [str(value) for value in item.get("uris") or []]
            key = ""
            for uri in uris:
                match = re.search(r"/items/([A-Z0-9]+)$", uri, re.IGNORECASE)
                if match:
                    key = match.group(1)
                    break
            key = key or str(item_data.get("id") or "")
            record = {
                "number": number,
                "key": key,
                "id": item.get("id") or item_data.get("id"),
                "uris": uris,
                "itemData": item_data,
            }
            existing = by_number.get(number)
            if existing:
                old_title = str((existing.get("itemData") or {}).get("title") or "")
                new_title = str(item_data.get("title") or "")
                if existing.get("key") != record.get("key") and old_title != new_title:
                    conflicts.append(
                        f"编号 {number} 同时映射到 {existing.get('key')} / {record.get('key')}"
                    )
                continue
            by_number[number] = record
    preferences = _custom_properties(payloads)
    manifest = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source.resolve()),
        "style": _style_from_preferences(preferences),
        "items": [by_number[number] for number in sorted(by_number)],
        "conflicts": conflicts,
    }
    if output:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return manifest


def load_reference_manifest(source: str | Path) -> dict[str, Any]:
    path = Path(source)
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("Zotero 清单顶层必须是 JSON 对象")
        return payload
    return extract_reference_manifest(path)


def _make_run(run_props: ET.Element | None) -> ET.Element:
    run = ET.Element(_w("r"))
    if run_props is not None:
        run.append(copy.deepcopy(run_props))
    return run


def _field_run(
    run_props: ET.Element | None,
    field_type: str,
) -> ET.Element:
    run = _make_run(run_props)
    ET.SubElement(run, _w("fldChar"), {_w("fldCharType"): field_type})
    return run


def _instruction_run(run_props: ET.Element | None, instruction: str) -> ET.Element:
    run = _make_run(run_props)
    node = ET.SubElement(run, _w("instrText"))
    node.set(XML_SPACE, "preserve")
    node.text = f" {instruction.strip()} "
    return run


def _text_run(run_props: ET.Element | None, text: str) -> ET.Element:
    run = _make_run(run_props)
    node = ET.SubElement(run, _w("t"))
    node.set(XML_SPACE, "preserve")
    node.text = text
    return run


def _citation_field(
    run_props: ET.Element | None,
    *,
    paragraph_index: int,
    display: str,
    numbers: list[int],
    items_by_number: dict[int, dict[str, Any]],
) -> tuple[ET.Element, list[ET.Element]]:
    citation_items = []
    for number in numbers:
        item = items_by_number.get(number)
        if item is None:
            raise ValueError(f"正文引用 [{number}] 缺少 Zotero 条目映射")
        citation_items.append(
            {
                "id": item.get("id"),
                "uris": item.get("uris") or [],
                "itemData": item.get("itemData") or {},
            }
        )
    seed = f"{paragraph_index}|{display}|" + "|".join(
        str(item.get("key") or item.get("id") or "") for item in citation_items
    )
    citation_id = "PP" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
    payload = {
        "citationID": citation_id,
        "properties": {
            "formattedCitation": display,
            "plainCitation": display,
            "noteIndex": 0,
        },
        "citationItems": citation_items,
        "schema": CSL_CITATION_SCHEMA,
    }
    instruction = (
        "ADDIN ZOTERO_ITEM CSL_CITATION "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
    nodes = [
        _field_run(run_props, "begin"),
        _instruction_run(run_props, instruction),
        _field_run(run_props, "separate"),
        _text_run(run_props, display),
        _field_run(run_props, "end"),
    ]
    return nodes[0], nodes


def _replace_span(
    paragraph: ET.Element,
    start: int,
    end: int,
    field_nodes: list[ET.Element],
) -> None:
    text_nodes = list(paragraph.iter(_w("t")))
    offset = 0
    target_node = None
    local_start = 0
    local_end = 0
    for node in text_nodes:
        value = node.text or ""
        next_offset = offset + len(value)
        if offset <= start and end <= next_offset:
            target_node = node
            local_start = start - offset
            local_end = end - offset
            break
        offset = next_offset
    if target_node is None:
        raise ValueError("引用标记跨越多个文本节点，需人工检查 Word run 结构")

    parent_run = None
    for run in paragraph.iter(_w("r")):
        if any(candidate is target_node for candidate in run.iter(_w("t"))):
            parent_run = run
            break
    if parent_run is None:
        raise ValueError("无法定位引用标记所在 Word run")
    run_parent = None
    for candidate in paragraph.iter():
        for child_index, child in enumerate(list(candidate)):
            if child is parent_run:
                run_parent = candidate
                break
        if run_parent is not None:
            break
    if run_parent is None:
        raise ValueError("无法定位引用标记 run 的父节点")
    extra_nodes = [
        node
        for node in list(parent_run)
        if node.tag not in {_w("rPr"), _w("t")}
    ]
    text_children = parent_run.findall(_w("t"))
    if extra_nodes or len(text_children) != 1:
        raise ValueError("引用标记所在 run 含复杂结构，拒绝破坏性重建")

    value = target_node.text or ""
    prefix = value[:local_start]
    suffix = value[local_end:]
    run_props = parent_run.find(_w("rPr"))
    replacement: list[ET.Element] = []
    if prefix:
        replacement.append(_text_run(run_props, prefix))
    replacement.extend(field_nodes)
    if suffix:
        replacement.append(_text_run(run_props, suffix))
    position = list(run_parent).index(parent_run)
    run_parent.remove(parent_run)
    for offset_index, node in enumerate(replacement):
        run_parent.insert(position + offset_index, node)


def _insert_bibliography_field(
    paragraphs: list[ET.Element],
    *,
    heading_index: int,
    run_props: ET.Element | None,
) -> tuple[int, int]:
    reference_indexes = [
        index
        for index in range(heading_index + 1, len(paragraphs))
        if REFERENCE_LINE_RE.match(_paragraph_text(paragraphs[index]))
    ]
    if not reference_indexes:
        raise ValueError("参考文献标题后未找到 [n] 条目")
    first = paragraphs[reference_indexes[0]]
    last = paragraphs[reference_indexes[-1]]
    instruction = (
        'ADDIN ZOTERO_BIBL {"uncited":[],"omitted":[],"custom":[]} '
        "CSL_BIBLIOGRAPHY"
    )
    prefix_nodes = [
        _field_run(run_props, "begin"),
        _instruction_run(run_props, instruction),
        _field_run(run_props, "separate"),
    ]
    insert_at = 1 if first.find(_w("pPr")) is not None else 0
    for offset, node in enumerate(prefix_nodes):
        first.insert(insert_at + offset, node)
    last.append(_field_run(run_props, "end"))
    return reference_indexes[0], reference_indexes[-1]


def _merge_zotero_preferences(
    target_payloads: dict[str, bytes],
    source_payloads: dict[str, bytes],
) -> bool:
    source_values = _custom_properties(source_payloads)
    wanted = {
        name: source_values[name]
        for name in ("ZOTERO_PREF_1", "ZOTERO_PREF_2")
        if source_values.get(name)
    }
    if len(wanted) != 2:
        return False
    raw = target_payloads.get("docProps/custom.xml")
    ET.register_namespace("", CUSTOM_NS)
    ET.register_namespace("vt", VT_NS)
    if raw:
        root = ET.fromstring(raw)
    else:
        root = ET.Element(f"{{{CUSTOM_NS}}}Properties")
    for prop in list(root.findall(f"{{{CUSTOM_NS}}}property")):
        if prop.get("name") in wanted:
            root.remove(prop)
    used_pids = {
        int(prop.get("pid"))
        for prop in root.findall(f"{{{CUSTOM_NS}}}property")
        if str(prop.get("pid") or "").isdigit()
    }
    pid = max(used_pids, default=1) + 1
    for name, value in wanted.items():
        prop = ET.SubElement(
            root,
            f"{{{CUSTOM_NS}}}property",
            {
                "fmtid": "{D5CDD505-2E9C-101B-9397-08002B2CF9AE}",
                "pid": str(pid),
                "name": name,
            },
        )
        text = ET.SubElement(prop, f"{{{VT_NS}}}lpwstr")
        text.text = value
        pid += 1
    target_payloads["docProps/custom.xml"] = ET.tostring(
        root, encoding="utf-8", xml_declaration=True
    )
    return True


def rebind_zotero_fields(
    source: str | Path,
    target: str | Path,
    output: str | Path,
    *,
    manifest: str | Path | None = None,
    report_path: str | Path | None = None,
) -> dict[str, Any]:
    """把目标 DOCX 中的数字引用和文献表重新绑定为 Zotero Word 域。"""
    source = Path(source)
    target = Path(target)
    output = Path(output)
    manifest_payload = (
        load_reference_manifest(manifest) if manifest else extract_reference_manifest(source)
    )
    items = [
        item
        for item in manifest_payload.get("items") or []
        if isinstance(item, dict) and str(item.get("number") or "").isdigit()
    ]
    items_by_number = {int(item["number"]): item for item in items}
    if not items_by_number:
        raise ValueError("Zotero 清单没有任何编号条目")

    with zipfile.ZipFile(target) as archive:
        target_payloads = {name: archive.read(name) for name in archive.namelist()}
    with zipfile.ZipFile(source) as archive:
        source_payloads = {name: archive.read(name) for name in archive.namelist()}
    xml_bytes = target_payloads["word/document.xml"]
    root = ET.fromstring(xml_bytes)
    paragraphs = list(root.iter(_w("p")))
    heading_index = next(
        (
            index
            for index, paragraph in enumerate(paragraphs)
            if REFERENCE_HEADING_RE.match(_paragraph_text(paragraph).strip())
        ),
        -1,
    )
    if heading_index < 0:
        raise ValueError("目标 DOCX 未找到参考文献标题")

    citation_count = 0
    missing_numbers: set[int] = set()
    for paragraph_index, paragraph in enumerate(paragraphs[:heading_index]):
        text = _paragraph_text(paragraph)
        matches = list(CITATION_RE.finditer(text))
        for match in reversed(matches):
            numbers = _expand_number_group(match.group(1))
            missing = [number for number in numbers if number not in items_by_number]
            if missing:
                missing_numbers.update(missing)
                continue
            text_nodes = [
                node
                for node in paragraph.iter(_w("t"))
                if (node.text or "").find(match.group(0)) >= 0
            ]
            run_props = None
            if text_nodes:
                for run in paragraph.iter(_w("r")):
                    if any(node is text_nodes[0] for node in run.iter(_w("t"))):
                        run_props = run.find(_w("rPr"))
                        break
            _, nodes = _citation_field(
                run_props,
                paragraph_index=paragraph_index,
                display=match.group(0),
                numbers=numbers,
                items_by_number=items_by_number,
            )
            _replace_span(paragraph, match.start(), match.end(), nodes)
            citation_count += 1
    if missing_numbers:
        raise ValueError(f"Zotero 清单缺少正文引用编号: {sorted(missing_numbers)}")

    first_ref_index = next(
        (
            index
            for index in range(heading_index + 1, len(paragraphs))
            if REFERENCE_LINE_RE.match(_paragraph_text(paragraphs[index]))
        ),
        -1,
    )
    if first_ref_index < 0:
        raise ValueError("未找到可包裹的参考文献条目")
    first_ref = paragraphs[first_ref_index]
    first_run = next(iter(first_ref.iter(_w("r"))), None)
    first_rpr = first_run.find(_w("rPr")) if first_run is not None else None
    first_reference_paragraph, last_reference_paragraph = _insert_bibliography_field(
        paragraphs,
        heading_index=heading_index,
        run_props=first_rpr,
    )

    preferences_copied = _merge_zotero_preferences(target_payloads, source_payloads)
    if not preferences_copied:
        raise ValueError("源 DOCX 缺少 ZOTERO_PREF_1/2，无法保证 Zotero 可刷新")
    target_payloads["word/document.xml"] = serialize_document_xml(root, xml_bytes)

    output.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output.with_suffix(output.suffix + ".tmp")
    with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in target_payloads.items():
            archive.writestr(name, payload)
    temp_path.replace(output)

    audit = audit_zotero_docx(output)
    reference_texts: dict[int, str] = {}
    for paragraph in paragraphs[first_reference_paragraph : last_reference_paragraph + 1]:
        text = _paragraph_text(paragraph)
        match = REFERENCE_LINE_RE.match(text)
        if match:
            reference_texts[int(match.group(1))] = text
    refresh_risks: list[dict[str, Any]] = []
    for number, item in sorted(items_by_number.items()):
        reference_text = reference_texts.get(number, "")
        field_doi = str((item.get("itemData") or {}).get("DOI") or "").strip()
        line_match = DOI_RE.search(reference_text)
        line_doi = line_match.group(0) if line_match else ""
        if field_doi and line_doi and field_doi.lower() != line_doi.lower():
            refresh_risks.append(
                {
                    "number": number,
                    "key": item.get("key") or "",
                    "code": "zotero_metadata_mismatch",
                    "field_doi": field_doi,
                    "reference_doi": line_doi,
                    "message": "Zotero 域 DOI 与当前参考文献 DOI 不一致，刷新前需先更新 Zotero 条目",
                }
            )
    report = {
        "schema_version": "1.0",
        "source": str(source.resolve()),
        "target": str(target.resolve()),
        "output": str(output.resolve()),
        "manifest_item_count": len(items_by_number),
        "rewritten_citation_count": citation_count,
        "bibliography_paragraphs": {
            "first": first_reference_paragraph,
            "last": last_reference_paragraph,
        },
        "preferences_copied": preferences_copied,
        "refresh_risks": refresh_risks,
        "refresh_ready": bool(audit.get("pass")) and not refresh_risks,
        "audit": audit,
        "pass": bool(audit.get("pass")) and citation_count > 0,
    }
    if report_path:
        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if not report["pass"]:
        raise RuntimeError(f"Zotero 域重新绑定后审计失败: {audit}")
    return report


def probe_zotero_local(
    base_url: str = "http://127.0.0.1:23119",
    *,
    timeout: float = 1.5,
) -> dict[str, Any]:
    """检查 Zotero 本地连接器/API 是否运行。"""
    base = base_url.rstrip("/")
    result: dict[str, Any] = {
        "base_url": base,
        "running": False,
        "connector": False,
        "local_api": False,
        "error": "",
    }
    try:
        request = urllib.request.Request(
            f"{base}/connector/ping",
            headers={"User-Agent": "paper-polish-project/1.0"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result["connector"] = response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        result["error"] = str(exc)
        return result
    try:
        request = urllib.request.Request(
            f"{base}/api/users/0/items?limit=1&format=csljson",
            headers={"User-Agent": "paper-polish-project/1.0"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result["local_api"] = response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        result["error"] = str(exc)
    result["running"] = bool(result["connector"] or result["local_api"])
    return result
