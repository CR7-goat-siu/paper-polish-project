# 测试稿件埋点清单（Planted Flaws Checklist）

> 配套文件：
> - `cn-eeg-denoising-draft.md`（中文测试稿，3195 汉字）
> - `en-eeg-denoising-draft.md`（英文测试稿，3007 词）
>
> 两稿同题（多分支特征生成对抗网络用于脑电信号去噪，对应 biomedical-eeg + deep-learning 领域），
> 均为"研究生初稿"风格：结构完整但刻意埋入下列缺陷。**坑点只存在于稿件中，本清单不参与润色。**

## A. 客观性守卫（claim-guard / hype + informal 词库 / review-gate hype_residue）

| # | 坑点 | 中文稿位置 | 英文稿位置 |
| --- | --- | --- | --- |
| A1 | "首次提出" | 摘要、引言贡献(1) | "for the first time in history"（引言贡献） |
| A2 | "完美地去除" | 摘要 | "perfectly removes all kinds of noise"（摘要） |
| A3 | "极大地提升" | 摘要、引言贡献(3)、第5节 | "greatly outperforms all existing methods"（摘要）、"contributes greatly"/"a great improvement" |
| A4 | "国际领先水平" | 摘要 | "an international leading level"（摘要） |
| A5 | "众所周知" | 引言第3段 | "It is well known that everyone knows"（引言第3段） |
| A6 | "填补该领域空白" | 引言第3段 | "filling the gap in this field"（引言第3段） |
| A7 | "毫无疑问/彻底改变" | 第6节结论 | "undoubtedly the best"/"completely change"（结论） |
| A8 | "显著优于所有现有方法" | 第5节 | "Obviously, MBF-GAN is the best among all methods"（第5节） |
| A9 | 口语词 | "说白了"（引言1）、"特别牛"（第5节）、"总的来说就是"（第6节）、"特别好用"（第6节）、"搞了个"（3.4）、"这块儿"（2.1） | "To be honest"（引言1）、"works amazingly well"（3.4） |
| A10 | 套话 | "随着时代的发展与科技的进步，人们的生活水平不断提高"（引言1） | "In modern society nowadays, with the rapid development..."（引言1） |

## B. 术语一致性（term-check / 领域术语库 nonstandard→canonical）

| # | 坑点 | 中文稿位置 | 英文稿位置 |
| --- | --- | --- | --- |
| B1 | 生成对抗网络 / 生成式对抗网络 / 对抗生成网络 / GAN 混用 | 摘要（生成式）、关键词（生成）、引言贡献（生成式）、3.3（对抗生成） | "generative adversarial network" / **"generative antagonistic network"（生造词，摘要×2、3.3）** |
| B2 | 脑电信号 / EEG 信号 / 脑电图信号 / 脑电波 混用 | 摘要、引言1（三种同段出现）、3.1（脑电波） | "EEG" / "electroencephalography" / "electroencephalogram" / **"brain electricity signal"（生造，引言1）** |
| B3 | 多分支特征 / 多尺度特征 混用 | 3.2（多分支提取 + 多尺度融合同段） | "multi-branch" / **"multibranch feature fusion"（3.2）** |
| B4 | 判别器 / 鉴别器 混用 | 2.2（鉴别器）、3.3（鉴别器×2 + 判别器×1） | "discriminator" / "discriminative network"（2.2、3.3） |
| B5 | 去噪 / 噪声消除 变体 | 全文统一"去噪"（对照组） | **"denosing"（引言2）/ "de-noising"（引言2）/ "denoising" 三种拼写混用** |
| B6 | 记号不一致 | x̃ / x 一致（对照） | 3.1 "recover x ... G(x̃) ≈ **X**"（大小写混用） |

## C. 信息保真（review-gate / guardrails：数值、引用、公式、锚点零丢失）

润色稿必须原样保留以下全部锚点（`number_preservation_rate = 1.0` 硬门禁）：

- 数值：94.7%、12.5 dB、256 Hz、32 名被试/32-channel、10 折交叉验证、p < 0.001、λ1 = 10、λ2 = 5、89.3%、10.8 dB、95.8%、3.2 个百分点、12800 段、4 秒、50 Hz、0.5–4/4–8/8–13 Hz、学习率 0.001、batch 64、200 轮/epochs、通道数 32/64/128/256/512、核大小 16、240 万参数、8.6 ms、RMSE 0.18/0.42/0.35/0.31/0.27/0.22、准确率 78.4/83.6/86.2/89.7/91.5/94.7（表1两稿一致）
- 英文独有：1 billion、18%、-5 dB/9.8 dB/7.3 dB、10 dB/11.4 dB/9.6 dB、2.4 million、10-20 system、Cz、0.05、8:1:1、每 10 epochs 更新权重
- 公式：`L_total = L_adv + λ1 · L_rec + λ2 · L_freq`（中文 3.4 / 英文 3.4）
- 引用编号：[1]–[8] 出现处不得丢失/改写/越界；表格数值行不得错位

## D. 疑似杜撰（fabrication_suspects_as_errors → 默认判 error）

| # | 坑点 | 位置 |
| --- | --- | --- |
| D1 | "全球有超过 10 亿人存在不同程度的脑电异常"（论断 + 大数字，无引用） | 中文引言第3段 |
| D2 | "over 1 billion people worldwide suffer from various EEG abnormalities"（无引用） | 英文引言第3段 |

## E. 文献缺口与引用规范（lit-search / lit-verify / cite-manager / format-adapter）

