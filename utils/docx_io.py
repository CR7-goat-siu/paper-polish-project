"""DOCX 读写工具（纯标准库：zipfile + xml.etree）。

read_paragraphs：按文档顺序抽取段落（样式 + 文本），供 paper-parser 做结构化切分。
write_polished_docx：将润色后的段落文本按索引写回源 docx，尽量保留原段落样式。
create_minimal_docx：构造最小合法 docx，供测试与样例使用。

约束：不依赖 python-docx 等第三方库；写出失败由调用方回退为纯文本输出。
"""

from __future__ import annotations

import copy
import os
import re
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from utils.logger import get_logger

logger = get_logger("docx-io")

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_MATH = "{" + M_NS + "}"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""

_HEADING_STYLE_RE = re.compile(r"(?i)^(?:heading|标题)\s*([1-6])$")
_TITLE_STYLE_RE = re.compile(r"(?i)^(?:title|标题)$")
_REFERENCES_RE = re.compile(r"^\s*(references|bibliography|参\s*考\s*文\s*献|文\s*献)\s*$", re.IGNORECASE)
_ABSTRACT_RE = re.compile(r"^\s*(abstract|摘\s*要)\s*[:：]?\s*$", re.IGNORECASE)

# 真实世界 docx 常用纯文本编号标题（如 “1 摘要”“3.1生成器”“3.1.1编码器”）。
# 约束：编号段 1-2 位数字、总长 ≤ 46、不含句内标点结尾，避免把 "2019年提出" 这类正文误判为标题。
_NUMBERED_HEADING_RE = re.compile(r"^\s*\d{1,2}(?:\.\d{1,2}){0,3}\s*[\u4e00-\u9fffA-Za-z（(].{0,44}$")
_BARE_HEADING_RE = re.compile(
    r"^\s*(方法|实验|结果|讨论|结论|引言|相关工作|材料与方法|数据分析|实验结果)\s*$"
)
_SENTENCE_ENDINGS = ("。", "！", "？", "；", ".", "!", "?", ";", "，", ",", "：", ":")

# 根元素上的 xmlns 前缀声明（w:、mc:、w14: 等）
_XMLNS_DECL_RE = re.compile(r'xmlns:([A-Za-z0-9_.\-]+)="([^"]*)"')


def _register_document_namespaces(xml_bytes: bytes) -> None:
    """收集源文档全部 xmlns 前缀声明并注册（含深层元素的局部声明）。

    ElementTree 默认把前缀改写为 ns0:/ns1:，而 mc:Ignorable="w14 wp14" 的值
    按名字引用前缀——前缀改名后引用悬空，Word 会报“发现无法读取的内容”
    （WPS 宽容，Word 严格）。必须在 tostring 之前注册。
    """
    import io as _io

    try:
        for _, (prefix, uri) in ET.iterparse(
            _io.BytesIO(xml_bytes), events=("start-ns",)
        ):
            if prefix:
                ET.register_namespace(prefix, uri)
    except ET.ParseError:
        pass


def _reinject_root_decls(out_xml: bytes, source_xml_bytes: bytes) -> bytes:
    """把源根元素有、输出根缺失的 xmlns 声明补注回输出根。

    ElementTree 会丢弃树中未使用的命名空间声明，但 mc:Ignorable 的值仍按
    名字引用它们，缺失同样会让 Word 报无法读取的内容。
    """
    def _root_decls(data: bytes) -> tuple[str, list[tuple[str, str]]]:
        text = data.decode("utf-8")
        start = text.find("<")
        end = text.find(">", text.find("?>") + 2) if text.startswith("<?xml") else text.find(">")
        return text[: end + 1], _XMLNS_DECL_RE.findall(text[start : end + 1])

    out_head, _ = _root_decls(out_xml)
    _, src_decls = _root_decls(source_xml_bytes)
    missing = [
        (prefix, uri)
        for prefix, uri in src_decls
        if f'xmlns:{prefix}="' not in out_head
    ]
    if not missing:
        return out_xml
    inject = " " + " ".join(f'xmlns:{p}="{u}"' for p, u in missing)
    return (
        out_head[:-1].encode("utf-8")
        + inject.encode("utf-8")
        + b">"
        + out_xml[len(out_head):]
    )


def serialize_document_xml(root: ET.Element, source_xml_bytes: bytes) -> bytes:
    """用源文档的命名空间前缀把 document 树序列化回 bytes（Word 兼容）。"""
    _register_document_namespaces(source_xml_bytes)
    out = ET.tostring(root, encoding="UTF-8", xml_declaration=True)
    return _reinject_root_decls(out, source_xml_bytes)


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


def _paragraph_style(paragraph: ET.Element) -> str:
    p_pr = paragraph.find(_w("pPr"))
    if p_pr is None:
        return ""
    style = p_pr.find(_w("pStyle"))
    if style is None:
        return ""
    return str(style.get(_w("val"), "") or "")


def read_paragraphs(path: str | Path) -> list[dict[str, Any]]:
    """按文档顺序读取段落：[{index, style, text}]。"""
    path = Path(path)
    with zipfile.ZipFile(path) as archive:
        xml_bytes = archive.read("word/document.xml")
    root = ET.fromstring(xml_bytes)
    paragraphs: list[dict[str, Any]] = []
    for index, paragraph in enumerate(root.iter(_w("p"))):
        paragraphs.append(
            {
                "index": index,
                "style": _paragraph_style(paragraph),
                "text": _paragraph_text(paragraph),
            }
        )
    return paragraphs


def read_tables(path: str | Path) -> list[dict[str, Any]]:
    """读取 DOCX 表格的行列文本，供统计登记与跨表一致性检查使用。"""
    with zipfile.ZipFile(Path(path)) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    all_paragraphs = list(root.iter(_w("p")))
    paragraph_indexes = {id(paragraph): index for index, paragraph in enumerate(all_paragraphs)}
    tables: list[dict[str, Any]] = []
    for table_index, table in enumerate(root.iter(_w("tbl"))):
        rows: list[list[str]] = []
        table_paragraph_indexes: list[int] = []
        for row in table.findall(_w("tr")):
            cells = []
            for cell in row.findall(_w("tc")):
                cells.append(_wt_concat(cell).strip())
                table_paragraph_indexes.extend(
                    paragraph_indexes[id(paragraph)]
                    for paragraph in cell.iter(_w("p"))
                    if id(paragraph) in paragraph_indexes
                )
            rows.append(cells)
        tables.append(
            {
                "index": table_index,
                "rows": rows,
                "paragraph_indexes": sorted(set(table_paragraph_indexes)),
            }
        )
    return tables


