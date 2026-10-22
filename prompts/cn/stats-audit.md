# 任务
把论文中抽取的数字分类为确定性的统计登记表。本任务不核实研究事实本身。

## 论文
{{title}}

## 已抽取数字
{{registry}}

## 规则
1. 不得修改、归一化或编造任何数字。
2. 只能使用已提供的位置、含义、单位和上下文。
3. `metric_key` 用于识别跨位置表达的同一概念，例如 `sample_size_enrolled`、`accuracy_test_set`、`p_value_primary_test`。
4. 只有当论文语境表明多个数字描述同一数量且本应一致时，才放入同一比较组。
5. 可识别的样本量、主要结局、检验统计量、p 值、置信区间、效应量和缺失值应标记 `critical: true`。
6. 不得把任何数字标为 `verified`；分类不等于事实核验。
7. 只有输入和报告输出都在登记表中时才提出重算。

## 输出
只返回严格 JSON：
{
  "updates": [
    {
      "id": "N1",
      "metric_key": "stable_metric_key",
      "metric": "人类可读指标名",
      "unit": "单位；无则空",
      "meaning": "该数字在上下文中的含义",
      "critical": true,
      "figure_table_id": "F1/T1；无则空"
    }
  ],
  "comparisons": [
    {
      "id": "CMP1",
      "metric": "本应一致的数量",
      "number_ids": ["N1", "N2"]
    }
  ],
  "recomputations": [
    {
      "id": "RC1",
      "operation": "sum|mean|difference|ratio|percent",
      "input_ids": ["N1", "N2"],
      "reported_id": "N3",
      "tolerance": 0.01
    }
  ],
  "uncertainties": ["需要作者确认的项目"]
}