| # | 坑点 | 中文稿 | 英文稿 |
| --- | --- | --- | --- |
| E1 | 陈旧引用支撑"近年来"论断：[4] 为 2003 年文献，用于佐证"近年来深度学习巨大成功" | 引言第2段 | 引言第2段（"In recent years... [4]"） |
| E2 | 正文提及的文献不在文献表："Smith 等人报道了……" | 2.2 | 2.2（"Smith et al. reported..."） |
| E3 | 引用编号越界：正文引 [9]，文献表仅到 [8] | 2.2 | 2.2 |
| E4 | 文献信息不全：[3] 缺发表载体（仅 "Deep learning for EEG. 2018."）；[5] 缺卷期页与 DOI；[6] 缺页码；[7] 缺期刊信息 | 参考文献 | 参考文献 |
| E5 | 著录格式混乱：[2] 标题与刊名全小写（"eeg signal denoising using deep learning"） | 参考文献 | 参考文献（作者名也全小写） |
| E6 | 缺支撑论断（信号词无引用）："卷积神经网络……被引入脑电去噪任务"（中文2.2）/ "Convolutional neural networks have also been introduced..."（英文2.2）、"自适应滤波与经验模态分解等也被尝试"（中文2.1） | 2.1、2.2 | 2.1、2.2 |

## F. 逻辑/结构一致性（coherence / 人工或 A/B 盲评可见）

| # | 坑点 | 位置（两稿相同章节） |
| --- | --- | --- |
| F1 | 数据集 B：4.1 引入"用于泛化性验证"，第5节从未报告任何 B 的结果，结论却给出"95.8%" | 4.1 / 6 |
| F2 | "图 3"被第5节引用，全文从未定义/描述 | 第5节 |
| F3 | 摘要声称"完美地去除各类噪声干扰"与结果 94.7%（非 100%）不对应 | 摘要 vs 第5节 |

## G. 精炼规范（conciseness 规则 / GLEU）

| # | 坑点 | 中文稿 | 英文稿 |
| --- | --- | --- | --- |
| G1 | 超长无标点流水句 | 第5节"这一结果表明……较大幅度的提升。"（约90字无标点） | 引言1 "The reason why EEG denoising is difficult..."（约60词无标点） |
| G2 | 介词/句式冗余 | "通过对……的精心设计，使得……"（3.2，通过+使得双介词吞主语） | "In order to solve..., in order to improve..." 连用（引言4）；"It is worth noting that"（2.2） |

## H. 语法与语病（light 档应修复 / GEC 测试集）

| # | 坑点 | 中文稿 | 英文稿 |
| --- | --- | --- | --- |
| H1 | 的/地误用 | "随着深度学习**地**快速发展"（2.2）、"取得了更**好**地平衡"（引言4） | — |
| H2 | 数量词赘余 | "共**约** 12800 段**左右**"（4.1） | — |
| H3 | 主谓一致 | — | "The amplitude of these noise **are**"（引言1）、"deep learning **have** achieved"（引言2）、"recent researches **shows**"（2.2） |
| H4 | 冠词误用 | — | "**an** unique scale"（2.2）、"**a** EEG segment"（3.1） |
| H5 | 词性误用 | — | "we **revolutionary** propose"（摘要，应副词） |
| H6 | 悬垂修饰 | — | "**Using the proposed MBF-GAN, the results were obtained**..."（第5节，results 不会使用模型） |
| H7 | 形容词/副词混淆 | — | "it is **clearly** that it will cause..."（2.1，应 clear） |
| H8 | 不可数名词复数化 | — | "The **datas** are divided..."（4.1） |
| H9 | 时态混用 | — | "was proposed... **has**... " / "was designed..., **which improves**..."（2.2，过去时与现在时混用） |

## 建议测试流程

```python
# 1) harness 调用 batch-processor 的 build 阶段
batch_processor.run({
    "inputs": ["test-manuscripts/cn-eeg-denoising-draft.md"],
    "language": "cn",
    "phase": "build",
    "out": "output/test-cn",
})
batch_processor.run({
    "inputs": ["test-manuscripts/en-eeg-denoising-draft.md"],
    "language": "en",
    "phase": "build",
    "out": "output/test-en",
})

# 2) coding harness 回填响应（output/test-*/responses/<job_id>.txt）

# 3) apply：门禁 + 装配 + diff 报告；关注 result.json 的 guardrail_summary
batch_processor.run({
    "inputs": ["test-manuscripts/cn-eeg-denoising-draft.md"],
    "language": "cn",
    "phase": "apply",
    "out": "output/test-cn",
})
batch_processor.run({
    "inputs": ["test-manuscripts/en-eeg-denoising-draft.md"],
    "language": "en",
    "phase": "apply",
    "out": "output/test-en",
})

# 4) 单项能力核验
claim_guard.run({...})               # A 组 + D 组
term_check.run({...})                # B 组
lit_search.run({"mode": "gap", ...}) # D/E 组
format_adapter.run({...})            # E4/E5
cite_manager.run({"mode": "audit", ...}) # E2/E3
```

**判定要点**：A 组词应在润色稿中清零（dehype_rate=1.0）；B 组生造/退化术语应被拦截；C 组锚点在润色稿中零丢失零篡改；D 组两句无引用大数字应报 error/needs_review；E 组应产出缺口/核验报告而非静默通过；H 组语病应被修复且不引发 C 组锚点变动。
