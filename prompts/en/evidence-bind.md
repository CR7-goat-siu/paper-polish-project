# Task
Bind one manuscript claim to verifiable evidence from the cited source. This is a strict evidence audit, not a plausibility judgment.

## Claim
- Claim ID: {{claim_id}}
- Location: {{claim_location}}
- Text: {{claim_text}}

## Citation
- Citation ID: {{citation_id}}
- Metadata:
{{citation_metadata}}

## Source material
{{source_material}}

## Rules
1. Use only the supplied source material. Do not infer support from the title, abstract-less metadata, world knowledge, or similarity of topic.
2. A `verified` result requires an exact source location and a verbatim evidence quote of at least 12 characters.
3. `support_type` must be one of `direct`, `indirect`, `background`, or `opposing`.
4. `support_strength` must be one of `high`, `medium`, or `low`.
5. If the source material is missing, incomplete, or does not contain the claim, return `unverified` with an empty quote and explain the gap.
6. Never fabricate a page, section, quotation, DOI, or author.
7. Preserve uncertainty. A citation that merely shares a topic does not verify a specific factual claim.

## Output
Return strict JSON only, with no code fences and no prose outside JSON:
{
  "claim_id": "{{claim_id}}",
  "reference_id": "{{citation_id}}",
  "source_location": "page/section/paragraph or empty",
  "quote": "verbatim source sentence or empty",
  "support_type": "direct|indirect|background|opposing|unverified",
  "support_strength": "high|medium|low",
  "verification_status": "verified|unverified",
  "reason": "brief deterministic explanation",
  "missing_evidence": ["what the author must provide when unverified"]
}
