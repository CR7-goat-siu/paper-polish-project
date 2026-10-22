# Task
Classify numbers extracted from a manuscript into a deterministic statistics registry. This task does not verify that the underlying research is true.

## Paper
{{title}}

## Extracted registry
{{registry}}

## Rules
1. Do not change, normalize, or invent any numeric value.
2. Use only the supplied location, meaning, unit, and surrounding text.
3. `metric_key` must identify the same conceptual measure across locations, for example `sample_size_enrolled`, `accuracy_test_set`, or `p_value_primary_test`.
4. Put numbers in the same comparison group only when the manuscript implies they describe the same quantity and should agree.
5. Use `critical: true` for sample sizes, primary outcomes, test statistics, p-values, confidence intervals, effect sizes, and missing-value counts when identifiable.
6. Do not mark any number `verified`; classification is not factual verification.
7. Propose recomputations only when all inputs and the reported output are present in the registry.

## Output
Return strict JSON only:
{
  "updates": [
    {
      "id": "N1",
      "metric_key": "stable_metric_key",
      "metric": "human-readable metric",
      "unit": "unit or empty",
      "meaning": "what this number means in context",
      "critical": true,
      "figure_table_id": "F1/T1 or empty"
    }
  ],
  "comparisons": [
    {
      "id": "CMP1",
      "metric": "quantity that should agree",
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
  "uncertainties": ["items that require author confirmation"]
}