def parse_paragraph_kinds(paragraphs: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """按样式与内容推断每个段落的 (kind, heading)。

    kind 取值与 paper-parser 的 chunk kind 对齐：
    title / abstract / section / body / references。
    """
    in_references = False
    in_abstract = False
    kinds: list[tuple[str, str]] = []
    for paragraph in paragraphs:
        text = paragraph["text"].strip()
        style = paragraph["style"]
        heading_match = _HEADING_STYLE_RE.match(style)
        is_title = bool(_TITLE_STYLE_RE.match(style))
        if _REFERENCES_RE.match(text):
            in_references = True
            in_abstract = False
            kinds.append(("references", text))
            continue
        if in_references:
            kinds.append(("references", ""))
            continue
        if _ABSTRACT_RE.match(text) or (in_abstract and not heading_match and not is_title and text):
            if _ABSTRACT_RE.match(text):
                in_abstract = True
                kinds.append(("abstract", text))
            else:
                kinds.append(("abstract", ""))
            continue
        if heading_match:
            in_abstract = False
            kinds.append(("section", text))
            continue
        if is_title:
            kinds.append(("title", text))
            continue
        if _looks_like_text_heading(text):
            in_abstract = False
            kinds.append(("section", text))
            continue
        kinds.append(("body", ""))
    return kinds


def _looks_like_text_heading(text: str) -> bool:
    """无样式 docx 的文本标题启发式：编号标题或裸章节词。"""
    if not text or len(text) > 46 or text.endswith(_SENTENCE_ENDINGS):
        return False
    if _BARE_HEADING_RE.match(text):
        return True
    return bool(_NUMBERED_HEADING_RE.match(text))


def _wt_nodes(paragraph: ET.Element) -> list[ET.Element]:
    """按文档顺序收集段落内的 w:t 文本节点（m:oMath 内是 m:t，天然不受影响）。"""
    return [node for node in paragraph.iter() if node.tag == _w("t")]


def _wt_concat(paragraph: ET.Element) -> str:
    return "".join(node.text or "" for node in _wt_nodes(paragraph))


def _splice_texts(texts: list[str], start: int, end: int, replacement: str) -> bool:
    """在跨 run 拼接文本的 [start, end) 区间上做替换，回写各节点文本。

    替换串只会落在第一个相交节点；其余相交节点让出被覆盖的部分。
    返回是否发生了相交（即是否找到目标区间）。
    """
    pos = 0
    placed = False
    for index, text in enumerate(texts):
        node_start, node_end = pos, pos + len(text)
        pos = node_end
        if node_end <= start or node_start >= end:
            continue
        local_start = max(start - node_start, 0)
        local_end = min(end - node_start, len(text))
        head, tail = text[:local_start], text[local_end:]
        if not placed:
            texts[index] = head + replacement + tail
            placed = True
        else:
            texts[index] = head + tail
    return placed


def _apply_token_replacements(
    paragraph: ET.Element, pairs: list[tuple[str, str]], paragraph_index: int
) -> str:
    """外科式替换：只改 w:t 文本，不动 run 结构、公式（oMath）与图形对象。

    适用于"段落整体不能重写、只需改引用编号等 token"的场景。
    严格模式：任一 token 在本段找不到即抛错，避免重排漏改造成编号错位。
    返回替换后的 w:t 拼接文本（供回读校验比对）。
    """
    nodes = _wt_nodes(paragraph)
    texts = [node.text or "" for node in nodes]
    original = "".join(texts)
    missing = [old for old, _ in pairs if old and old not in original]
    if missing:
        raise ValueError(
            f"段落 {paragraph_index} 外科替换失败，未找到待替换 token: {missing}；"
            "请按段落实际文本构造 token_replacements"
        )
    for old, new in pairs:
        if not old:
            continue
        search_from = 0
        # 从上次替换点之后继续查找，避免新文本包含旧 token 时死循环
        while True:
            concat = "".join(texts)
            pos = concat.find(old, search_from)
            if pos < 0:
                break
            _splice_texts(texts, pos, pos + len(old), str(new))
            search_from = pos + len(str(new))
    for node, text in zip(nodes, texts):
        if node.text != text:
            node.text = text
            node.set(XML_SPACE, "preserve")
    return "".join(texts)


def _normalize_token_pairs(spec: Any, paragraph_index: int) -> list[tuple[str, str]]:
    """token_replacements 支持列表 [(old, new)] 或映射 {old: new}（映射按长度降序）。"""
    if isinstance(spec, dict):
        items = sorted(spec.items(), key=lambda kv: len(str(kv[0])), reverse=True)
        return [(str(k), str(v)) for k, v in items]
    pairs: list[tuple[str, str]] = []
    for item in spec or []:
        if isinstance(item, dict):
            old, new = item.get("old", ""), item.get("new", "")
        else:
            old, new = item[0], item[1]
        pairs.append((str(old), str(new)))
    if not pairs:
        raise ValueError(f"段落 {paragraph_index} 的 token_replacements 为空")
    return pairs


def _check_readback(
    paragraphs: list[ET.Element],
    expected_by_index: dict[int, str],
    stage: str,
) -> None:
    """回读校验：实际段落文本必须与期望逐字符相等，乱串/重复拼接在库层面报错。"""
    for index, expected in expected_by_index.items():
        if index < 0 or index >= len(paragraphs):
            raise ValueError(f"回读校验（{stage}）失败：段落索引越界 {index}")
        actual = _wt_concat(paragraphs[index])
        if actual != expected:
            raise ValueError(
                f"回读校验（{stage}）失败：段落 {index} 写入文本与期望不一致。"
                f"期望前 80 字符: {expected[:80]!r}；实际前 80 字符: {actual[:80]!r}"
            )


def write_polished_docx(
    source_path: str | Path,
    target_path: str | Path,
    replacements: dict[int, Any],
    insertions: dict[int, list[str]] | None = None,
) -> bool:
    """把 {paragraph_index: new_text} 写回 docx，内置回读校验。

    段落新文本支持三种形式：
    - str：整段替换（单 run）；
    - [(text, fmt)]：整段替换（多 run，fmt="superscript" 设上标）；
    - {"token_replacements": [(old, new), ...]}：外科式替换——保留段落全部
      子节点（run 结构、公式 oMath、图形、批注锚点），只在 w:t 文本内做
      token 级替换；适合含公式段落上只改引用编号的场景。

    仅替换有改动的段落；空白对空白（空换空）视为未改动。任何异常抛给调用方
    处理（由其回退纯文本）。写入前后各做一次回读比对：序列化后反解析比对一次，
    落盘后从磁盘重读再比对一次，失败即抛错并删除损坏的目标文件。
    """
    source_path = Path(source_path)
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source_path) as archive:
        names = archive.namelist()
        xml_bytes = archive.read("word/document.xml")
        payloads = {name: archive.read(name) for name in names}

    root = ET.fromstring(xml_bytes)
    paragraphs = list(root.iter(_w("p")))
    expected_by_index: dict[int, str] = {}
    for index, new_text in replacements.items():
        if index < 0 or index >= len(paragraphs):
            raise IndexError(f"段落索引越界: {index}（共 {len(paragraphs)} 段）")
        paragraph = paragraphs[index]
        if isinstance(new_text, dict):
            # 外科式替换：不动段落结构，只改 w:t 内的 token
            pairs = _normalize_token_pairs(
                new_text.get("token_replacements"), index
            )
            expected_by_index[index] = _apply_token_replacements(paragraph, pairs, index)
            continue
        flat = new_text if isinstance(new_text, str) else "".join(t for t, _ in new_text)
        if not flat.strip() and not _paragraph_text(paragraph).strip():
            continue
        first_run = paragraph.find(_w("r"))
        if first_run is None:
            for hyperlink in paragraph.iter(_w("hyperlink")):
                first_run = hyperlink.find(_w("r"))
                if first_run is not None:
                    break
        run_props = None
        if first_run is not None:
            props = first_run.find(_w("rPr"))
            if props is not None:
                run_props = copy.deepcopy(props)
        # 整体清空策略：仅保留段落属性，其余子节点（含 w:ins/smartTag/sdt 等
        # 包装节点内部的旧 run）全部移除——按标签枚举会漏掉嵌套包装，导致
        # 旧文本残留与新文本拼接成乱串。含公式/图形对象时拒绝替换并报错。
        for child in list(paragraph):
            if child.tag == _w("pPr"):
                continue
            if child.tag in (_MATH + "oMath", _MATH + "oMathPara", _w("drawing"), _w("object"), _w("pict")):
                raise ValueError(f"段落 {index} 含公式/图形对象，拒绝整段文本替换；请改用 token_replacements 外科式替换")
            paragraph.remove(child)
        # 段落文本支持两种形式：str（单 run）；[(text, fmt)] 多 run，fmt="superscript" 设上标
        segments = new_text if isinstance(new_text, list) else [(str(new_text), "plain")]
        for seg_text, fmt in segments:
            run = ET.SubElement(paragraph, _w("r"))
            if run_props is not None:
                run.append(copy.deepcopy(run_props))
            if fmt == "superscript":
                r_pr = run.find(_w("rPr"))
                if r_pr is None:
                    r_pr = ET.Element(_w("rPr"))
                    run.insert(0, r_pr)
                vert = ET.SubElement(r_pr, _w("vertAlign"))
                vert.set(_w("val"), "superscript")
            text_node = ET.SubElement(run, _w("t"))
            text_node.set(XML_SPACE, "preserve")
            text_node.text = str(seg_text)
        expected_by_index[index] = flat

    if insertions:
        # 在指定段落后插入新段落（复制锚点段落的 pPr 与 run 字符格式）；
        # 仅支持 body 直接子级段落（正文段落），表格内的锚点不支持。
        # 插入会改变段落总数，因此只对"被替换段落"做回读比对（按原索引定位）。
        body = root.find(_w("body"))
        children = list(body)
        for after_index in sorted(insertions, reverse=True):
            anchor = paragraphs[after_index]
            pos = children.index(anchor)
            new_els = []
            for text in insertions[after_index]:
                np_ = ET.Element(_w("p"))
                p_pr = anchor.find(_w("pPr"))
                if p_pr is not None:
                    np_.append(copy.deepcopy(p_pr))
                r = ET.SubElement(np_, _w("r"))
                a_run = anchor.find(_w("r"))
                if a_run is not None and a_run.find(_w("rPr")) is not None:
                    r.append(copy.deepcopy(a_run.find(_w("rPr"))))
                t = ET.SubElement(r, _w("t"))
                t.set(XML_SPACE, "preserve")
                t.text = str(text)
                new_els.append(np_)
            for el in reversed(new_els):
                children.insert(pos + 1, el)
        body[:] = children

    # 回读比对按插入后的段落位置进行：锚点之后的所有原段落整体后移
    def _post_insertion_position(index: int) -> int:
        shift = 0
        for anchor in sorted(insertions or {}):
            if anchor < index:
                shift += len(insertions[anchor])
        return index + shift

    expected_by_position = {
        _post_insertion_position(index): expected
        for index, expected in expected_by_index.items()
    }

    document_xml = serialize_document_xml(root, xml_bytes)
    # 回读校验第一道：序列化 → 反解析 → 比对（写入磁盘前拦截树级错误）
    _check_readback(
        list(ET.fromstring(document_xml).iter(_w("p"))), expected_by_position, "serialize"
    )
    tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in payloads.items():
            if name == "word/document.xml":
                archive.writestr(name, document_xml)
            else:
                archive.writestr(name, payload)
    # 回读校验第二道：从临时文件重读 → 比对（拦截序列化/压缩层错误）
    try:
        with zipfile.ZipFile(tmp_path) as archive:
            disk_root = ET.fromstring(archive.read("word/document.xml"))
        _check_readback(list(disk_root.iter(_w("p"))), expected_by_position, "disk")
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    try:
        _replace_with_retry(tmp_path, target_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return True


def normalize_text_colors(
    source_path: str | Path,
    target_path: str | Path,
    *,
    color: str = "000000",
    remove_highlight: bool = True,
) -> Path:
    """统一正文、页眉和页脚中的文字颜色，生成干净候选稿。

    保留 run 结构、字体、加粗、上下标和公式；只修改 w:color / w:highlight。
    """
    source_path = Path(source_path)
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    color = color.lstrip("#").upper()
    with zipfile.ZipFile(source_path) as archive:
        payloads = {name: archive.read(name) for name in archive.namelist()}
    story_parts = [
        name
        for name in payloads
        if name == "word/document.xml"
        or re.fullmatch(r"word/(header|footer)\d+\.xml", name)
    ]
    for name in story_parts:
        source_payload = payloads[name]
        root = ET.fromstring(source_payload)
        for color_node in root.iter(_w("color")):
            color_node.set(_w("val"), color)
            for attr in ("themeColor", "themeTint", "themeShade"):
                key = _w(attr)
                if key in color_node.attrib:
                    del color_node.attrib[key]
        if remove_highlight:
            for parent in root.iter():
                for highlight in list(parent.findall(_w("highlight"))):
                    parent.remove(highlight)
        payloads[name] = serialize_document_xml(root, source_payload)
    temp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in payloads.items():
            archive.writestr(name, payload)
    os.replace(temp_path, target_path)
    return target_path


def repeat_table_headers_and_prevent_row_split(
    source_path: str | Path,
    target_path: str | Path,
) -> dict[str, Any]:
    """让表首行跨页重复，并禁止任一表格行在分页处被拆断。

    该操作只补 `w:tblHeader` / `w:cantSplit` 行属性，不改变单元格文本、
    列宽或边框。函数可重复执行且不会产生重复属性。
    """
    source_path = Path(source_path)
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source_path) as archive:
        payloads = {name: archive.read(name) for name in archive.namelist()}
    xml_bytes = payloads["word/document.xml"]
    root = ET.fromstring(xml_bytes)

    def ensure_row_property(row: ET.Element, tag: str) -> None:
        tr_pr = row.find(_w("trPr"))
        if tr_pr is None:
            tr_pr = ET.Element(_w("trPr"))
            row.insert(0, tr_pr)
        elif row[0] is not tr_pr:
            row.remove(tr_pr)
            row.insert(0, tr_pr)
        if tr_pr.find(_w(tag)) is None:
            ET.SubElement(tr_pr, _w(tag))

    table_count = 0
    header_count = 0
    row_count = 0
    for table in root.iter(_w("tbl")):
        rows = table.findall(_w("tr"))
        if not rows:
            continue
        table_count += 1
        for row in rows:
            ensure_row_property(row, "cantSplit")
            row_count += 1
        ensure_row_property(rows[0], "tblHeader")
        header_count += 1

    payloads["word/document.xml"] = serialize_document_xml(root, xml_bytes)
    temp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in payloads.items():
            archive.writestr(name, payload)
    os.replace(temp_path, target_path)
    return {
        "path": str(target_path.resolve()),
        "table_count": table_count,
        "header_count": header_count,
        "row_count": row_count,
    }


