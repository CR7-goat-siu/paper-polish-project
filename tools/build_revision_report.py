"""对比两个 DOCX，生成逐段修改对照表与机器可读 JSON。"""

from __future__ import annotations

import difflib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.differ import diff_stats, similarity_ratio  # noqa: E402
from utils.docx_io import read_paragraphs  # noqa: E402


def build_revision_report(
    before: Path,
    after: Path,
    out_md: Path,
    out_json: Path,
    *,
    title: str,
) -> dict[str, Any]:
    source = read_paragraphs(before)
    target = read_paragraphs(after)
    changes: list[dict[str, Any]] = []
    source_texts = [str(item.get("text") or "") for item in source]
    target_texts = [str(item.get("text") or "") for item in target]
    matcher = difflib.SequenceMatcher(None, source_texts, target_texts, autojunk=False)

    def add_change(
        change_type: str,
        source_item: dict[str, Any] | None,
        target_item: dict[str, Any] | None,
    ) -> None:
        old_text = str((source_item or {}).get("text") or "")
        new_text = str((target_item or {}).get("text") or "")
        changes.append(
            {
                "change_type": change_type,
                "index": int((target_item or source_item or {}).get("index", 0)),
                "source_index": (
                    int(source_item["index"]) if source_item is not None else None
                ),
                "target_index": (
                    int(target_item["index"]) if target_item is not None else None
                ),
                "source_style": (source_item or {}).get("style", ""),
                "target_style": (target_item or {}).get("style", ""),
                "similarity": (
                    round(similarity_ratio(old_text, new_text), 4)
                    if old_text and new_text
                    else 0.0
                ),
                "diff": diff_stats(old_text, new_text),
                "before": old_text,
                "after": new_text,
            }
        )

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":
            for source_item in source[i1:i2]:
                add_change("delete", source_item, None)
            continue
        if tag == "insert":
            for target_item in target[j1:j2]:
                add_change("insert", None, target_item)
            continue
        paired = min(i2 - i1, j2 - j1)
        for offset in range(paired):
            add_change("replace", source[i1 + offset], target[j1 + offset])
        for source_item in source[i1 + paired : i2]:
            add_change("delete", source_item, None)
        for target_item in target[j1 + paired : j2]:
            add_change("insert", None, target_item)

    result = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "title": title,
        "before": str(before.resolve()),
        "after": str(after.resolve()),
        "paragraph_count": len(source),
        "source_paragraph_count": len(source),
        "target_paragraph_count": len(target),
        "changed_paragraph_count": len(changes),
        "changes": changes,
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        f"# {title}",
        "",
        f"- 修改前：`{before}`",
        f"- 修改后：`{after}`",
        f"- 修改前段落数：{len(source)}",
        f"- 修改后段落数：{len(target)}",
        f"- 修改段落数：{len(changes)}",
        "",
        "## 修改索引",
        "",
        "| 类型 | 段落 | 相似度 | 前 60 字 |",
        "| --- | --- | --- | --- |",
    ]
    for change in changes:
        preview = change["after"].replace("\n", " ")[:60]
        lines.append(
            f"| {change['change_type']} | {change['index']} | "
            f"{change['similarity']:.4f} | {preview} |"
        )
    lines.extend(["", "## 逐段对照", ""])
    for change in changes:
        lines.extend(
            [
                f"### 段落 {change['index']}",
                "",
                f"- 类型：{change['change_type']}",
                f"- 相似度：{change['similarity']:.4f}",
                f"- 原词数/新词数：{change['diff']['source_words']} / {change['diff']['polished_words']}",
                "",
                "**修改前**",
                "",
                change["before"],
                "",
                "**修改后**",
                "",
                change["after"],
                "",
            ]
        )
    out_md.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return result
