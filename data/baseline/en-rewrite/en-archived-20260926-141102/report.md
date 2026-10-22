# 润色质量评测报告（en / archived / rewrite）

| 指标 | 数值 | 目标 |
| --- | --- | --- |
| 去浮夸成功率 dehype_rate | 1.0 | ≥ 0.8 |
| 守卫检出率 guard_detection_rate | 1.0 | ≥ 0.8 |
| 术语召回率 terminology_recall | 1.0 | ≥ 0.8 |
| 自造词拦截率 coined_word_interception | 1.0 | ≥ 0.8 |
| GLEU（grammar） | 0.6368 | ≥ 0.6 |
| 错误修正率 fix_rate_avg | 1.0 | ≥ 0.8 |
| 过度修正率 overcorrection_rate | 0.0 | ≤ 0.2 |
| 数字保真率 number_preservation_rate | 1.0 | = 1.0 |

## objectivity

- ✅ adv-001: {'change_ratio': 0.2958}
- ✅ adv-002: {'change_ratio': 0.5682}
- ✅ adv-003: {'change_ratio': 0.2959}
- ✅ adv-004: {'change_ratio': 0.4962}
- ✅ adv-005: {'change_ratio': 0.2}
- ✅ adv-006: {'change_ratio': 0.402}
- ✅ adv-007: {'change_ratio': 0.2455}
- ✅ adv-008: {'change_ratio': 0.4333}
- ✅ adv-009: {'change_ratio': 0.503}
- ✅ adv-010: {'change_ratio': 0.3714}

## terminology

-  term-001: ok
-  term-002: ok
-  term-003: ok
-  term-004: ok
-  term-005: ok
-  term-006: ok
-  term-007: ok
-  term-008: ok
-  term-009: ok
-  term-010: ok

## grammar

-  gram-001: {'gleu': 0.4231}
-  gram-002: {'gleu': 1.0}
-  gram-003: {'gleu': 1.0}
-  gram-004: {'gleu': 0.6429}
-  gram-005: {'gleu': 0.5185}
-  gram-006: {'gleu': 0.7564}
-  gram-007: ok
-  gram-008: {'gleu': 0.72}
-  gram-009: {'gleu': 0.6707}