def remove_trailing_empty_paragraphs(
    source_path: str | Path,
    target_path: str | Path,
) -> dict[str, Any]:
    """只移除正文末尾无内容的普通空段落，防止生成空白尾页。

    含编号、分页符、公式、图形、对象、字段或节属性的段落一律保留。
    """
    source_path = Path(source_path)
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source_path) as archive:
        payloads = {name: archive.read(name) for name in archive.namelist()}
    xml_bytes = payloads["word/document.xml"]
    root = ET.fromstring(xml_bytes)
    body = root.find(_w("body"))
    if body is None:
        raise ValueError("DOCX 缺少 w:body")

    protected_tags = {
        _w("br"),
        _w("drawing"),
        _w("object"),
        _w("pict"),
        _w("fldSimple"),
        _MATH + "oMath",
        _MATH + "oMathPara",
    }

    def is_safe_blank(paragraph: ET.Element) -> bool:
        if _paragraph_text(paragraph).strip():
            return False
        if any(node.tag in protected_tags for node in paragraph.iter()):
            return False
        p_pr = paragraph.find(_w("pPr"))
        if p_pr is not None and any(
            p_pr.find(_w(tag)) is not None
            for tag in ("numPr", "pageBreakBefore", "sectPr")
        ):
            return False
        return True

    removable: list[ET.Element] = []
    for child in reversed(list(body)):
        if child.tag == _w("sectPr"):
            continue
        if child.tag != _w("p") or not is_safe_blank(child):
            break
        removable.append(child)
    for paragraph in removable:
        body.remove(paragraph)

    payloads["word/document.xml"] = serialize_document_xml(root, xml_bytes)
    temp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in payloads.items():
            archive.writestr(name, payload)
    os.replace(temp_path, target_path)
    return {
        "path": str(target_path.resolve()),
        "removed_count": len(removable),
    }


