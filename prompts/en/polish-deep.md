# Task
Apply a **deep polish** to the academic passage below: improve sentence flow, cohesion, paragraph logic, and academic register while preserving all claims, facts, and numbers.

## Passage metadata
- Paper title: {{title}}
- Section type: {{section_type}}
- Context hint: {{context}}

## Document-level editing brief
If the brief below is not "(none)", it records style decisions and scope agreements confirmed with the author for the whole document. Apply it consistently in this passage:

{{brief}}

## Author/teacher comments on this passage (highest priority)
If the comments below are not "(none)", they are explicit revision requests anchored to this passage by the teacher/reviewer. Resolve every comment fully before polishing; a comment overrides the default strength:

{{comments}}

## Strength
Strength label: {{strength_label}}

## Reviewer feedback from a previous pass
If the feedback below is not "(none)", resolve every listed violation first (missing numbers, weakened citations, exaggerated wording, dropped content), then polish:

{{feedback}}

## Rules (JSON)
Follow the structured rules exactly, especially the objectivity, coherence, and conciseness policies:

{{rules}}

## Terminology glossary (JSON)
Use the canonical form for any matched term:

{{terms}}

## Terminology corrections (must apply)
Replace every nonstandard form below with its canonical form wherever it appears:

{{term_corrections}}

## Mandatory anchors for this passage (auto-extracted)
{{anchors}}

## Passage to edit
Only the content between `TEXT_BEGIN` and `TEXT_END` may be modified. Do not omit any part:

TEXT_BEGIN
{{text}}
TEXT_END

## Output requirements

{{self_check}}

### Hard constraints (violating any one fails review)
a. Only numbers, percentages, organizations, datasets and sources already present in the source may appear. Never add factual content absent from the source, even if it "sounds more credible".
b. When encountering uncertain terminology, vague factual statements, doubtful abbreviations, or claims lacking literature support, never guess or fabricate from parametric memory — keep the source's objective wording; do not "complete" it.
c. Any hype wording or unsupported absolute claim already present in the source must be removed in this pass, or downgraded to an evidence-backed statement.

1. Return only the fully revised passage, with no commentary, prefix, or Markdown code fences.
2. Prioritize wordiness, ambiguous references, weak transitions, and nominalizations; change little when the original is already strong.
3. Objectivity: remove inflated wording (unprecedented, groundbreaking, massive, perfect) and state claims only as strongly as the reported evidence supports; keep every numeric result, citation marker, equation, and paragraph exactly as in the source.
4. Never change technical meaning, experimental facts, numbers, equations, code, citations, or the number of paragraphs.
