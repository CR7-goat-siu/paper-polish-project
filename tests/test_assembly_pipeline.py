"""装配层端到端护栏测试。

这些用例盯的是"稿件走完 apply 后是否完整"——全块在场、顺序保持、缺块不静默、
润色内容存活、锚点零丢失、重复执行一致。它们不评测润色质量（那由 evaluator /
guardrails 的测试负责），只保证装配这一层不出静默事故。

历史背景（三类已修/新发现的静默事故）：
1. apply_responses 曾只按 chunk_id 归组，batch-processor 作业包（chunk_id 恒 c001）
   被压成 1 块、15 块内容静默消失；
2. 归组键改为只取 parent_chunk_id 后，同一章节的多个子块又被合并成一组、
   只取一个响应——长章节超出 chunk 上限时必然触发，同样静默丢内容；
3. batch-processor 自身按 parent_chunk_id 归组，缺 parent 的作业会被并到空键下，
   只有第一个章节生效。
以上均由 utils/chunk_contract.py 的复合键与差集校验兜住。
"""

import copy
import tempfile
import unittest
from pathlib import Path

from conftest import skill_module

SOURCE = "\n\n".join(
    [
        "第一段：该方法在基准数据集上的准确率达到 94.2%，如表 1 所示 [1]。",
        "第二段：训练过程在 20 轮迭代内保持稳定，学习率设置为 0.01。",
        "第三段：对比实验包含 notch 滤波、ICA 与小波阈值法共 3 种基线方法 [2]。",
        "第四段：在 256 Hz 采样率下，单段推理耗时 8.6 ms，满足实时性要求。",
        "第五段：消融实验表明，多分支结构带来 1.8 dB 的信噪比提升 [3]。",
    ]
)
# 每块一个互不相同的良性改写：保留全部数值与引用编号（可过门禁），
# 且彼此可区分，用于断言"润色后的内容"确实存活且顺序正确。
REPHRASES = ["，收敛性良好", "，未出现发散", "，满足实时性约束", "，与文献报道相符", "，结果可重复"]
CHUNK_TOKENS = 48


def _build(module, text=SOURCE, strength="light", max_chunk_tokens=CHUNK_TOKENS):
    return module.build_jobs(text, strength=strength, max_chunk_tokens=max_chunk_tokens)


def _fill_identity(jobs):
    """恒等响应：每块原样返回，稳定过门禁，用于隔离"装配是否完整"这一问题。"""
    for job in jobs:
        job["response"] = job["chunk_content"]
    return jobs


def _fill_rephrase(jobs):
    """良性改写响应：保留锚点、每块带独特尾巴，用于验证润色内容存活。"""
    for index, job in enumerate(jobs):
        job["response"] = job["chunk_content"] + REPHRASES[index % len(REPHRASES)]
    return jobs


