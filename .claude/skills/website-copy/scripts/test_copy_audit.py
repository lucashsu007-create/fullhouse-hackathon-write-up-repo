#!/usr/bin/env python3
"""Tests for copy_audit.py.

Stdlib unittest, like the other user-level skills: no dependency on a pytest
that happens to be installed in some project's venv. Run either way:

    python3 scripts/test_copy_audit.py
    python3 -m unittest discover -s scripts -p 'test_*.py'

Fixtures are the failure patterns found on real client pages, with names removed.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True  # no __pycache__ left behind in the repos this skill is copied into

import copy_audit as ca  # noqa: E402

EM = chr(0x2014)
EN_DASH = chr(0x2013)


def page(body: str, lang: str = "nl", head: str = "") -> str:
    return f'<!doctype html><html lang="{lang}"><head><title>Test</title>{head}</head><body>{body}</body></html>'


def blocks(html: str) -> list:
    return [b for b in ca.blocks_from_html(html, "t.html")[0] if b.role != "title"]


def report(html: str) -> dict:
    found, info = ca.blocks_from_html(html, "t.html")
    return ca.analyse("t.html", found, info)


def flagged(html: str) -> dict:
    """check name -> list of flagged texts."""
    out: dict = {}
    for f in report(html)["findings"]:
        out.setdefault(f["check"], []).append(f["text"])
    return out


class Extraction(unittest.TestCase):
    def test_inline_link_stays_in_its_sentence(self):
        got = blocks(page('<p>Prices come from <a href="#">Nord Pool</a> for the Baltic markets.</p>', "en"))
        self.assertEqual([(b.role, b.text) for b in got], [("p", "Prices come from Nord Pool for the Baltic markets.")])

    def test_navigation_links_are_separate_link_blocks(self):
        got = blocks(page('<nav><a href="/">Menu</a><a href="/b">Bestellen</a><a href="/c">Bezoek ons</a></nav>'))
        self.assertEqual([(b.role, b.text) for b in got], [("link", "Menu"), ("link", "Bestellen"), ("link", "Bezoek ons")])

    def test_sibling_spans_do_not_glue_together(self):
        got = blocks(page("<p><span>Elke dag open</span><span>17:00 - 22:15</span></p><p><em>Huis</em>-wijn</p>"))
        self.assertEqual([b.text for b in got], ["Elke dag open 17:00 - 22:15", "Huis-wijn"])

    def test_scripts_styles_and_hidden_elements_are_skipped(self):
        html = page(
            "<style>p{color:red}</style><script>var t = 'Ontdek alles';</script>"
            "<div hidden><p>Verborgen tekst</p></div><svg><text>Logo</text></svg><p>Zichtbaar</p>"
        )
        self.assertEqual([b.text for b in blocks(html)], ["Zichtbaar"])

    def test_attribute_copy_is_extracted_and_meta_is_deduplicated(self):
        head = (
            '<meta name="description" content="Sushi in De Pijp.">'
            '<meta property="og:description" content="Sushi in De Pijp.">'
        )
        html = page('<img src="a.jpg" alt="De eetzaal in de avond"><input placeholder="Uw naam">', head=head)
        got = [(b.role, b.text) for b in blocks(html)]
        self.assertEqual(got, [("meta", "Sushi in De Pijp."), ("alt", "De eetzaal in de avond"), ("placeholder", "Uw naam")])

    def test_br_inside_a_heading_is_a_split_not_a_new_block(self):
        (heading,) = blocks(page("<h2>Een tafel voor<br>een lange avond.</h2>"))
        self.assertEqual((heading.role, heading.text, heading.splits), ("h2", "Een tafel voor een lange avond.", 1))

    def test_br_outside_a_heading_separates_blocks(self):
        got = blocks(page("<p>Voorbeeldstraat 4<br>1072 AB Amsterdam</p>"))
        self.assertEqual([b.text for b in got], ["Voorbeeldstraat 4", "1072 AB Amsterdam"])

    def test_paragraph_inside_a_list_item_is_list_content(self):
        (item,) = blocks(page("<ul><li><p>Equity, mezzanine, vendor and equipment-linked finance.</p></li></ul>", "en"))
        self.assertEqual(item.role, "li")

    def test_numbered_kickers_are_counted_bare_or_fused_with_a_label(self):
        found, info = ca.blocks_from_html(
            page("<span>01</span><h2>Menu</h2><span>02 de sfeer</span><h2>Prijzen</h2><p>MCHI / 03</p>"), "t")
        self.assertEqual(info["kickers"], 2)
        self.assertEqual([(b.role, b.text) for b in found if b.text.startswith("0")],
                         [("kicker", "01"), ("text", "02 de sfeer")])

    def test_lines_without_letters_stay_unless_they_are_icons(self):
        got = blocks(page("<p>020 123 4567</p><p>17:00 - 22:15</p><span>\u2197</span><button>\u2192</button>"))
        self.assertEqual([b.text for b in got], ["020 123 4567", "17:00 - 22:15"])

    def test_aria_hidden_content_is_shown_as_decor_and_never_checked(self):
        html = page('<div aria-hidden="true"><label>Ontdek onze website</label></div><p>Echte tekst hier.</p>')
        self.assertEqual([(b.role, b.text) for b in blocks(html)][0], ("decor", "Ontdek onze website"))
        self.assertNotIn("stock", flagged(html))

    def test_social_image_alt_is_alt_text(self):
        head = '<meta property="og:image:alt" content="Het logo op een donkere achtergrond">'
        self.assertEqual([(b.role, b.text) for b in blocks(page("", head=head))],
                         [("alt", "Het logo op een donkere achtergrond")])

    def test_language_comes_from_the_page_and_is_refined_per_block(self):
        html = page("<p>Wij zijn elke dag open en de keuken sluit om tien uur.</p><p lang=\"en\">Open every day.</p>"
                    "<p>We are open every day and the kitchen closes at ten.</p>")
        self.assertEqual([b.lang for b in blocks(html)], ["nl", "en", "en"])

    def test_markdown_draft(self):
        found, info = ca.blocks_from_text("# Over ons\n\nWe koken elke dag vers.\nMet de hand.\n\n- Sushi\n- Sashimi\n", "d.md")
        self.assertEqual([(b.role, b.text) for b in found],
                         [("h1", "Over ons"), ("p", "We koken elke dag vers. Met de hand."), ("li", "Sushi"), ("li", "Sashimi")])
        self.assertEqual(info["lang"], "nl")

    def test_json_catalog_keeps_copy_and_skips_code_like_values(self):
        data = {"hero": {"title": "Kom voor de sushi, blijf voor de sfeer.", "cta": "Reserveer"},
                "href": "https://example.nl/menu", "key": "hero.title", "items": ["Vers <b>gerold</b>"]}
        found, info = ca.blocks_from_catalog(data, "nl.json", "nl")
        self.assertEqual([(b.where, b.text) for b in found],
                         [("nl.json#hero.title", "Kom voor de sushi, blijf voor de sfeer."),
                          ("nl.json#hero.cta", "Reserveer"), ("nl.json#items[0]", "Vers gerold")])
        self.assertEqual(info["lang"], "nl")


class Sentences(unittest.TestCase):
    def test_abbreviations_decimals_and_times_do_not_split(self):
        self.assertEqual(ca.sentences("Wij serveren o.a. Japanse gerechten. Beoordeeld met een 4.6 om 13:00. Klaar."),
                         ["Wij serveren o.a. Japanse gerechten.", "Beoordeeld met een 4.6 om 13:00.", "Klaar."])

    def test_a_lowercase_continuation_is_not_a_new_sentence(self):
        self.assertEqual(ca.sentences("Open ma. t/m vr. vanaf twaalf uur."), ["Open ma. t/m vr. vanaf twaalf uur."])

    def test_words_count_tokens_with_a_letter_or_digit(self):
        self.assertEqual(len(ca.words(f"EUR 2,30 per 15 minuten {EM} echt")), 6)


class BlockChecks(unittest.TestCase):
    def test_stock_slogans_in_both_languages(self):
        got = flagged(page("<p>Kom voor de sushi, blijf voor de sfeer.</p><h2>Ontdek onze kaart</h2>"))
        self.assertEqual(len(got["stock"]), 2)
        got = flagged(page("<p>Come for the sushi, stay for the mood.</p><p>A culinary journey in the heart of town.</p>", "en"))
        self.assertEqual(len(got["stock"]), 2)

    def test_plain_practical_copy_is_not_flagged(self):
        html = page("<p>Reserveren kan online of telefonisch. We zijn elke dag open van 13:00 tot 23:00, "
                    "en de keuken sluit om half elf.</p><p>Liever afhalen? Bel ons, dan staat het over "
                    "twintig minuten klaar.</p>")
        self.assertEqual(flagged(html), {})

    def test_meta_talk(self):
        got = flagged(page("<p>No firm is named here, and both sides are checked.</p><h2>On this site</h2>", "en"))
        self.assertEqual(got["meta-talk"], ["No firm is named here, and both sides are checked."])
        got = flagged(page("<p>Bewegende beelden zijn met AI gemaakt. De foto's zijn echt.</p>"))
        self.assertIn("meta-talk", got)

    def test_one_finding_per_check_per_block(self):
        got = report(page("<p>No firm is named here, and no client is listed on this site.</p>", "en"))
        meta = [f for f in got["findings"] if f["check"] == "meta-talk"]
        self.assertEqual(len(meta), 1)
        self.assertIn(";", meta[0]["note"])

    def test_dash_joints_but_not_ranges_or_phone_numbers(self):
        got = flagged(page(f"<p>Vers gesneden {EM} elke dag.</p><p>Sushi {EN_DASH} en meer.</p>"
                           f"<p>Open 17:00 - 22:15, bel 020 {EN_DASH} 123 45 67.</p>"))
        self.assertEqual(got["dash"], [f"Vers gesneden {EM} elke dag.", f"Sushi {EN_DASH} en meer."])

    def test_staccato_run_of_short_sentences(self):
        got = flagged(page("<p>Few get in. The screen runs both ways.</p>"
                           "<p>We are open every day from 13:00. The kitchen closes at 22:30.</p>", "en"))
        self.assertEqual(got["staccato"], ["Few get in. The screen runs both ways."])

    def test_colon_setups_as_a_habit_but_not_labels_or_times(self):
        setups = ["Sushi, sashimi, teppanyaki, wok en grill: meer dan honderd gerechten.",
                  "Kijk even binnen: acht beelden uit onze eetzaal."]
        got = flagged(page("".join(f"<p>{s}</p>" for s in setups) +
                           "<p>E-mail: info@example.org</p><p>Open vanaf 17:00 op zondag en maandag.</p>"))
        self.assertEqual(got["colon"], setups)

    def test_a_single_colon_setup_is_not_reported(self):
        self.assertNotIn("colon", flagged(page("<p>Sushi, sashimi, wok en grill: meer dan honderd gerechten.</p>")))

    def test_triad_in_short_display_lines_not_in_lists(self):
        got = flagged(page("<p>Een avond vol smaak, vuur en verrassingen.</p>"
                           "<ul><li>Tonijn, zalm en sint-jakobsschelp.</li></ul>"))
        self.assertEqual(got["triad"], ["Een avond vol smaak, vuur en verrassingen."])

    def test_triad_is_found_inside_a_longer_paragraph(self):
        para = ("Een avond vol smaak, vuur en verrassingen. Van sushi en sashimi tot gerechten "
                "met een eigen twist van de chef.")
        self.assertEqual(flagged(page(f"<p>{para}</p>"))["triad"], [para])

    def test_a_question_followed_by_a_link_label_is_not_clipped(self):
        got = flagged(page('<p>Zin gekregen? <a href="/menu">Bekijk de kaart</a></p>'
                           "<p>Thank you. It has been sent.</p>"))
        self.assertEqual(got.get("staccato"), ["Thank you. It has been sent."])

    def test_dutch_fronted_clause_but_not_a_real_question(self):
        got = flagged(page("<p>Welke tijden u per dag kunt reserveren, ziet u in het reserveringssysteem.</p>"
                           "<p>Welke tijden zijn er vandaag nog vrij, ziet u dat ook?</p>"))
        self.assertEqual(got["word-order"], ["Welke tijden u per dag kunt reserveren, ziet u in het reserveringssysteem."])

    def test_dutch_title_case_but_not_proper_names(self):
        got = flagged(page("<h2>Bekijk Onze Hele Kaart</h2><h2>Kom langs op het Frederik Hendrikplein</h2>"
                           "<h2>Sushi Fusion All You Can Eat</h2>"))
        self.assertEqual(got["title-case"], ["Bekijk Onze Hele Kaart"])

    def test_stiff_words_suggest_the_everyday_one(self):
        got = report(page("<p>Wij verzoeken u middels dit formulier uw gegevens te sturen.</p>"))
        (stiff,) = [f for f in got["findings"] if f["check"] == "stiff"]
        self.assertIn('"middels" -> met, via', stiff["note"])
        self.assertIn("stiff", flagged(page("<p>Please be advised that we utilise local produce.</p>", "en")))

    def test_inventory_alt_text(self):
        long_alt = ("De eetzaal in de avond: marmeren wanden met warme ledlijnen, zes rotan hanglampen, "
                    "blauwe fluwelen banken en gedekte houten tafels met waxinelichtjes.")
        got = flagged(page(f'<img alt="{long_alt}"><img alt="De eetzaal in de avond">'))
        self.assertEqual(got["alt"], [long_alt])

    def test_dense_figures_and_counting_headings_but_not_dish_numbers(self):
        dossier = ("Metro 52 stopt op vier minuten lopen. Vanaf Centraal is dat drie haltes. "
                   "Tram 1, 7 en 19 en nachtbus N82 stoppen ook.")
        got = flagged(page(f"<p>{dossier}</p><h2>Zestien gerechten van de plaat</h2><h3>36 Unagi</h3>"
                           "<h3>Four Seasons of Mochi</h3><p>Bel 020 123 4567 of reserveer online.</p>"))
        self.assertEqual(got["figures"], [dossier, "Zestien gerechten van de plaat"])


class PageChecks(unittest.TestCase):
    def test_repeated_sentences(self):
        got = report(page("<p>De keuken sluit om 22:30.</p><p>Elke dag open. De keuken sluit om 22:30.</p>"
                          "<p>Bel ons.</p><p>Bel ons.</p>"))
        self.assertEqual([(r["text"], r["count"]) for r in got["repeats"]], [("De keuken sluit om 22:30.", 2)])

    def test_template_heading_shapes(self):
        body = "".join(f"<span>0{i}</span><h2>Kop {i} voor<br>een avond.</h2>" for i in range(1, 5))
        heads = report(page(body))["headings"]
        self.assertEqual((len(heads["ending_in_full_stop"]), len(heads["set_over_lines"]), heads["numbered_kickers"]), (4, 4, 4))
        self.assertIn("heading shape", ca.render_scan(report(page(body))))

    def test_eyebrows_above_headings_are_counted(self):
        body = ("<p>Van de kaart</p><h2>Favorieten</h2><p>Praktisch</p><h2>Tot straks</h2>"
                "<p>Vanavond nog?</p><h2>Reserveren</h2><p>Dit is een gewone zin.</p><h2>Contact</h2>")
        self.assertEqual(report(page(body))["headings"]["eyebrows"], 3)

    def test_captions_and_labels_do_not_count_as_sentences(self):
        labels = "".join(f"<figure><figcaption>Warm licht {i}</figcaption></figure><p>Elke dag open</p>"
                         for i in range(10))
        self.assertEqual(report(page(labels + "<p>We zijn er elke avond vanaf vijf uur, ook op zondag.</p>"))
                         ["rhythm"]["sentences"], 1)

    def test_locked_lines_are_counted_but_not_flagged(self):
        html = page("<p>Nothing on this website is an offer or investment advice.</p>"
                    "<p>No firm is named here, and both sides are checked.</p>", "en")
        found, info = ca.blocks_from_html(html, "t.html")
        got = ca.analyse("t.html", found, info, locked=("nothing on this website",))
        self.assertEqual([f["text"] for f in got["findings"]], ["No firm is named here, and both sides are checked."])
        self.assertEqual(got["locked_blocks"], 1)

    def test_clipped_rhythm_is_noted_and_varied_rhythm_is_not(self):
        clipped = "".join(f"<p>{s}</p>" for s in [
            "She runs the screen herself.", "A clear role.", "Contractor and funding arrive together.",
            "Current projects are in Latvia.", "Both sides are checked.", "Tell us which side you are on.",
            "Keep it short.", "Projects sourced through relationships.", "No firm is named."])
        self.assertEqual(report(page(clipped, "en"))["rhythm"]["note"], "clipped")
        varied = "".join(f"<p>{s}</p>" for s in [
            "We look for energy projects that need funding, and we check each one before any investor hears about it.",
            "That takes time.",
            "Most of it goes into the grid connection and the land, because a project without either will not get built.",
            "Investors go through the same process, so both sides know who they are talking to.",
            "Then we introduce them.",
            "If a number cannot be traced back to a published source, we stop there and say why.",
            "Our CEO reviews every file herself before anyone is introduced to anyone else.",
            "A short first message is enough to start the conversation."])
        self.assertEqual(report(page(varied, "en"))["rhythm"]["note"], "")


class Cli(unittest.TestCase):
    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = ca.main(list(argv))
        return code, out.getvalue()

    def test_scan_and_extract_on_a_file_and_a_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            (site / "index.html").write_text(page("<h1>Ontdek onze kaart</h1><p>Kom voor de sushi, blijf voor de sfeer.</p>"), encoding="utf-8")
            (site / "node_modules").mkdir()
            (site / "node_modules" / "skipme.html").write_text(page("<p>Niet scannen</p>"), encoding="utf-8")
            code, text = self.run_cli("scan", str(site))
            self.assertEqual(code, 0)
            self.assertIn("stock phrase (2)", text)
            self.assertNotIn("skipme", text)
            code, text = self.run_cli("extract", str(site / "index.html"))
            self.assertEqual(code, 0)
            self.assertIn("h1", text)
            code, text = self.run_cli("scan", "--json", str(site / "index.html"))
            self.assertEqual(json.loads(text)[0]["lang"], "nl")

    def test_missing_input_exits_2(self):
        self.assertEqual(self.run_cli("scan", "/no/such/page.html")[0], 2)

    def test_locked_file_on_the_command_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            html, locked = Path(tmp) / "p.html", Path(tmp) / "locked.txt"
            html.write_text(page("<p>Ontdek de kaart in het hart van de stad.</p>"), encoding="utf-8")
            locked.write_text("# owner-approved wording\nin het hart van de stad\n", encoding="utf-8")
            code, text = self.run_cli("scan", "--json", "--locked", str(locked), str(html))
            self.assertEqual((code, json.loads(text)[0]["findings"], json.loads(text)[0]["locked_blocks"]), (0, [], 1))


if __name__ == "__main__":
    unittest.main(verbosity=1)
