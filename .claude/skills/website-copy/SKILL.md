---
name: website-copy
description: Write, rewrite and review website copy so it sounds like a real business talking to a customer, not like AI. Use whenever you write or edit customer-facing text for a website, landing page, demo or POC in English or Dutch, such as hero lines, headings, about, services, menus and prices, FAQ, contact, buttons, form labels and messages, meta titles and descriptions, alt text, captions and translations. Also use when copy is called robotic, AI-sounding, generic, stiff, clipped or too long ("te veel tekst", "klinkt als AI"), when someone wants it simpler, clearer, warmer or more natural, or when writing a site's own website text rules. Includes a scanner that pulls a page's visible text and flags AI tells, over-cut prose, repeated joints, repetition and stiff Dutch. Project rules on facts, legal wording and locked text still win.
---

# Website copy that sounds like a person

Pages written with AI help tend to read as machine-made, and people notice at a
glance: readers say the setup shows and ask for simpler, clearer text. The
cause is rarely carelessness. It is three habits of careful work:

1. **Writing for the auditor.** Every true fact goes on the page, so it reads
   like a dossier: night-bus numbers, the count of parking spaces and charging
   points, six rattan lamps listed in the alt text.
2. **Writing to a template.** Every section gets the same numbered kicker, the
   same eyebrow, the same two-line headline with a full stop, the same list of
   three.
3. **Fixing by banning.** When the first two get flagged, a list of banned tics
   goes up and the copy is cut to fragments, which is a tell of its own:
   "She runs the screen herself." "A clear role." Gluing each fragment back on
   with ", and" is the next one.

What works instead: write what the owner would say to a customer, then tidy it.

## Three tests for every line

**The counter test.** Could the owner say this out loud, to a customer at the
counter or on the phone, without sounding like they are reading a leaflet?
People speak in full sentences, and say *because* or *so* only when there is a
reason. They don't speak in fragments, slogans or legal notes. This catches
clipped fragments, stiff word order, slogans, and the page talking about itself.

**The visitor test.** Does the line answer something a visitor wants to know,
at this point on the page? If not, it goes: to the build notes, to a second
page, or nowhere. True is necessary, not sufficient. This catches inventory,
repetition and too much text. It is a test for facts, not for personality: the
one thing only this business has (a running joke, a house story, a strange
dish) stays even though no visitor asked for it.

**The swap test.** Put a competitor's name in. Is the line still true? "Een
avond vol smaak, vuur en verrassingen" is true of every restaurant in town, so
it says nothing. "Er zijn tafels aan het raam, en in de lounge brandt een open
vuur" is true of one, and it says what is there without promising where a
guest may sit. Pick the one or two details that are both specific and worth
knowing; all of them is inventory again.

## When the project has rules of its own

Look first for a "Website text rules" section (in `BUILD-NOTES.md`, the client
README or `CLAUDE.md`) and the latest "Copy pass" record after it. The rules
are this skill applied to that site, so they win where the two differ. If there
are none and more copy work will follow, write them: read
`references/site-rules.md` first. The project's other notes (`CLAUDE.md`,
`BUILD-NOTES.md`, `GUARDRAILS.md`) win over this skill too:

- **Facts, legal wording, locked text, banned characters**: binding, always.
- **Voice and taste**: binding too. A rule like "atmosphere is the product,
  never cut personality" beats the visitor test.
