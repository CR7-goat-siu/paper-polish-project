# Task
Find real, verifiable references for the claim below and return machine-readable JSON.

## Paper context
- Paper title: {{title}}
- Allowed public sources (all key-free APIs): {{sources}}
- Target citation style: {{style}}

## Claim missing citation support
{{gap}}

## Requirements
1. Use only the public sources listed above; never scrape Google Scholar, never use any service that requires an API key;
2. Convert the claim into precise academic English keywords and a boolean query (the draft query in the gap is a starting point), and retry with synonyms or field restrictions if needed;
3. Every candidate must carry verifiable metadata (DOI, title, authors, year, venue, volume/issue/pages, URL) plus a one-line rationale;
4. Never fabricate: drop any item you cannot locate in a source; title, year and venue must match the source record exactly;
5. Prefer authoritative venues and highly cited works from the last five years; return at most 8 items, ranked by relevance.

## Output (strict JSON, no extra prose and no code fences)
{
  "gap_id": "same id as the input gap",
  "keywords_used": ["english keywords actually used"],
  "boolean_query": "boolean query actually used",
  "candidates": [
    {
      "title": "Title exactly as indexed",
      "authors": ["Family, G.", "Family2, G2"],
      "year": 2024,
      "venue": "Full journal or conference name",
      "volume": "12",
      "issue": "3",
      "pages": "45-58",
      "doi": "10.xxxx/yyyy",
      "url": "https://doi.org/10.xxxx/yyyy",
      "source": "crossref / openalex / semanticscholar / arxiv / europepmc",
      "cited_by": 42,
      "why_relevant": "one sentence on how it supports the claim",
      "evidence_quote": "sentence from the abstract or conclusion supporting the claim"
    }
  ]
}