def sanitize_docx_field_artifacts(
    source_path: str | Path,
    target_path: str | Path,
) -> dict[str, Any]:
    """移除 Zotero 文献与引用字段指令，保留字段结果文本。"""
    markers = ("ADDIN ZOTERO_", "CSL_BIBLIOGRAPHY", "CSL_CITATION")
    source_path = Path(source_path)
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source_path) as archive:
        payloads = {name: archive.read(name) for name in archive.namelist()}
    xml_bytes = payloads["word/document.xml"]
    root = ET.fromstring(xml_bytes)
    removed_instructions = 0
    removed_control_runs = 0

    for paragraph in root.iter(_w("p")):
        children = list(paragraph)
        remove_indexes: set[int] = set()
        for index, child in enumerate(children):
            if child.tag != _w("r"):
                continue
            instruction = "".join(
                node.text or "" for node in child.iter(_w("instrText"))
            )
            upper = instruction.upper()
            if not instruction or not any(marker in upper for marker in markers):
                continue
            remove_indexes.add(index)
            removed_instructions += 1

            previous = index - 1
            while previous >= 0 and children[previous].tag == _w("r"):
                field_char = children[previous].find(_w("fldChar"))
                if field_char is not None and field_char.get(_w("fldCharType")) == "begin":
                    remove_indexes.add(previous)
                    break
                visible = "".join(
                    node.text or ""
                    for node in children[previous].iter(_w("t"), _w("instrText"))
                ).strip()
                if visible:
                    break
                previous -= 1

            following = index + 1
            while following < len(children) and children[following].tag == _w("r"):
                field_char = children[following].find(_w("fldChar"))
                if field_char is not None and field_char.get(_w("fldCharType")) in {
                    "separate",
                    "end",
                }:
                    remove_indexes.add(following)
                    break
                visible = "".join(
                    node.text or ""
                    for node in children[following].iter(_w("t"), _w("instrText"))
                ).strip()
                if visible:
                    break
                following += 1

        for index in sorted(remove_indexes, reverse=True):
            field_char = children[index].find(_w("fldChar"))
            if field_char is not None:
                removed_control_runs += 1
            paragraph.remove(children[index])

    payloads["word/document.xml"] = serialize_document_xml(root, xml_bytes)
    temp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in payloads.items():
            archive.writestr(name, payload)
    os.replace(temp_path, target_path)
    return {
        "path": str(target_path.resolve()),
        "removed_instruction_count": removed_instructions,
        "removed_control_run_count": removed_control_runs,
    }