- **A style rule that causes the robotic tone** (say, "one idea per
  sentence"): don't silently obey it or break it. Write within it and add two
  or three lines showing what its removal would buy, as a proposal. If the
  owner has already said the tone is wrong, the site's text rules may
  supersede it instead (`references/site-rules.md` says how).
- **Good examples in the rules** are not approved copy: check one like your own
  line (sources, the line beside it, the slot's length) before using it.
- **Notes go stale.** A later recorded decision and the live markup beat an
  older comment or note. Fix any comment your edit makes wrong.
- **Structure**: a copy pass works inside the existing markup. Turning a list
  into prose, dropping kickers or eyebrows, merging a name card into a
  sentence, or removing markers the design uses is a design change. Hand it
  back as a question, with a copy-only fallback.
- **Coupled edits**: a line often lives in several places (nav and footer,
  legal lines mirrored on the legal pages, per-page head tags, each language's
  dictionary). Search the raw files of every page, not only the keys you were
  given, and read any HTML comment beside the line. "Strings in more than one
  place" below has the full map.

## How to write it

On your first rewrite in a session, read `references/examples.md`: real
before-and-after pairs, Dutch and English, restaurant and B2B. They show a
method, not lines to reuse: an after written for one page can repeat a nearby
line or say more than your sources do on another.

### 1. Find the voice first

Collect the owner's own words before writing any: their old site or menu,
social captions, replies to reviews, job ads, emails or messages, notes from a
call. Note:

- how they address people (*je* or *u*, first names or not) and themselves
  (*we*, *I*, the business name);
- the words they use for their own things (*de kaart* or *het menu*, *clients*
  or *partners*);
- anything with personality: a dish they're proud of, a joke, a phrase regulars
  repeat in reviews;
- in English: contractions or not, British or American spelling, and *we* or
  *I* when one named person runs the firm.

**Check who wrote it.** A page built with AI help is not the owner's voice, and
the page you are rewriting is often exactly that. Trace a line before you count
it (the old site, `git log -S'<phrase>'`, the build notes). Not the owner, even
when a note quotes it: an earlier build's copy, notes by earlier agents, a
booking widget or ordering platform, stock form or theme text (a theme demo's
"How we work" can be a fine heading, but it is no evidence of voice), a
sentence competitors also print, and this skill's own sample lines. Lines the
owner did write still get their facts checked, and their self-praise ("of high
quality") still fails the swap test: keep the line and offer a replacement.

Reuse the owner's good lines as they are. Fix typos, keep the idiom. Count *je*
and *u* (and *we* or *wij*, contractions) only in what the owner wrote to
customers. If that is thin, let the lines you can't change (a locked success
message, the privacy notice, the booking emails) break the tie, so no page or
booking flow mixes the two. Record the counts and the choice as yours and ask
the owner. Don't write "the owner says X" until you have found X in their own
text. Where there's little evidence, plain beats invented.

Research tells you how to write, not what to paste. An internal metric (a reply
rate, a follower count) is never copy; a public rating the owner chose to show
can be.

### 2. List the visitor's questions, in order

Each section answers one question a real visitor has.

- Restaurant, cafe, bar, hotel: what kind of place and food, what it costs,
  when it's open, where it is, how to book or order, and whether it suits them
  (groups, dietary needs, kids, parking).
- Professional services: what you do, for whom, how it works, who is behind
  it, how to start.

A fact that answers nobody's question belongs in the notes. A question the
sources don't answer goes on the owner's list, not the page: don't publish a
finding of your own (a nearest stop, a nearest garage) or an inference from
reviews, and don't add an FAQ to hold it.

### 3. Draft it spoken, then tidy

Write each section the way the owner would say it. Then tidy: exact facts,
filler out, full sentences in running copy. A full sentence has a subject, a
verb and the small words headline style drops (*the, a, we*; *de, het*); "She
runs the screen herself." has a verb and is still shorthand. Labels stay
labels: headings, a hero's one-line sub-line that says what and where, list
items, buttons, captions, field labels, alt text, and label-and-value blocks
such as an hours strip. A section's lead under its heading is running copy.
Don't write a half-way item ("A contingency reserve") that reads as neither.

Fix each fragment on its own terms: give back its subject or verb, reorder it,
make it a question and its answer, or cut it. Don't hang it on the sentence
before with ", and" / ", en"; done everywhere, that joint is the next tic. Two
short complete sentences aren't a fragment, and "a full sentence" never means
"one sentence": an FAQ answer or an error message can take two. Add a
connective only when the link is true (see "Keep it true"). Let sentence length
vary: a short sentence after two longer ones lands, and a page of short ones
reads like a telegram.

Shorter is usually better, but cut by removing what the visitor doesn't need,
not by compressing sentences. Four hundred words in full sentences read better
than four hundred words of fragments. If the owner has asked for less text,
hold the word count as the notes measured it, and say how you counted. When a
fix makes a line longer, make room by cutting a whole sentence or fact the
visitor doesn't need, never the small words that carry the meaning (*specific,
online, only, at least*). If there isn't enough to cut, fix what the visitor
sees first and hand the rest back as one proposal with its word count. If the
owner hasn't asked for less, a count is an estimate, not a limit.

### 4. Headings and page furniture

Most headings just say what's in the section: *Menu*, *Prijzen*, *Reserveren*,
*How we work*, *Contact*. Give one or two lines personality, from the owner's
own words or a true detail. If the owner has no line for the hero, it says
plainly what and where ("Asian fusion aan de haven.") and the personality lives
in a sign-off or the captions; don't write a slogan to fill the slot. Eyebrows,
numbered kickers (01, 02) and a tagline under every heading are template
furniture: keep one only if it tells the visitor something the heading doesn't.
Don't set every heading over two lines with a full stop. Buttons say what
happens: *Reserveer een tafel*, *Bekijk de kaart*, *Send enquiry*.

List the business's personality lines (a karaoke room, a house cocktail, the
person who does the checks) and keep them on every pass. Where one is written
as bare logistics, make it warmer, not only shorter. Before writing into a
slot, see what the design does with it (a two-line split, uppercase, a fixed
width); `references/copy-pass.md`, "Layout", has the checks.

