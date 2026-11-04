# Glossary supplement candidate design

**Status: proposed design only.** No supplement generator, candidate format, or
automatic merge behavior is implemented by this document.

## Current behavior

Configured authoritative glossaries use schema 2 concept entries. Runtime
projection selects terms explicitly present for the configured source and
target languages. If a concept lacks either language form, the projection does
not infer a missing form from a third language. An explicitly selected
`reference_glossary` is read-only context: it can help a worker recognize a
concept, but it cannot supply an authoritative translation or change glossary
priority. Its sparse runtime context includes only language forms explicitly
present for the current source/target pair. For example, a German-only
reference snapshot has no English hit for an English source term; workers must
not read the full German snapshot to infer an English alias. Translation
entities remain a separate per-book source and are not a fallback channel for
repairing a missing authoritative glossary projection.

## Proposed workflow

When a required projection is missing, preparation should fail closed and
identify the affected concept IDs. A future review workflow may produce a
separate candidate supplement for a human to inspect. Candidate generation
would collect evidence and propose a change; it would not update the source
glossary, configuration, translation entities, or worker context.

Each candidate should record:

- the stable `concept_id` and missing `source_language` form;
- the proposed source-language form, if one can be supported by evidence;
- the exact evidence reference and a short, bounded excerpt or locator;
- provenance for every piece of evidence, including which read-only reference
  concept or source document supplied it;
- a confidence value with a brief reason, never a confidence-only approval;
- before/after term diffs and SHA-256 hashes for the authoritative glossary,
  selected reference snapshot, and evidence input;
- an explicit review status such as `proposed`, `accepted`, or `rejected`, plus
  reviewer identity and decision note when reviewed.

Candidate evidence may help a reviewer compare a known concept across
languages. It must not be used to infer an authoritative term automatically.
The reviewer must verify the source-language form and its concept identity.
An accepted change could either be applied directly to the authoritative
schema 2 glossary or saved as a separate schema 2 supplement scoped to one
book. Each supplement concept used for translation must itself contain the
explicit source-language and target-language forms required by that book's
configured pair. The loader projects each selected glossary independently; it
does not join missing language fields across files. Therefore, adding a
supplement beside a selected de-only concept cannot make that original concept
projectable in English. A book-scoped supplement may be selected through the
existing `translation.glossaries` configuration; it is not mounted
automatically. The original glossary can remain unchanged, but it must not be
treated as a partial entry that the supplement completes. In either case,
normal explicit-glossary selection, snapshot, projection, and conflict checks
must run again. Validation must detect duplicate concepts, incompatible forms,
and conflicts with other selected glossaries, then verify the configured
source/target projection. An unreviewed
candidate and a read-only reference snapshot must never enter the authoritative
term priority chain or be written into `translation_entities` as a substitute.

## Review and freshness requirements

A candidate is reviewable only while all recorded hashes match the current
inputs. A changed glossary, reference snapshot, or evidence source makes the
candidate stale and requires regeneration or a fresh review. Acceptance applies
only to the exact concept and language form shown in the diff. It does not
authorize edits to neighboring concepts, infer additional language forms, or
alter `fixed`/`preferred` priority.

Existing configuration already permits selecting multiple authoritative
glossaries; that is implemented behavior for independent entries and explicit
language-pair projection, not cross-file completion of missing forms.
Candidate generation, review
tracking, and a dedicated book-scoped supplement lifecycle are future design
work and are not implemented. Until a reviewed candidate flow exists and its
contracts are tested, missing authoritative language forms remain a
preparation error requiring a manual glossary update or a separately prepared
and explicitly selected glossary file.
