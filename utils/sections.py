"""章节类型识别：根据章节标题推断 section_type，供润色 Prompt 使用 section_guidance。

规则文件 config/*-rules.yaml 的 section_guidance 键即为本模块的合法返回值：
title / abstract / keywords / introduction / related_work / method /
experiment / discussion / conclusion；无法识别时返回空字符串。
"""

from __future__ import annotations

import re

# 按优先级排列（摘要 / 关键词靠前，避免 "summary" 撞车 "conclusion"）。
_KEYWORDS: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        "abstract": ("abstract", "summary of contributions"),
        "keywords": ("keywords", "key words", "index terms"),
        "related_work": ("related work", "literature review", "prior work", "background survey"),
        "introduction": ("introduction", "motivation", "background"),
        "conclusion": ("conclusion", "concluding remarks", "closing remarks"),
        "experiment": (
            "experiment",
            "evaluation",
            "results",
            "ablation",
            "implementation",
            "experimental setup",
            "case study",
        ),
        "method": (
            "method",
            "methodology",
            "approach",
            "proposed",
            "model",
            "framework",
            "algorithm",
            "architecture",
            "preliminaries",
            "problem formulation",
            "materials and methods",
        ),
        "discussion": ("discussion", "analysis", "limitations"),
    },
    "cn": {
        "abstract": ("摘要", "内容提要"),
        "keywords": ("关键词", "关键字", "索引词"),
        "related_work": ("相关工作", "研究现状", "文献综述", "国内外研究"),
        "introduction": ("引言", "绪论", "研究背景", "背景"),
        "conclusion": ("结论", "结束语", "总结", "结语", "展望"),
        "experiment": ("实验", "试验", "评估", "结果", "性能测试", "仿真", "案例分析", "消融"),
        "method": (
            "方法",
            "模型",
            "算法",
            "网络结构",
            "体系结构",
            "设计",
            "理论基础",
            "问题定义",
            "预备知识",
        ),
        "discussion": ("讨论", "分析与讨论", "局限性"),
    },
}

_INDEX_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)*|[IVXLC]{1,5}\.\s|第\s*\d+\s*[章讲节部分])\s*")


def classify_section(heading: str, language: str) -> str:
    """根据标题文本返回章节类型；无标题或无法识别返回 ""。"""
    if not heading:
        return ""
    text = _INDEX_RE.sub("", heading.strip()).strip().lower()
    if not text:
        return ""
    table = _KEYWORDS.get("en" if language == "en" else "cn", {})
    for section_type, keywords in table.items():
        for keyword in keywords:
            if keyword in text:
                return section_type
    return ""
