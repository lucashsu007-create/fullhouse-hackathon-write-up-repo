# A site's own text rules

When a site will get more than one copy pass, write down how the skill applies
to it: who is talking, the owner's words, what is locked, where each string
lives, how to check. Everyone who edits the site's words afterwards, person or
agent, reads that first. Three sites (two restaurants and an advisory firm)
arrived at the outline below, and the checks after it are faults reviewers
found in those first drafts.

This file covers writing the rules. The mechanics of each pass (strings in more
than one place, layout, checking a full rewrite) are in `references/copy-pass.md`,
and the record is under "What to hand back" in SKILL.md.

## Where the rules live

- A `## Website text rules` section in the file agents already read for that
  site (its `BUILD-NOTES.md` or client README), not a new file.
- A one-line pointer where sessions start (`CLAUDE.md`, `AGENTS.md`, the build
  skill), so nobody writes against an older authority such as a playbook or a
  superseded list of voice rules.
- Each pass is recorded as its own `## Copy pass, <date>` section after the
  rules. The rules change only when a rule changes or the owner answers a
  question, and each such change is dated.

## Research before drafting

- **Voice.** As in SKILL.md step 1. Also open the old theme's demo and two or
  three competitors, and strike what they also print. Give the path to the
  owner's material even when it is gitignored or outside the worktree, or a
  later agent will conclude there is none.
- **Truth.** Rank the sources: the facts file, the legal pages, the owner's own
  writing. List what is not a source: the current page, earlier builds, notes by
  earlier agents, this skill's examples, photos. List every claim on the page
  that has no source.
- **Map.** Where each string lives (SKILL.md, "Strings in more than one
  place"), what is locked, and which slots the layout constrains.
- **Visitor.** The questions each page answers, in order.

## The outline

1. **Before you edit**: five to eight lines that must not be skipped (which
   folder to edit, the register, the places each change must reach, what the
   lock kinds mean, the checks to run before hand-back).
2. **Scope and precedence**: what the rules cover, what they supersede, and
   what wins over them, in one straight line. No loops (rules that defer to a
   guardrails file that names a playbook as the authority).
3. **Who is talking, and to whom**: the register with its evidence and counts,
   labelled as our choice where the evidence doesn't show it, with the owner
   question that settles it.
4. **The owner's lines and the personality**: lines to keep as written, and a
   list of personality lines that binds on every pass, with at least one pair
   that makes a line warmer, not only shorter.
5. **Words**: a table of *use (NL)*, *use (EN)*, *avoid* and *evidence*. Each
   *use* cell is the wording for the page in that language ("met sushi in de
   hoofdrol" / "with a focus on sushi"), never a gloss or a concept name:
   writers copy the cell.
6. **What each page answers**, in order.
7. **Each kind of text** (hero, headings, running copy, FAQ, form messages,
   alt text, meta, buttons): a real Bad line from the site and a Good line.
8. **Facts**: sources, not sources, fine to say, remove now (every copy, by key
   and page, both languages), on the page now but ask first, never.
9. **Locked text**: a table of each item, what is locked (words, meaning,
   obligations, figures, warnings) and who approves a change, plus the lock
   file. Don't open it with a blanket rule that contradicts its own table.
10. **Strings in more than one place**, and the check that proves them in step.
11. **Structure a copy pass keeps**, with copy-only fallbacks, and the strings
    tied to the layout.
12. **Length**: a ceiling measured against the page, saying how it was counted
    (visible words in `<main>` is the scanner's figure). Call it an estimate
    if the owner hasn't asked for less text; never a target.
13. **Review**: a runnable block of commands.
14. **Open questions for the owner**, numbered, each with the default that
    holds until it is answered.
15. **Older records that no longer bind**: stale comments, superseded rules,
    copy decks that no longer match the live page.

## Checks before handing the rules over

- Apply the visitor test to the rules themselves. What an editor needs before
  touching a line (Before you edit, the words table, Good and Bad per kind of
  text, the locks, the string map) goes first and stays short; evidence, counts
  and research go below it or into the Copy pass record. The first rules
  written this way ran to five or six thousand words each, longer than this
  skill, and every editor is told to read them first.
- Writers paste Good examples word for word, so write each for its exact slot
  and test it as copy: true by the strictest source, no repeat of the line
  beside it, the slot's length, the owner's words, a clean scan. Bad examples
  are real lines from the site, never invented to match a scanner flag.
- Say only what is particular to this site. Don't restate this skill, copy its
  examples, or import advice the site has no use for (an FAQ rule for a site
  with no FAQ).
- Credit a line to the owner only after tracing it; in a mixed document (a memo
  that gives the owner advice) quote only what they wrote.
- No quotas (particles per section, sentences per paragraph) and no "budget"
  wording: both turn into targets.
- Point only to files and lists that exist. Create the numbered question list
  when the first question comes up; append new ones, never renumber.
- Date anything that can go stale: a rating, a count, "checked on".
- Make the review block self-contained for a fresh shell: say which directory
  it runs from, fail loudly on empty input (an empty lock list is an error,
  not a pass), and avoid backslash escapes the file format can mangle. Run it
  once, then on a scratch copy with one seeded mistake, and see it fail.
- Critique the draft for truth, voice and whether an agent can follow it
  before a rewrite relies on it.

## Superseding a rule

Only when the owner has already said the tone is wrong. Name the rule exactly:
file, section, list position (say whether counting starts at 0 or 1) and its
opening words, so an off-by-one can't supersede the wrong one. Say what
replaces it and on what basis (who asked, when, paraphrased), keep what the old
rule was for, and mark it "Superseded <date> by <section>" where readers meet
it, including any pointer file that still names it as the authority. Put the
change on the owner's list; the new rule is the default until they answer.
Never restate the old rule in softer words ("one point per sentence" is still
the same rule).