### 5. Review

Run the scanner (below), read every block against the three tests, then read
the page aloud. When a line fails, rewrite it from the visitor's question.
Don't fix a tell by deleting it: taking out a dash by splitting a sentence into
two fragments trades one tic for another. Leave lines that already work alone.
Churn is not improvement, and some lines carry coupled edits (a changed `<h1>`
can mean re-rendering a social card; check the project's notes).

Every replacement line, yours or a reviewer's, gets the same checks: the
sources (did it add a cause, a comparison, a claim?), the three tests, and a
read-aloud beside its neighbours. A reviewer's wording is a draft, not an
approved line. When a review flags something wrong wherever it appears (a
calque, "het ... restaurant"), fix every copy of it on the site, in both
languages. A joint is wrong only in bulk: rework enough of them that
neighbouring sentences don't share it, by changing the sentence rather than
swapping the connective, and leave the rest. A full rewrite is checked from
four angles, and again after a fix round (`references/copy-pass.md`, "Checking a full rewrite").

## Keep it true

Human doesn't mean invented. Don't add stories, family recipes, founding years,
"loved by locals", chef's picks, quotes or numbers the sources don't support.
The page you are rewriting is a draft, not a source: a factual line you keep
needs a source as much as one you add.

Small words carry claims, and rewrites slip them in (worked pairs in
`references/examples.md`):

- labels that imply something: "Favorieten van de chef" (a chef chose them),
  "Nog een tafel reserveren" under a request nobody has confirmed;
- *het / the* before a category ("het Asian fusion restaurant": the only one),
  comparisons (*nearest, best-known*), *eigen / own*, *elke dag / daily*;
- limits: *tot 10* can exclude 10, and "up to 60 minutes before" reads as a
  maximum; check the rule in the code;
- connectives: *so, because, dus, want, omdat* claim a cause, *but, maar* a
  contrast. If the source gives two facts and no link, write two sentences, or
  one clause with a plain *and* / *en* ("staat er al een tijd zo bij en wordt
  niet elke dag bijgewerkt"). Dropping the comma from ", en" is not a fix;
- a rephrase: "de smaken van Korea" (the flavours of) is not "gerechten uit
  Korea" (dishes from);
- what sits together: drinks named beside an all-in price read as included.

Take a legal or privacy condition from the legal page, not from a note that
paraphrases it. A warning that protects some readers (no lift, a height limit)
goes wherever the thing is recommended, and an alternative offered after it
answers only what the sources say it solves. Dish contents come from the menu,
never a photo, and alt text names nothing (a dish, a street, the time of day)
that no source gives. A public rating keeps its source and date. A claim found
in one language is usually also in the other, in meta tags, alt text and
JSON-LD: list each copy before removing it, then search the whole site for its
words. Warmth comes from voice and true details, not made-up ones.

**Locked text** stays as it is unless the owner (or their lawyer) approves a
change: legal and regulatory lines, required disclosures (labels for
AI-generated media, the imprint, a regulatory boundary note), and anything a
project's notes mark as verbatim. Find it before editing, in the notes and in
HTML comments beside the line, and see what each lock covers. **Words**: never
edited; propose a change. **Meaning** or **obligations**: may be reworded (a
*u* to *je* switch included) if every obligation survives. **Figures** (prices,
hours): the sentence may change, the figure and its written form may not
("1,90 meter" is not "1,9 meter"). Where the project names no kind, the lock
covers the words. Each reworded locked line goes on its approver's list. Count
the locked lines before and after the pass (`copy_audit.py locks`); the counts
must match.

House rules still apply. Some projects ban the em dash in customer-facing copy.
Rewrite the sentence; don't swap the dash for a full stop and leave two
fragments.

## What to look for

These are symptoms, not rules to write by: a page can avoid every one and still
sound machine-made, which is why the three tests come first. Each is listed,
with worked fixes, in `references/examples.md`.

- **Written for the auditor** (fails the visitor test): inventory, counting for
  its own sake, the page talking about itself ("De foto's zijn echt."), the same
  explanation twice, a checklist pasted in as copy, narrating the photos.
- **Written to a template** (fails the swap test): stock phrases (*discover,
  in the heart of, come for X stay for Y*; *ontdek, beleef, met gevoel*), lists
  of three, slogan shapes, colon setups and dash joints, one joint in line
  after line (", en", ", dus"), every section built the same way. If the owner
  uses a stock word (*genieten*), drop the brochure shape, not the word.
- **Over-cut** (fails the counter test): fragments and shorthand ("A clear
  role."), curt instructions, translated word order, official words (*prior
  to*; *middels, dient u te*), every sentence the same length.

## Genre notes

**Restaurants, cafes, bars, hotels.** Short, warm, practical, a little
informal. The menu and the photos do most of the talking. A good page has one
line on what the place is, a clear way to book or order, the menu with prices,
hours and address, and the practical answers (groups, dietary needs, parking)
in plain sentences. Dish descriptions work the way a menu does: what it is and
what's in it, with an adjective only when it's a fact (*gegrild op
houtskool*, *huisgemaakt*). A yes/no FAQ answer starts with *Ja,* or *Nee,*
and goes on as a full sentence; an open question's answer starts with the
answer, never "zie hierboven". Directions are for getting there: the nearest
stop (if the sources give one) and the parking catch (a height limit, no lift)
beat night-bus numbers.

**Professional services, B2B, finance.** Calm, specific, first person plural.
Say what you do in your clients' words. Industry terms are fine when the reader
uses them (an energy developer knows EPC and COD); don't explain what they
know, and don't stack terms to sound serious. Show you're careful through a few
concrete specifics, not a compliance voice. Regulatory lines stay exact, said
once, plainly. Service verbs (*arrange, advise, manage*), assurance words
(*vetted, suitable, bankable*) and agency words (*on your behalf, recommend*)
can claim regulated activity: check each where the firm is the subject.

**Forms and status messages.** Before rewording an error, hint or status line,
find out when it appears (read the code path, or trigger it), write for that
case and keep its scope words (*online*, *today*). A sent request isn't a
booking: "Send another request", not "Reserve another table". Where the site
may not promise a reply, check every string for paraphrases too ("you'll hear
from us", "u krijgt een bevestiging").

**Bilingual sites.** Write each language natively; a weak slogan gets weaker
as a calque. Native doesn't mean different: compare the twins claim by claim
(*bij jou thuis* is at home, not "at your table"). Legal and locked text is
translated faithfully, with the original as the reference. A length limit
holds for each language on its own. English readers of a Dutch restaurant site
want the practical lines first. Read `references/nederlands.md` before writing
or reviewing either language of a Dutch site, including a fix round that
touches only the English: its last section covers the English twin and the
calques seen so far.

## The scanner

`scripts/copy_audit.py` (Python 3, standard library only) reads a page the way
a visitor does and points at likely problems.

```bash
python3 <skill-dir>/scripts/copy_audit.py extract <page.html | directory | URL | messages/nl.json | draft.md>
python3 <skill-dir>/scripts/copy_audit.py scan <same inputs>   # --json, --locked FILE, --alt-words N
python3 <skill-dir>/scripts/copy_audit.py locks --locked FILE <site directory or .html/.js/.json files>
```

`<skill-dir>` is `~/.claude/skills/website-copy` device-wide, or
`.claude/skills/website-copy` inside a repo.

- `extract` lists the visible copy in reading order, tagged by role (h1, p,
  li, button, alt, meta; `state` for hidden errors and success messages).
  Start every review here.
- `scan` flags likely tells per block, then repeats, one joint used again and
  again, short lines standing alone, heading shapes and rhythm. Its length
  figure is visible words in `<main>` by section, leaving out hidden states,
  numbered kickers, aria-hidden text and attributes. `--alt-words N` takes the
  project's alt-text limit.
- No JavaScript runs, so copy a script injects (a language dictionary, form
  messages) is missing, and the scan says so when it sees `data-i18n`. Scan
  built HTML or a page saved from a browser. A JSON catalog can be scanned
  directly; a JavaScript dictionary is read by hand.
- `--locked FILE` takes the lines kept word for word, one per line (or a `.md`
  file's `locked` fences). Sentences holding one are never flagged and are left
  out of the rhythm, the running-copy count, the repeats and the joints; the
  visible-words figure still counts them. Use unique substrings: "per persoon"
  would exempt every sentence with it. `locks` counts each locked line in the
  raw source, JavaScript escapes included, before and after a pass.

It is a flashlight, not a linter. Many flags are fine on a second read, and a
clean scan proves nothing: it can't see an unsupported claim ("Een geliefde
plek") or a false cause. Never edit to make a flag go away; rewrite from the
visitor's question and read it aloud. Project checks import `load`, `analyse`
and `render_scan`; keep them stable. Tests: `python3 -B
scripts/test_copy_audit.py` (`-B` keeps bytecode out of the skill folder).

## A full copy pass

Rewriting a whole site, rather than a line or a section, has its own mechanics:
every place a string lives (both languages, script dictionaries, meta tags,
other pages), what the layout does with a slot, and how to check the rewrite
from independent angles. Read `references/copy-pass.md` before starting one.

## What to hand back

When rewriting a page or a section, give the owner something they can approve
line by line. Write it into the project's notes as a dated `## Copy pass,
<date>` section after the site's rules (or a dated entry in the project's
build-notes file), in this order, and give the same in chat:

1. **Before / after / why**, per page: the key or place, the language, before,
   after, and a one-line why naming the visitor's question or the test the old
   line failed. Open with a line on how to read it.
2. **Facts taken off the page**, and where each is still kept, including lines
   whose meaning narrowed, so the owner is never surprised.
3. **Claims removed as unsupported**, listed on their own with every copy
   (language, page, meta tag). The owner may have the evidence you didn't.
4. **Changes beyond the words**: markup or layout a line needs, every other
   place the same string lives, no-break joins, and HTML comments corrected.
5. **Locked lines**, with the counts before and after.
6. **Waiting for approval**: applied but waiting for an OK (with the text to
   restore), and not applied (locked words, markup, CSS, code), each with its
   approver: owner, lawyer or designer.
7. **Questions for the owner**, numbered after the last existing one and never
   renumbered, each with the default that holds until it is answered.
8. **What is still wrong**: held or locked lines, or lines over the word count,
   that still fail a test; where you read a rule broadly; and review findings
   you didn't apply, with why.
9. **Word counts and scans, before and after**, with how you counted and the
   widths checked. Keep it brief, and don't chase the numbers.

If the project keeps a sitemap with `lastmod`, move it for every page the pass
changed.