def normalize_docx_terms(
    source_path: str | Path,
    target_path: str | Path,
    replacements: dict[str, str],
) -> dict[str, Any]:
    """在保留 run、公式和表格结构的前提下归一显式术语映射。"""
    source_path = Path(source_path)
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    pairs = [(str(old), str(new)) for old, new in replacements.items() if str(old)]
    pairs.sort(key=lambda item: len(item[0]), reverse=True)
    with zipfile.ZipFile(source_path) as archive:
        payloads = {name: archive.read(name) for name in archive.namelist()}
    xml_bytes = payloads["word/document.xml"]
    root = ET.fromstring(xml_bytes)
    counts = {old: 0 for old, _ in pairs}
    for text_node in root.iter(_w("t")):
        value = text_node.text or ""
        for old, new in pairs:
            count = value.count(old)
            if count:
                value = value.replace(old, new)
                counts[old] += count
        if value != (text_node.text or ""):
            text_node.text = value
    payloads["word/document.xml"] = serialize_document_xml(root, xml_bytes)
    temp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in payloads.items():
            archive.writestr(name, payload)
    os.replace(temp_path, target_path)
    return {
        "path": str(target_path.resolve()),
        "replacement_count": sum(counts.values()),
        "by_term": counts,
    }


def set_heading_outline_levels(
    source_path: str | Path,
    target_path: str | Path,
) -> dict[str, Any]:
    """按章节编号给手工标题补 Word outline level，不改变现有直接格式。"""
    source_path = Path(source_path)
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source_path) as archive:
        payloads = {name: archive.read(name) for name in archive.namelist()}
    xml_bytes = payloads["word/document.xml"]
    root = ET.fromstring(xml_bytes)
    counts = {"level_1": 0, "level_2": 0, "level_3": 0}
    for paragraph in root.iter(_w("p")):
        text = _paragraph_text(paragraph).strip()
        level = None
        if (
            re.match(r"^\d+\s+\S", text)
            or text in {"参考文献", "References", "Bibliography"}
            or text in {"摘要", "Abstract"}
        ):
            level = 0
        elif re.match(r"^\d+\.\d+(?![\d.])\s*(?=[A-Za-z\u4e00-\u9fff])", text):
            level = 1
        elif re.match(
            r"^\d+\.\d+\.\d+(?![\d.])\s*(?=[A-Za-z\u4e00-\u9fff])",
            text,
        ):
            level = 2
        if level is None:
            continue
        p_pr = paragraph.find(_w("pPr"))
        if p_pr is None:
            p_pr = ET.Element(_w("pPr"))
            paragraph.insert(0, p_pr)
        if p_pr.find(_w("numPr")) is not None:
            prefix_match = re.match(r"^\s*\d+(?:\.\d+){0,2}\s+", text)
            if prefix_match:
                remaining = len(prefix_match.group(0))
                for text_node in paragraph.iter(_w("t")):
                    value = text_node.text or ""
                    take = min(remaining, len(value))
                    if take:
                        text_node.text = value[take:]
                        remaining -= take
                    if remaining <= 0:
                        break
        for old in list(p_pr.findall(_w("outlineLvl"))):
            p_pr.remove(old)
        ET.SubElement(p_pr, _w("outlineLvl"), {_w("val"): str(level)})
        counts[f"level_{level + 1}"] += 1
    payloads["word/document.xml"] = serialize_document_xml(root, xml_bytes)
    temp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in payloads.items():
            archive.writestr(name, payload)
    os.replace(temp_path, target_path)
    return {"path": str(target_path.resolve()), **counts}


def materialize_mteqn_fields(source_path: str | Path, target_path: str | Path) -> dict[str, Any]:
    """把 SEQ MTEqn 公式编号字段替换为字段缓存中已有的普通文本编号。"""
    source_path = Path(source_path)
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source_path) as archive:
        payloads = {name: archive.read(name) for name in archive.namelist()}
    xml_bytes = payloads["word/document.xml"]
    root = ET.fromstring(xml_bytes)
    materialized = 0
    for paragraph in root.iter(_w("p")):
        children = list(paragraph)
        rebuilt: list[ET.Element] = []
        index = 0
        while index < len(children):
            child = children[index]
            begin = child.find(_w("fldChar")) if child.tag == _w("r") else None
            is_begin = (
                begin is not None
                and begin.attrib.get(_w("fldCharType")) == "begin"
            )
            if not is_begin:
                rebuilt.append(child)
                index += 1
                continue
            end_index = index
            depth = 0
            instructions = ""
            display_parts: list[str] = []
            while end_index < len(children):
                current = children[end_index]
                field_char = current.find(_w("fldChar")) if current.tag == _w("r") else None
                if field_char is not None:
                    field_type = field_char.attrib.get(_w("fldCharType"))
                    if field_type == "begin":
                        depth += 1
                    elif field_type == "end":
                        depth -= 1
                for node in current.iter(_w("instrText")):
                    value = node.text or ""
                    instructions += value
                    if "MACROBUTTON" not in value and "SEQ" not in value:
                        display_parts.append(value)
                for node in current.iter(_w("t")):
                    display_parts.append(node.text or "")
                if depth == 0:
                    break
                end_index += 1
            display_text = "".join(display_parts)
            match = re.search(r"\(\d+\.\d+\)", display_text)
            if (
                "MACROBUTTON MTPlaceRef" in instructions
                and "SEQ MTEqn" in instructions
                and match
                and end_index < len(children)
            ):
                new_run = ET.Element(_w("r"))
                source_run = next(
                    (
                        node
                        for node in children[index : end_index + 1]
                        if node.tag == _w("r") and node.find(_w("t")) is not None
                    ),
                    child,
                )
                r_pr = source_run.find(_w("rPr"))
                if r_pr is not None:
                    new_run.append(copy.deepcopy(r_pr))
                text_node = ET.SubElement(new_run, _w("t"))
                text_node.text = match.group(0)
                rebuilt.append(new_run)
                materialized += 1
                index = end_index + 1
                continue
            rebuilt.extend(children[index : end_index + 1])
            index = end_index + 1
        paragraph[:] = rebuilt
    payloads["word/document.xml"] = serialize_document_xml(root, xml_bytes)
    temp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in payloads.items():
            archive.writestr(name, payload)
    os.replace(temp_path, target_path)
    return {"path": str(target_path.resolve()), "materialized_count": materialized}


