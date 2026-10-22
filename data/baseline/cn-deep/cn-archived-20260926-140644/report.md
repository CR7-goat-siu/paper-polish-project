# 润色质量评测报告（cn / archived / deep）

| 指标 | 数值 | 目标 |
| --- | --- | --- |
| 去浮夸成功率 dehype_rate | 1.0 | ≥ 0.8 |
| 守卫检出率 guard_detection_rate | 1.0 | ≥ 0.8 |
| 术语召回率 terminology_recall | 1.0 | ≥ 0.8 |
| 自造词拦截率 coined_word_interception | 0.9583 | ≥ 0.8 |
| GLEU（grammar） | 0.4799 | ≥ 0.6 |
| 错误修正率 fix_rate_avg | 1.0 | ≥ 0.8 |
| 过度修正率 overcorrection_rate | 0.25 | ≤ 0.2 |
| 数字保真率 number_preservation_rate | 1.0 | = 1.0 |

## objectivity

- ✅ cadv-001: {'change_ratio': 0.5714}
- ✅ cadv-002: {'change_ratio': 0.4203}
- ✅ cadv-003: {'change_ratio': 0.6}
- ✅ cadv-004: {'change_ratio': 0.5}
- ✅ cadv-005: {'change_ratio': 0.3548}
- ✅ cadv-006: {'change_ratio': 0.619}
- ✅ cadv-007: {'change_ratio': 0.2973}
- ✅ cadv-008: {'change_ratio': 0.7021}

## terminology

-  cterm-001: ok
-  cterm-002: ok
-  cterm-003: {'nonstandard_leaked': ['误差反向传播']}
-  cterm-004: ok
-  cterm-005: ok
-  cterm-006: ok
-  cterm-007: ok
-  cterm-008: ok

## grammar

-  cgram-001: {'gleu': 0.1}
-  cgram-002: {'gleu': 1.0}
-  cgram-003: {'gleu': 0.3333}
-  cgram-004: {'gleu': 1.0}
-  cgram-005: {'gleu': 0.6}
-  cgram-006: {'gleu': 0.2}
-  cgram-007: {'gleu': 0.3333}
-  cgram-008: {'gleu': 0.2727}
