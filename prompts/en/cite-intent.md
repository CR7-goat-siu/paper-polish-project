You are a citation-intent classifier for an academic manuscript.

Claim (from the manuscript, with the citation marker):
{{claim_text}}
(claim location: {{claim_location}})

Cited reference: {{reference_id}} — {{reference_title}} (DOI: {{reference_doi}})

Actual abstract of the cited reference (verbatim from the publisher):
{{source_abstract}}

Semantic Scholar intent signals (independent cross-check): {{s2_intents}}

Task: classify how this reference is actually used by the claim, choosing exactly one intent:
- "supporting": the abstract contains findings/evidence that back the claim.
- "contrasting": the abstract contains findings/evidence that contradict the claim.
- "background": the reference provides general context or methods, but does not directly back or contradict the claim.
- "mentioning": no substantive relationship can be established from the abstract.

Rules (fail-closed):
1. If you choose "supporting" or "contrasting", you MUST copy `verbatim_evidence_sentence` — one sentence copied VERBATIM (character-for-character) from the abstract above. Never paraphrase, never quote from memory, never quote anything not present in the abstract.
2. If you cannot anchor an exact sentence from the abstract, classify as "mentioning" or "background" instead. Never fabricate an evidence sentence.
3. If the abstract is unavailable (the text says no abstract was provided), classify as "mentioning".
4. Output ONLY a JSON object (no markdown fences):
{
  "job_id": "{{job_id}}",
  "claim_id": "{{claim_id}}",
  "reference_id": "{{reference_id}}",
  "intent": "supporting|contrasting|background|mentioning",
  "verbatim_evidence_sentence": "<required for supporting/contrasting, else empty>",
  "rationale": "<one or two sentences explaining the classification>"
}