def preserve_inline_math_paragraphs(
    original_path: str | Path,
    candidate_path: str | Path,
    target_path: str | Path,
) -> dict[str, Any]:
    """把候选稿中含内嵌公式对象的段落回退为原始段落。

    模型通常看不到 OLE/OMML 行内对象，整段替换会造成变量丢失。这里只回退
    “有正文文本且含内嵌对象”的段落，独立公式段落不处理。
    """
    original_path = Path(original_path)
    candidate_path = Path(candidate_path)
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(original_path) as archive:
        source_payload = archive.read("word/document.xml")
    with zipfile.ZipFile(candidate_path) as archive:
        candidate_payload = archive.read("word/document.xml")
        payloads = {name: archive.read(name) for name in archive.namelist()}
    source_root = ET.fromstring(source_payload)
    candidate_root = ET.fromstring(candidate_payload)
    source_paragraphs = list(source_root.iter(_w("p")))
    candidate_paragraphs = list(candidate_root.iter(_w("p")))
    if len(source_paragraphs) != len(candidate_paragraphs):
        raise ValueError(
            f"段落数不一致，不能回退内嵌公式段落: original={len(source_paragraphs)}, "
            f"candidate={len(candidate_paragraphs)}"
        )
    parent_map = {
        child: parent
        for parent in candidate_root.iter()
        for child in parent
    }
    preserved = 0
    indices: list[int] = []
    for index, (source_para, candidate_para) in enumerate(
        zip(source_paragraphs, candidate_paragraphs)
    ):
        has_math = any(
            node.tag in {_w("object"), _w("pict"), _MATH + "oMath", _MATH + "oMathPara"}
            for node in source_para.iter()
        )
        has_text = len(_paragraph_text(source_para).strip()) >= 8
        if not (has_math and has_text):
            continue
        parent = parent_map.get(candidate_para)
        if parent is None:
            continue
        position = list(parent).index(candidate_para)
        parent.remove(candidate_para)
        parent.insert(position, copy.deepcopy(source_para))
        preserved += 1
        indices.append(index)
    payloads["word/document.xml"] = serialize_document_xml(
        candidate_root,
        candidate_payload,
    )
    temp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in payloads.items():
            archive.writestr(name, payload)
    os.replace(temp_path, target_path)
    return {
        "path": str(target_path.resolve()),
        "preserved_count": preserved,
        "paragraph_indexes": indices,
    }


def compact_reference_paragraphs(
    source_path: str | Path,
    target_path: str | Path,
    *,
    line_twips: int = 240,
) -> dict[str, Any]:
    """压缩参考文献条目段前段后距并统一单倍行距，避免末页大面积留白。"""
    source_path = Path(source_path)
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source_path) as archive:
        payloads = {name: archive.read(name) for name in archive.namelist()}
    xml_bytes = payloads["word/document.xml"]
    root = ET.fromstring(xml_bytes)
    count = 0
    for paragraph in root.iter(_w("p")):
        text = _paragraph_text(paragraph).strip()
        if not re.match(r"^\[\d+\]", text):
            continue
        p_pr = paragraph.find(_w("pPr"))
        if p_pr is None:
            p_pr = ET.Element(_w("pPr"))
            paragraph.insert(0, p_pr)
        spacing = p_pr.find(_w("spacing"))
        if spacing is None:
            spacing = ET.SubElement(p_pr, _w("spacing"))
        spacing.set(_w("before"), "0")
        spacing.set(_w("after"), "0")
        spacing.set(_w("line"), str(line_twips))
        spacing.set(_w("lineRule"), "auto")
        count += 1
    payloads["word/document.xml"] = serialize_document_xml(root, xml_bytes)
    temp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in payloads.items():
            archive.writestr(name, payload)
    os.replace(temp_path, target_path)
    return {"path": str(target_path.resolve()), "compacted_count": count}


