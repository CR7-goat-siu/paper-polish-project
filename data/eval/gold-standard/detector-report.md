# 金标检测器量化报告（F5 层 1）

- 夹具：8 篇真实 arXiv 论文摘录（离线固化集）
- 阳性植入召回：35/35（1.0）
- 阴性 P0/P1 误报率：6/8（0.75）
- 干净文本告警总量：444（均值 55.5/篇，全部为 P2/P3 噪声画像）

## 干净文本上的启发式噪声画像（调阈依据）

| 签名 | 触发次数 |
| --- | --- |
| cite-manager::uncited_entry | 232 |
| lit-search::incomplete_entry | 166 |
| claim-guard::intensifier | 11 |
| claim-guard::unanchored_claim | 6 |
| claim-guard::unverifiable | 6 |
| claim-guard::colloquial | 6 |
| term-check::alias_without_canonical | 5 |
| claim-guard::hype | 5 |
| lit-search::missing_citation | 3 |
| claim-guard::wordy | 2 |
| claim-guard::absolute | 1 |
| lit-search::stale_citation | 1 |

## 逐夹具明细

- ：告警 53（P0 0 / P1 2 / P2 3 / P3 48），植入召回 4/4
- ：告警 70（P0 0 / P1 0 / P2 1 / P3 69），植入召回 4/4
- ：告警 83（P0 0 / P1 2 / P2 1 / P3 80），植入召回 5/5
- ：告警 88（P0 0 / P1 2 / P2 2 / P3 84），植入召回 5/5
- ：告警 76（P0 0 / P1 1 / P2 1 / P3 74），植入召回 5/5
- ：告警 62（P0 0 / P1 0 / P2 2 / P3 60），植入召回 4/4
- ：告警 6（P0 0 / P1 1 / P2 2 / P3 3），植入召回 4/4
- ：告警 6（P0 0 / P1 3 / P2 2 / P3 1），植入召回 4/4