class AssemblyPipelineTests(unittest.TestCase):
    """build → 回填响应 → apply 的装配完整性。"""

    def setUp(self):
        self.module = skill_module("cn-polish")

    def _apply(self, jobs, text=SOURCE, strength="light", max_chunk_tokens=CHUNK_TOKENS):
        return self.module.apply_responses(
            text, jobs, strength=strength, max_chunk_tokens=max_chunk_tokens
        )

    def test_build_produces_multiple_chunks(self):
        built = _build(self.module)
        self.assertGreaterEqual(
            built["chunk_count"], 3, "多段源文本应切出多个块，否则后续断言无意义"
        )

    def test_all_chunks_present_in_output(self):
        built = _build(self.module)
        applied = self._apply(_fill_identity(built["jobs"]))
        missing = [
            job["chunk_id"]
            for job in built["jobs"]
            if job["chunk_content"].strip()[:12] not in applied["polished_text"]
        ]
        self.assertEqual(missing, [], f"以下块的内容未出现在终稿中: {missing}")

    def test_polished_content_survives_assembly(self):
        """润色后的内容（而非仅原文）必须存活。"""
        built = _build(self.module)
        applied = self._apply(_fill_rephrase(built["jobs"]))
        expected = min(len(built["jobs"]), len(REPHRASES))
        survived = sum(1 for phrase in REPHRASES if phrase in applied["polished_text"])
        self.assertEqual(survived, expected, "部分块的润色结果未进入终稿")

    def test_chunk_order_preserved(self):
        built = _build(self.module)
        applied = self._apply(_fill_rephrase(built["jobs"]))
        positions = [
            applied["polished_text"].index(REPHRASES[index % len(REPHRASES)])
            for index in range(len(built["jobs"]))
        ]
        self.assertEqual(positions, sorted(positions), "块顺序在装配后发生错乱")

    def test_missing_job_is_not_silent(self):
        """抽掉一个作业：块内容必须仍在终稿，且必须有显式告警。

        这是"16 块压成 1 块、15 块静默消失"的回归测试。
        """
        built = _build(self.module)
        dropped = [job for job in built["jobs"] if job["chunk_id"] != "c002"]
        applied = self._apply(_fill_identity(dropped))
        lost = [j for j in built["jobs"] if j["chunk_id"] == "c002"][0]["chunk_content"]
        self.assertIn(
            lost.strip()[:12],
            applied["polished_text"],
            "缺失作业对应的块被静默丢弃，终稿内容不完整",
        )
        self.assertTrue(
            any("c002" in warning for warning in applied["warnings"]),
            f"缺块未产生任何告警: {applied['warnings']}",
        )

    def test_multi_sub_chunks_of_one_parent_are_all_kept(self):
        """同一章节的多个子块必须全部装配，不能被合并成一组只取一个响应。"""
        long_section = "\n\n".join(
            [
                f"第 {index} 段：该方法在基准数据集上的准确率达到 9{index}.2%，"
                "如表 1 所示 [1]，训练过程保持稳定。"
                for index in range(4)
            ]
        )
        built = _build(self.module, text=long_section, max_chunk_tokens=60)
        self.assertGreaterEqual(len(built["jobs"]), 2, "长章节应切出多个子块")
        for job in built["jobs"]:
            job["parent_chunk_id"] = "c002"
            job["job_id"] = f"c002-{job['chunk_id']}-a1"
        applied = self._apply(
            _fill_identity(built["jobs"]), text=long_section, max_chunk_tokens=60
        )
        self.assertEqual(applied["chunk_count"], len(built["jobs"]))
        self.assertEqual(
            len(applied["polished_text"]),
            sum(len(job["chunk_content"]) for job in built["jobs"]),
        )
        for job in built["jobs"]:
            self.assertIn(job["chunk_content"].strip()[:15], applied["polished_text"])

    def test_anchor_numbers_survive_assembly(self):
        built = _build(self.module)
        applied = self._apply(_fill_identity(built["jobs"]))
        for anchor in ("94.2", "20", "0.01", "256", "8.6", "1.8"):
            self.assertEqual(
                applied["polished_text"].count(anchor),
                SOURCE.count(anchor),
                f"锚点数值 {anchor} 在装配后数量发生变化",
            )

    def test_apply_is_idempotent(self):
        built = _build(self.module)
        jobs = _fill_identity(built["jobs"])
        first = self._apply(copy.deepcopy(jobs))
        second = self._apply(copy.deepcopy(jobs))
        self.assertEqual(first["polished_text"], second["polished_text"])
        self.assertEqual(first["chunk_reports"], second["chunk_reports"])

    def test_batch_processor_job_shape_is_not_collapsed(self):
        """batch-processor 作业包（chunk_id 恒 c001、身份在 parent_chunk_id）不得被压成一块。"""
        bp_jobs = [
            {
                "job_id": f"c{parent:03d}-c001-a1",
                "chunk_id": "c001",
                "parent_chunk_id": f"c{parent:03d}",
                "chunk_content": f"第 {parent} 段内容，准确率 {90 + parent}.0% [1]。",
                "response": "",
            }
            for parent in range(2, 6)
        ]
        applied = self._apply(_fill_identity(bp_jobs))
        self.assertEqual(applied["chunk_count"], 4)
        for job in bp_jobs:
            self.assertIn(job["chunk_content"][:10], applied["polished_text"])

    def test_foreign_skill_jobs_are_excluded_from_validation(self):
        """混入其它 skill 的作业不应参与块身份校验（否则误报缺块/冲突）。"""
        built = _build(self.module)
        own_count = len(built["jobs"])
        jobs = _fill_identity(list(built["jobs"]))
        jobs.append(
            {
                "job_id": "term-check-x",
                "chunk_id": "c999",
                "skill": "term-check",
                "chunk_content": "完全不同的内容",
                "response": "",
            }
        )
        applied = self._apply(jobs)
        self.assertNotIn("term-check-x", str(applied["chunk_reports"]))
        self.assertEqual(applied["chunk_count"], own_count)


