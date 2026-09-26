# A full copy pass

What follows applies to every pass that rewrites more than a line or two, with
or without written rules.

## Strings in more than one place

- **The map.** On a site with a language switch, a string can live in the
  static HTML (the no-JavaScript render, equal to the default language), each
  language's dictionary, meta tags the switch never touches (`description`,
  `og:`, `twitter:`, `og:image:alt`, one set per page), per-language alt
  attributes, aria labels, the manifest and JSON-LD. The same key can hold
  different text on different pages: never replace by key across files.
- **The check.** After every pass, run a check that both languages have the
  same keys, no value is empty, the static HTML equals the default-language
  value, shared keys match on every page, and the alt text matches its
  default-language attribute. Key parity alone misses a one-sided edit.
- **How each string is inserted.** Text set with `textContent` shows entities
  and tags literally (`&ndash;`, `<a>`). If the renderer skips empty values,
  blanking a key keeps the old static text, so removing a line is a markup
  change. An apostrophe in a single-quoted JavaScript string stops the whole
  script, and a designed no-JavaScript fallback hides that in screenshots: use
  a curly apostrophe or escape it, then run `node --check`.
- **Locked lines.** Keep them in a plain-text file beside the rules, or in a
  `locked` code fence in the rules file itself, one unique substring per line.
  Use the same file as the scanner's `--locked` input and as the drift check:
  `copy_audit.py locks --locked FILE <site>` before and after the pass, and
  every count must match (a presence check misses a change on one page of
  five). Locking a line an alt text quotes (the h1) hides that alt from the
  scan.
- **No-break joins.** `&nbsp;` or a word joiner keeps a phone number or a time
  on one line, but a plain search no longer finds the string: list each one
  with the coupled edits.

## Layout

- Read a slot's CSS before writing into it. Where the design splits a heading
  over two lines, put a whole phrase on each line, never an article or
  preposition alone on the first. If no natural split exists, keep the current
  words if they are true and propose a one-line label.
- Write text the CSS uppercases in sentence case. A `<br>` hidden at some
  widths needs a space before it, or two words glue together. Markup inside
  copy may be styled (a `<span>` in a list item can render as a tag).
- A label in a fixed box (a caption beside carousel controls, a card sub-line,
  a pill button) has little room: keep new wording no wider than the old
  unless you have measured it at the narrowest width, in both languages,
  against whatever sits beside it. Shortening can break a layout too: a link
  that used to wrap may now run on inline.
- Check every changed line at 360, 390 and 430 px and at desktop, in both
  languages. Keep phone numbers, times ("17:00 tot 22:00"), prices and short
  place names on one line: a no-break space (`&nbsp;` in HTML, a literal
  U+00A0 in `textContent` strings), and after an en dash a word joiner or
  `white-space: nowrap`. Keep such joins out of any region a word counter
  reads as raw markup. If a name is wider than the column at 360 px, change the
  words (take it out of the heading) instead. One word alone on a last line is
  cosmetic; fix it where it splits a phrase.

## Checking a full rewrite

A full rewrite is checked from four angles, ideally by separate reviewers:

- **Truth**: each changed line against the sources, in each language: causes,
  comparisons, limits and the small words under "Keep it true".
- **Voice**: each language read aloud on its own: joints, calques, register,
  the personality list.
- **Mechanics**: every copy of each string, the locked-line counts, the
  project's own checks, the record.
- **Looks**: each changed section at the project's widths, in each language,
  with hidden states triggered (stub a failed request, fix the clock for a
  same-day note, open every reveal).

Then fix, and check again from all four: on two of the three sites behind this
skill, the first fix round brought faults of its own (a calque, an invented
cause, a fragment). A reviewer's suggested line is checked like your own. Stop
when a check finds nothing important. If faults still turn up after two fix
rounds, stop fixing, check the lines you changed last for truth, and hand back
what is left under "What is still wrong".