def dedupe_docx_members(source_path: str | Path, target_path: str | Path) -> dict[str, Any]:
    """去除 DOCX ZIP 重名部件，并清理指向不存在批注文件的失效关系。"""
    source_path = Path(source_path)
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source_path) as archive:
        infos = archive.infolist()
        payloads: dict[str, bytes] = {}
        for info in infos:
            payloads[info.filename] = archive.read(info.filename)
    removed_duplicates = len(infos) - len(payloads)
    rels_name = "word/_rels/document.xml.rels"
    if "word/comments.xml" not in payloads and rels_name in payloads:
        root = ET.fromstring(payloads[rels_name])
        changed = False
        for node in list(root):
            if node.get("Type") == COMMENTS_REL_TYPE or node.get("Target") == "comments.xml":
                root.remove(node)
                changed = True
        if changed:
            payloads[rels_name] = (
                COMMENTS_XML_DECL.encode("utf-8")
                + ET.tostring(root, encoding="utf-8", xml_declaration=False)
            )
    content_types = "[Content_Types].xml"
    if "word/comments.xml" not in payloads and content_types in payloads:
        root = ET.fromstring(payloads[content_types])
        changed = False
        for node in list(root):
            if node.get("PartName") == "/word/comments.xml":
                root.remove(node)
                changed = True
        if changed:
            payloads[content_types] = (
                COMMENTS_XML_DECL.encode("utf-8")
                + ET.tostring(root, encoding="utf-8", xml_declaration=False)
            )
    with zipfile.ZipFile(target_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in payloads.items():
            archive.writestr(name, payload)
    return {
        "source": str(source_path.resolve()),
        "target": str(target_path.resolve()),
        "removed_duplicate_entries": removed_duplicates,
        "member_count": len(payloads),
    }


COMMENTS_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
COMMENTS_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
COMMENTS_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


def _replace_with_retry(source: Path, target: Path, *, attempts: int = 8) -> None:
    """Windows 上目标文件可能被 Office/杀软短暂占用，原子替换需短重试。"""
    last_error: PermissionError | None = None
    for attempt in range(max(1, attempts)):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(min(0.05 * (2**attempt), 0.8))
    if last_error is not None:
        raise last_error


def _comment_paragraph(text: str, comment_id: int, author: str, initials: str, date: str) -> ET.Element:
    """构造 comments.xml 中的 w:comment 元素（含 annotationRef 占位，Word UI 必需）。"""
    comment = ET.Element(_w("comment"), {_w("id"): str(comment_id), _w("author"): author, _w("initials"): initials, _w("date"): date})
    paragraph = ET.SubElement(comment, _w("p"))
    p_pr = ET.SubElement(paragraph, _w("pPr"))
    ET.SubElement(p_pr, _w("pStyle"), {_w("val"): "CommentText"})
    ref_run = ET.SubElement(paragraph, _w("r"))
    ref_pr = ET.SubElement(ref_run, _w("rPr"))
    ET.SubElement(ref_pr, _w("rStyle"), {_w("val"): "CommentReference"})
    ET.SubElement(ref_run, _w("annotationRef"))
    text_run = ET.SubElement(paragraph, _w("r"))
    text_node = ET.SubElement(text_run, _w("t"))
    text_node.text = text
    text_node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    return comment


def add_comments(
    target_path: str | Path,
    comments: dict[int, list[str]],
    *,
    author: str = "paper-polish guardrails",
    initials: str = "PP",
) -> bool:
    """在 docx 中为指定段落写入 Word 审阅批注（纯标准库，OOXML 最小四要素）。

    comments: {paragraph_index: [批注文本, ...]}，paragraph_index 为
    read_paragraphs 的顺序下标。已存在 comments.xml 时追加（id 取 max+1）。

    OOXML 结构（ECMA-376 §17.13.4 / MS-OI29500 已核实）：
    - word/comments.xml：w:comment（w:id/w:author/w:initials/w:date）
    - document.xml：段首 w:commentRangeStart、段尾 w:commentRangeEnd
      + w:r/w:commentReference（三处 id 一致）
    - word/_rels/document.xml.rels：comments 关系
    - [Content_Types].xml：/word/comments.xml Override
    扩展部件（commentsExtended/Ids/Extensible）为可选，不写也能被 Word/WPS 打开。
    """
    target_path = Path(target_path)
    if not comments:
        return False
    with zipfile.ZipFile(target_path) as archive:
        names = archive.namelist()
        payloads = {name: archive.read(name) for name in names}
        if "word/document.xml" not in payloads:
            raise ValueError(f"不是有效的 docx（缺少 document.xml）: {target_path}")

    document_bytes = payloads["word/document.xml"]
    _register_document_namespaces(document_bytes)
    root = ET.fromstring(document_bytes)
    paragraphs = list(root.iter(_w("p")))
    date = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    # 收集已有批注 id 与既有 comments.xml 根
    existing_ids: set[int] = set()
    if "word/comments.xml" in payloads:
        comments_root = ET.fromstring(payloads["word/comments.xml"])
        for node in comments_root.iter(_w("comment")):
            try:
                existing_ids.add(int(str(node.get(_w("id")))))
            except ValueError:
                continue
    else:
        comments_root = ET.Element(_w("comments"))
        ET.register_namespace("w", W_NS)

    next_id = max(existing_ids, default=-1) + 1
    applied = 0
    for paragraph_index, texts in sorted(comments.items()):
        if not (0 <= paragraph_index < len(paragraphs)):
            logger.warning("批注目标段落下标越界: %s（共 %d 段）", paragraph_index, len(paragraphs))
            continue
        paragraph = paragraphs[paragraph_index]
        for text in texts:
            comment_id = next_id
            next_id += 1
            start = ET.Element(_w("commentRangeStart"), {_w("id"): str(comment_id)})
            paragraph.insert(0, start)
            end = ET.Element(_w("commentRangeEnd"), {_w("id"): str(comment_id)})
            paragraph.append(end)
            ref_run = ET.Element(_w("r"))
            ET.SubElement(ref_run, _w("commentReference"), {_w("id"): str(comment_id)})
            paragraph.append(ref_run)
            comments_root.append(
                _comment_paragraph(str(text), comment_id, author, initials, date)
            )
            applied += 1
    if not applied:
        return False

    document_xml = serialize_document_xml(root, document_bytes)
    payloads["word/document.xml"] = document_xml
    ET.register_namespace("w", W_NS)
    payloads["word/comments.xml"] = (
        COMMENTS_XML_DECL.encode("utf-8")
        + ET.tostring(comments_root, encoding="utf-8", xml_declaration=False)
    )

    # 关系注册（document.xml.rels；缺失则创建——真实 Word 文档必有，最小构造可能没有）
    rels_name = "word/_rels/document.xml.rels"
    if rels_name in payloads:
        rels_root = ET.fromstring(payloads[rels_name])
    else:
        rels_root = ET.Element("Relationships")
    existing_targets = {
        node.get("Target") for node in rels_root if node.get("Target") == "comments.xml"
    }
    if not existing_targets:
        used = {
            str(node.get("Id"))
            for node in rels_root
            if str(node.get("Id") or "").startswith("rId")
        }
        new_id = f"rId{len(used) + 1}"
        while new_id in used:
            new_id = f"rId{int(new_id[3:]) + 1}"
        ET.SubElement(
            rels_root,
            "Relationship",
            {"Id": new_id, "Type": COMMENTS_REL_TYPE, "Target": "comments.xml"},
        )
        payloads[rels_name] = (
            COMMENTS_XML_DECL.encode("utf-8")
            + ET.tostring(rels_root, encoding="utf-8", xml_declaration=False)
        )
        if rels_name not in names:
            names.append(rels_name)

    # 内容类型注册（[Content_Types].xml）
    ct_name = "[Content_Types].xml"
    if ct_name in payloads:
        ct_root = ET.fromstring(payloads[ct_name])
        overrides = {
            node.get("PartName") for node in ct_root if node.tag.endswith("}Override")
        }
        if "/word/comments.xml" not in overrides:
            ET.SubElement(
                ct_root,
                "Override",
                {"PartName": "/word/comments.xml", "ContentType": COMMENTS_CONTENT_TYPE},
            )
            payloads[ct_name] = (
                COMMENTS_XML_DECL.encode("utf-8")
                + ET.tostring(ct_root, encoding="utf-8", xml_declaration=False)
            )

    tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.writestr(name, payloads[name])
        if "word/comments.xml" not in names:
            archive.writestr("word/comments.xml", payloads["word/comments.xml"])
    try:
        _replace_with_retry(tmp_path, target_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    # 回读校验：read_comments 必须能读回刚写入的批注
    try:
        read_back = read_comments(target_path)
    except Exception as exc:  # noqa: BLE001 - 回读失败即视为写入损坏
        raise ValueError(f"批注回读校验失败: {exc}") from exc
    if len(read_back) < applied:
        raise ValueError(f"批注回读数量不足: 写入 {applied} 条，读回 {len(read_back)} 条")
    return True


def read_comments(path: str | Path, paragraphs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """读取 docx 批注（审阅 Comments）及其锚定位置。

    返回 [{id, author, date, text, paragraph_indexes, anchor_text}]；
    无批注部件时返回 []。锚定以 commentRangeStart/End 覆盖的段落为准。
    """
    path = Path(path)
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if "word/comments.xml" not in names:
            return []
        comments_root = ET.fromstring(archive.read("word/comments.xml"))
        doc_root = ET.fromstring(archive.read("word/document.xml"))

    comments: dict[str, dict[str, Any]] = {}
    for node in comments_root.iter(_w("comment")):
        comment_id = str(node.get(_w("id"), ""))
        text = "".join(t.text or "" for t in node.iter(_w("t")))
        comments[comment_id] = {
            "id": comment_id,
            "author": str(node.get(_w("author"), "") or ""),
            "date": str(node.get(_w("date"), "") or ""),
            "text": text.strip(),
            "paragraph_indexes": [],
            "anchor_text": "",
        }

    # 线性扫描 document.xml：commentRangeStart/End 激活区间，区间内 w:t 即锚定文本。
    active: set[str] = set()
    anchored_text: dict[str, list[str]] = {}
    touched_paragraphs: dict[str, list[int]] = {}
    paragraph_index = -1
    for node in doc_root.iter():
        tag = node.tag
        if tag == _w("p"):
            paragraph_index += 1
            continue
        if tag == _w("commentRangeStart"):
            comment_id = str(node.get(_w("id"), ""))
            if comment_id in comments:
                active.add(comment_id)
                touched_paragraphs.setdefault(comment_id, [])
                if paragraph_index not in touched_paragraphs[comment_id]:
                    touched_paragraphs[comment_id].append(paragraph_index)
        elif tag == _w("commentRangeEnd"):
            comment_id = str(node.get(_w("id"), ""))
            if comment_id in comments and paragraph_index not in touched_paragraphs.get(comment_id, []):
                touched_paragraphs.setdefault(comment_id, []).append(paragraph_index)
            active.discard(comment_id)
        elif tag == _w("commentReference"):
            comment_id = str(node.get(_w("id"), ""))
            if comment_id in comments and paragraph_index not in touched_paragraphs.get(comment_id, []):
                touched_paragraphs.setdefault(comment_id, []).append(paragraph_index)
        elif tag == _w("t") and active:
            for comment_id in active:
                anchored_text.setdefault(comment_id, []).append(node.text or "")

    for comment_id, comment in comments.items():
        comment["paragraph_indexes"] = touched_paragraphs.get(comment_id, [])
        comment["anchor_text"] = "".join(anchored_text.get(comment_id, [])).strip()
    if paragraphs is not None:
        for comment in comments.values():
            snippets = []
            for index in comment["paragraph_indexes"]:
                if 0 <= index < len(paragraphs):
                    snippet = paragraphs[index]["text"].strip()
                    if snippet:
                        snippets.append(snippet)
            if not comment["anchor_text"] and snippets:
                comment["anchor_text"] = snippets[0][:80]
    ordered = sorted(comments.values(), key=lambda c: c["paragraph_indexes"] or [0])
    return ordered


def create_minimal_docx(path: str | Path, paragraphs: list[tuple[str, str]]) -> Path:
    """构造最小合法 docx。paragraphs 为 [(style, text)]，style 可为空。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body_parts: list[str] = []
    for style, text in paragraphs:
        p_pr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        body_parts.append(f"<w:p>{p_pr}<w:r><w:t xml:space=\"preserve\">{escaped}</w:t></w:r></w:p>")
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(body_parts)}<w:sectPr/></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("word/document.xml", document)
    return path


# --------------------------------------------------------------------------- #
# Markdown → DOCX 交付装配（optimizer-loop deliver 阶段用）
# --------------------------------------------------------------------------- #

_MD_STYLES_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>
"""

_MD_STYLES_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""

_MD_DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>
"""

_MD_STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults><w:rPrDefault><w:rPr>
    <w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="SimSun"/>
    <w:sz w:val="24"/><w:szCs w:val="24"/>
  </w:rPr></w:rPrDefault></w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
  <w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/>
    <w:pPr><w:jc w:val="center"/><w:outlineLvl w:val="0"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="32"/><w:szCs w:val="32"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>
    <w:pPr><w:keepNext/><w:outlineLvl w:val="0"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="28"/><w:szCs w:val="28"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/>
    <w:pPr><w:keepNext/><w:outlineLvl w:val="1"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="26"/><w:szCs w:val="26"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:basedOn w:val="Normal"/>
    <w:pPr><w:keepNext/><w:outlineLvl w:val="2"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="24"/><w:szCs w:val="24"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Caption"><w:name w:val="Caption"/><w:basedOn w:val="Normal"/>
    <w:pPr><w:jc w:val="center"/></w:pPr><w:rPr><w:i/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="TableText"><w:name w:val="Table Text"/><w:basedOn w:val="Normal"/>
    <w:rPr><w:rFonts w:ascii="Courier New" w:hAnsi="Courier New"/><w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr></w:style>
</w:styles>
"""

_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_MD_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_MD_CAPTION_RE = re.compile(r"^\s*(?:图|表|Figure|Fig\.|Table)\s*\d+", re.IGNORECASE)


def _md_paragraphs(text: str) -> list[tuple[str, str]]:
    """Markdown 文本 → [(style, text)]。已知限制（交付时如实记录）：
    表格按行以等宽样式呈现而非 Word 原生表格；公式/引用块按原文保留。"""
    paragraphs: list[tuple[str, str]] = []
    title_used = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        heading = _MD_HEADING_RE.match(stripped)
        if heading:
            level = len(heading.group(1))
            content = heading.group(2).strip()
            if level == 1 and not title_used:
                paragraphs.append(("Title", content))
                title_used = True
            else:
                paragraphs.append((f"Heading{min(level, 3)}", content))
            continue
        if stripped.startswith("|"):
            if _MD_TABLE_SEP_RE.match(stripped):
                continue
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            paragraphs.append(("TableText", "  |  ".join(cells)))
            continue
        if _MD_CAPTION_RE.match(stripped) and len(stripped) < 200:
            paragraphs.append(("Caption", stripped))
            continue
        paragraphs.append(("", stripped))
    return paragraphs


def create_docx_from_markdown(path: str | Path, text: str) -> dict[str, Any]:
    """把 Markdown 全文装配为交付用 DOCX（标题/章节/图表题注样式齐全）。

    返回 {docx_path, paragraph_count, table_row_count, limitations}。
    写出失败抛异常，由调用方回退为纯文本交付。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    items = _md_paragraphs(text)
    body_parts: list[str] = []
    for style, content in items:
        p_pr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        escaped = (
            content.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        body_parts.append(
            f"<w:p>{p_pr}<w:r><w:t xml:space=\"preserve\">{escaped}</w:t></w:r></w:p>"
        )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(body_parts)}<w:sectPr>"
        '<w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" '
        'w:bottom="1440" w:left="1440"/>'
        "</w:sectPr></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _MD_STYLES_CONTENT_TYPES)
        archive.writestr("_rels/.rels", _MD_STYLES_RELS)
        archive.writestr("word/_rels/document.xml.rels", _MD_DOC_RELS)
        archive.writestr("word/styles.xml", _MD_STYLES_XML)
        archive.writestr("word/document.xml", document)
    return {
        "docx_path": str(path),
        "paragraph_count": len(items),
        "table_row_count": sum(1 for style, _ in items if style == "TableText"),
        "limitations": [
            "Markdown 表格按行以等宽文本呈现（非 Word 原生表格）",
            "公式与引用块按原文文本保留",
        ],
    }