class BatchProcessorAssemblyTests(unittest.TestCase):
    """batch-processor 自身的装配路径（经 run_skill 间接调用 apply_responses）。"""

    PARAS = [
        "第一段：该方法在基准数据集上的准确率达到 94.2%，如表 1 所示 [1]。",
        "第二段：训练过程在 20 轮迭代内保持稳定，学习率设置为 0.01。",
    ]

    def _parsed(self):
        title = "测试标题"
        body = "\n\n".join(self.PARAS)
        return {
            "title": title,
            "full_text": f"{title}\n\n{body}",
            "integrity": {"reconstructs": True},
            "chunks": [
                {"id": "c001", "kind": "title", "heading": title, "content": title},
                {"id": "c002", "kind": "section", "heading": "引言", "content": self.PARAS[0]},
                {"id": "c003", "kind": "section", "heading": "方法", "content": self.PARAS[1]},
            ],
        }

    def _jobs(self, parsed):
        jobs = []
        for chunk in parsed["chunks"]:
            if chunk["kind"] == "title":
                continue
            jobs.append(
                {
                    "job_id": f"{chunk['id']}-c001-a1",
                    "chunk_id": "c001",
                    "parent_chunk_id": chunk["id"],
                    "skill": "cn-polish",
                    "chunk_content": chunk["content"],
                    "response": chunk["content"],
                }
            )
        return jobs

    def _run(self, parsed, jobs):
        module = skill_module("batch-processor")
        with tempfile.TemporaryDirectory() as tmp:
            assembled, result, retries = module._run_polish_apply(
                "cn-polish",
                parsed=parsed,
                jobs=jobs,
                language="cn",
                strength="light",
                out_dir=Path(tmp),
                extra={},
            )
        return assembled, result, retries

    def test_all_sections_assembled(self):
        parsed = self._parsed()
        assembled, result, _ = self._run(parsed, self._jobs(parsed))
        self.assertEqual(result["chunk_count"], 2)
        for para in self.PARAS:
            self.assertIn(para, assembled)
        self.assertIn(parsed["title"], assembled)

    def test_missing_parent_chunk_id_is_rejected(self):
        """缺 parent_chunk_id 的作业会被并到空键、只有第一章生效——必须显式报错。"""
        parsed = self._parsed()
        jobs = self._jobs(parsed)
        for job in jobs:
            job.pop("parent_chunk_id")
        with self.assertRaises(ValueError) as ctx:
            self._run(parsed, jobs)
        self.assertIn("parent_chunk_id", str(ctx.exception))

    def test_job_pointing_to_unknown_section_is_rejected(self):
        parsed = self._parsed()
        jobs = self._jobs(parsed)
        jobs[0]["parent_chunk_id"] = "c999"
        with self.assertRaises(ValueError) as ctx:
            self._run(parsed, jobs)
        self.assertIn("c999", str(ctx.exception))


