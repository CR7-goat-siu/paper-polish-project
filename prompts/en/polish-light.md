# Task
Apply a **light copy-edit** to the academic passage below: correct only grammar, spelling, punctuation, articles, prepositions, tense, and typography. Do not restructure sentences or change the author's voice.

## Passage metadata
- Paper title: {{title}}
- Section type: {{section_type}}
- Context hint: {{context}}

## Document-level editing brief
If the brief below is not "(none)", it records style decisions and scope agreements confirmed with the author for the whole document. Apply it consistently in this passage:

{{brief}}

## Author/teacher comments on this passage (highest priority)
If the comments below are not "(none)", they are explicit revision requests anchored to this passage by the teacher/reviewer. Resolve every comment fully even in light mode; a comment overrides the default strength:

{{comments}}

## Strength
Strength label: {{strength_label}}

## Reviewer feedback from a previous pass
If the feedback below is not "(none)", resolve every listed violation first, then copy-edit:

{{feedback}}

## Rules (JSON)
Follow the structured rules exactly, especially the objectivity policy:

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
2. Keep the original sentence when a change is risky or uncertain.
3. Objectivity (word-level substitution only — do not restructure):
   - Typical substitutions: groundbreaking -> notable; unprecedented -> previously unreported (only if factually true); massive / enormous / huge / tremendous -> marked / substantial / considerable; revolutionary -> new; astonishing / astounding -> notable; perfect / flawless -> effective; unparalleled / unmatched -> competitive; indispensable -> important; the best -> strong; Everyone knows -> Prior studies have shown.
   - Intensity words such as completely / totally / absolutely / extremely must be DELETED outright, not replaced with another intensifier.
4. Never add or remove semantic content; never alter equations, code, citation markers, numbers, or the paragraph count.
