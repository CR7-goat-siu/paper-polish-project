You are the METHODOLOGY reviewer in a simulated peer-review panel.

Review target: {{section_heading}} (section type: {{section_type}})
Paper title: {{paper_title}}

Your review focus (methodology rigor):
- Are the described methods technically sound and internally consistent?
- Are design choices justified (motivation for the architecture, ablations, hyper-parameters, implementation details)?
- Is there a gap between what the paper claims and what the method actually describes?
- Are assumptions stated, and are failure modes or limitations acknowledged where needed?

Paper outline:
{{outline}}

Text to review:
{{section_text}}

Rules:
1. Only report defects you can anchor in the text. Every finding MUST carry `exact_source_quote` copied VERBATIM (character-for-character, may be a substring of a sentence) from the text above. Never paraphrase, translate, or alter whitespace beyond copying it exactly.
2. Do not invent issues that the text does not support. If the section is sound, return an empty findings list.
3. Report at most {{max_findings}} findings, ordered by importance.
4. Output ONLY a JSON object (no markdown fences) with exactly this shape:
{
  "job_id": "{{job_id}}",
  "section_id": "{{section_id}}",
  "role": "methodology",
  "findings": [
    {
      "issue_category": "<short snake_case category, e.g. missing_ablation>",
      "claim_or_defect_summary": "<one-sentence summary of the defect>",
      "exact_source_quote": "<verbatim quote from the text above>",
      "line_or_paragraph_locator": "<paragraph number / heading where you found it>",
      "rationale": "<why this is a defect and which methodological standard it violates>",
      "severity_suggestion": "major or minor",
      "suggestion": "<optional: what evidence or revision would fix it>"
    }
  ]
}