class ChunkContractTests(unittest.TestCase):
    """契约层单元测试。"""

    def test_normalize_job_key_is_composite(self):
        from utils import chunk_contract

        self.assertEqual(
            chunk_contract.normalize_job_key({"chunk_id": "c001", "parent_chunk_id": "c007"}),
            "c007/c001",
        )
        self.assertEqual(chunk_contract.normalize_job_key({"chunk_id": "c003"}), "c003")
        self.assertEqual(chunk_contract.normalize_job_key({"parent_chunk_id": "c007"}), "c007")
        self.assertEqual(chunk_contract.normalize_job_key({}), "")

    def test_normalize_job_key_separates_sub_chunks(self):
        """同一章节的两个子块必须得到不同键——这是多子块不合并的关键。"""
        from utils import chunk_contract

        keys = {
            chunk_contract.normalize_job_key(
                {"chunk_id": sub, "parent_chunk_id": "c002", "chunk_content": f"内容{sub}"}
            )
            for sub in ("c001", "c002")
        }
        self.assertEqual(len(keys), 2)

    def test_chunk_sort_key_is_numeric_and_two_level(self):
        """按各段数值排序；真实作业包要么全裸键、要么全复合键，不会混合。"""
        from utils import chunk_contract

        bare = ["c10", "c2", "c001", "c020"]
        self.assertEqual(
            sorted(bare, key=chunk_contract.chunk_sort_key), ["c001", "c2", "c10", "c020"]
        )
        composite = ["c002/c010", "c002/c002", "c010/c001", "c002/c001"]
        self.assertEqual(
            sorted(composite, key=chunk_contract.chunk_sort_key),
            ["c002/c001", "c002/c002", "c002/c010", "c010/c001"],
        )

    def test_validate_job_set_detects_conflicting_content(self):
        from utils import chunk_contract

        problems = chunk_contract.validate_job_set(
            [
                {"job_id": "a", "chunk_id": "c001", "parent_chunk_id": "c002", "chunk_content": "甲"},
                {"job_id": "b", "chunk_id": "c001", "parent_chunk_id": "c002", "chunk_content": "乙"},
            ]
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("c002/c001", problems[0])

    def test_validate_job_set_allows_multiple_attempts(self):
        """同一块的多次尝试内容相同，不算冲突。"""
        from utils import chunk_contract

        problems = chunk_contract.validate_job_set(
            [
                {"job_id": "a", "chunk_id": "c001", "chunk_content": "同一内容", "attempt": 1},
                {"job_id": "b", "chunk_id": "c001", "chunk_content": "同一内容", "attempt": 2},
            ]
        )
        self.assertEqual(problems, [])

    def test_validate_job_set_detects_empty_key(self):
        from utils import chunk_contract

        problems = chunk_contract.validate_job_set([{"job_id": "a", "chunk_content": "甲"}])
        self.assertEqual(len(problems), 1)
        self.assertIn("缺少块标识", problems[0])

    def test_diff_against_expected(self):
        from utils import chunk_contract

        missing, unexpected = chunk_contract.diff_against_expected(
            ["c001", "c003"], ["c001", "c002", "c003", "c004"]
        )
        self.assertEqual(missing, ["c002", "c004"])
        self.assertEqual(unexpected, [])
        missing, unexpected = chunk_contract.diff_against_expected(["c001", "c009"], ["c001"])
        self.assertEqual(missing, [])
        self.assertEqual(unexpected, ["c009"])

    def test_chunk_ref_key_and_sort(self):
        from utils import chunk_contract

        ref = chunk_contract.ChunkRef(parent_chunk_id="c002", chunk_id="c010", content="内容")
        self.assertEqual(ref.key, "c002/c010")
        self.assertEqual(ref.sort_key, (2, 10))
        refs = chunk_contract.to_chunk_refs(
            [{"parent_chunk_id": "c002", "chunk_id": "c001", "chunk_content": "甲"}]
        )
        self.assertEqual(refs[0].key, "c002/c001")
        self.assertEqual(refs[0].content, "甲")

    def test_verify_assembly_flags_short_output(self):
        from utils import chunk_contract

        errors, warnings = chunk_contract.verify_assembly(
            ["短"], ["c001"], source_text="x" * 1000, polished_text="短"
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("截断", warnings[0])

    def test_verify_assembly_flags_count_mismatch(self):
        from utils import chunk_contract

        errors, _ = chunk_contract.verify_assembly(
            ["a"], ["c001", "c002"], source_text="ab", polished_text="ab"
        )
        self.assertEqual(len(errors), 1)

    def test_expected_chunk_map_matches_build(self):
        from utils import chunk_contract

        built = _build(skill_module("cn-polish"))
        expected = chunk_contract.expected_chunk_map(SOURCE, CHUNK_TOKENS)
        self.assertEqual(
            sorted(expected), sorted(job["chunk_id"] for job in built["jobs"])
        )


if __name__ == "__main__":
    unittest.main()
