# 润色质量评测报告（cn / archived / light）

| 指标 | 数值 | 目标 |
| --- | --- | --- |
| 去浮夸成功率 dehype_rate | 1.0 | ≥ 0.8 |
| 守卫检出率 guard_detection_rate | 1.0 | ≥ 0.8 |
| 术语召回率 terminology_recall | 1.0 | ≥ 0.8 |
| 自造词拦截率 coined_word_interception | 1.0 | ≥ 0.8 |
| GLEU（grammar） | 0.6951 | ≥ 0.6 |
| 错误修正率 fix_rate_avg | 1.0 | ≥ 0.8 |
| 过度修正率 overcorrection_rate | 0.0 | ≤ 0.2 |
| 数字保真率 number_preservation_rate | 1.0 | = 1.0 |

## objectivity

- ✅ cadv-001: {'change_ratio': 0.3529}
- ✅ cadv-002: {'change_ratio': 0.1892}
- ✅ cadv-003: {'change_ratio': 0.5}
- ✅ cadv-004: {'change_ratio': 0.5455}
- ✅ cadv-005: {'change_ratio': 0.4194}
- ✅ cadv-006: {'change_ratio': 0.3333}
- ✅ cadv-007: {'change_ratio': 0.2308}
- ✅ cadv-008: {'change_ratio': 0.3659}

## terminology

-  cterm-001: ok
-  cterm-002: ok
-  cterm-003: ok
-  cterm-004: ok
-  cterm-005: ok
-  cterm-006: ok
-  cterm-007: ok
-  cterm-008: ok

## grammar

-  cgram-001: {'gleu': 1.0}
-  cgram-002: {'gleu': 1.0}
-  cgram-003: {'gleu': 0.3333}
-  cgram-004: {'gleu': 0.4}
-  cgram-005: {'gleu': 0.6}
-  cgram-006: {'gleu': 1.0}
-  cgram-007: {'gleu': 1.0}
-  cgram-008: {'gleu': 0.2273}
