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
        got = blocks(page('<p>Prices come from <a href="#">the exchange</a> for the northern markets.</p>', "en"))
        self.assertEqual([(b.role, b.text) for b in got], [("p", "Prices come from the exchange for the northern markets.")])

    def test_navigation_links_are_separate_link_blocks(self):
        got = blocks(page('<nav><a href="/">Menu</a><a href="/b">Bestellen</a><a href="/c">Bezoek ons</a></nav>'))
        self.assertEqual([(b.role, b.text) for b in got], [("link", "Menu"), ("link", "Bestellen"), ("link", "Bezoek ons")])

    def test_sibling_spans_do_not_glue_together(self):
        got = blocks(page("<p><span>Elke dag open</span><span>17:00 - 22:00</span></p><p><em>Huis</em>-wijn</p>"))
        self.assertEqual([b.text for b in got], ["Elke dag open 17:00 - 22:00", "Huis-wijn"])

    def test_scripts_and_styles_are_skipped_and_hidden_text_is_a_state(self):
        html = page(
            "<style>p{color:red}</style><script>var t = 'Ontdek alles';</script>"
            "<div hidden><p>Verborgen tekst</p></div><svg><text>Logo</text></svg><p>Zichtbaar</p>"
        )
        self.assertEqual([(b.role, b.text) for b in blocks(html)], [("state", "Verborgen tekst"), ("p", "Zichtbaar")])

    def test_attribute_copy_is_extracted_and_meta_is_deduplicated(self):
        head = (
            '<meta name="description" content="Sushi aan de gracht.">'
            '<meta property="og:description" content="Sushi aan de gracht.">'
        )
        html = page('<img src="a.jpg" alt="De eetzaal in de avond"><input placeholder="Uw naam">', head=head)
        got = [(b.role, b.text) for b in blocks(html)]
        self.assertEqual(got, [("meta", "Sushi aan de gracht."), ("alt", "De eetzaal in de avond"), ("placeholder", "Uw naam")])

    def test_br_inside_a_heading_is_a_split_not_a_new_block(self):
        (heading,) = blocks(page("<h2>Een tafel voor<br>een lange avond.</h2>"))
        self.assertEqual((heading.role, heading.text, heading.splits), ("h2", "Een tafel voor een lange avond.", 1))

    def test_br_outside_a_heading_separates_blocks(self):
        got = blocks(page("<p>Voorbeeldstraat 4<br>1011 AB Amsterdam</p>"))
        self.assertEqual([b.text for b in got], ["Voorbeeldstraat 4", "1011 AB Amsterdam"])

    def test_paragraph_inside_a_list_item_is_list_content(self):
        (item,) = blocks(page("<ul><li><p>Equity, mezzanine, vendor and equipment-linked finance.</p></li></ul>", "en"))
        self.assertEqual(item.role, "li")

    def test_numbered_kickers_are_counted_bare_or_fused_with_a_label(self):
        found, info = ca.blocks_from_html(
            page("<span>01</span><h2>Menu</h2><span>02 de sfeer</span><h2>Prijzen</h2><p>NAAM / 03</p>"), "t")
        self.assertEqual(info["kickers"], 2)
        self.assertEqual([(b.role, b.text) for b in found if b.text.startswith("0")],
                         [("kicker", "01"), ("text", "02 de sfeer")])

    def test_lines_without_letters_stay_unless_they_are_icons(self):
        got = blocks(page("<p>020 123 4567</p><p>17:00 - 22:00</p><span>\u2197</span><button>\u2192</button>"))
        self.assertEqual([b.text for b in got], ["020 123 4567", "17:00 - 22:00"])

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

    def test_a_wrapped_markdown_list_item_stays_one_item(self):
        text = ("- Good: Voor groepen hebben we een aparte ruimte,\n  waar je ook karaoke kunt zingen. Bel ons even.\n"
                "- Bad: Aparte ruimte.\nLazy continuation line.\n\nA paragraph.\n")
        found, _ = ca.blocks_from_text(text, "rules.md")
        self.assertEqual([(b.role, b.text) for b in found], [
            ("li", "Good: Voor groepen hebben we een aparte ruimte, waar je ook karaoke kunt zingen. Bel ons even."),
            ("li", "Bad: Aparte ruimte. Lazy continuation line."), ("p", "A paragraph.")])

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
        self.assertEqual(len(ca.words(f"EUR 1,80 per 15 minuten {EM} echt")), 6)


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
                           f"<p>Open 17:00 - 22:00, bel 020 {EN_DASH} 123 45 67.</p>"))
        self.assertEqual(got["dash"], [f"Vers gesneden {EM} elke dag.", f"Sushi {EN_DASH} en meer."])

    def test_staccato_run_of_short_sentences(self):
        got = flagged(page("<p>Few make it. The check runs both ways.</p>"
                           "<p>We are open every day from 13:00. The kitchen closes at 22:30.</p>", "en"))
        self.assertEqual(got["staccato"], ["Few make it. The check runs both ways."])

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
        dossier = ("Metro 58 stopt op vier minuten lopen. Vanaf het station is dat drie haltes. "
                   "Tram 3, 8 en 21 en nachtbus N90 stoppen ook.")
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
            "He runs the checks himself.", "A clear process.", "Builders and funding arrive together.",
            "Current sites are up north.", "Both sides are checked.", "Tell us what you need.",
            "Keep it short.", "Deals sourced through relationships.", "No firm is named."])
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


class Extraction2(unittest.TestCase):
    def test_textarea_placeholder_is_shown(self):
        got = blocks(page('<textarea name="message" placeholder="Two or three lines will do"></textarea>', "en"))
        self.assertEqual([(b.role, b.text) for b in got], [("placeholder", "Two or three lines will do")])

    def test_per_language_alt_text_is_extracted_when_it_differs(self):
        html = page('<img alt="Een lichtblauwe cocktail met munt." data-alt-nl="Een lichtblauwe cocktail met munt." '
                    'data-alt-en="A pale blue cocktail with mint in a tall glass on the bar beside a candle">')
        self.assertEqual([(b.role, b.lang) for b in blocks(html)], [("alt", "nl"), ("alt", "en")])

    def test_hidden_status_line_is_a_state_and_script_swapped_copy_is_reported(self):
        html = page('<p data-i18n="lead">Reserveer online.</p><p hidden>Komt u met meer dan 10 personen? Bel ons dan even.</p>')
        self.assertIn(("state", "Komt u met meer dan 10 personen? Bel ons dan even."), [(b.role, b.text) for b in blocks(html)])
        self.assertIn("script-swapped: 1", ca.render_scan(report(html)))


class NewChecks(unittest.TestCase):
    RESERVE = [  # a reservation page after a pass that fixed every fragment with ", en" (figures changed)
        "Voor groepen tot en met 16 personen hebben we een aparte ruimte, en daar kun je ook een eigen menu kiezen.",
        "Tot en met 11 personen is je reservering meteen bevestigd, en bij 12 kijken we er eerst zelf naar.",
        "Lunch en diner reserveer je in hetzelfde formulier, en daar zie je per dag welke tijden je kunt kiezen.",
        "De eindtijd staat vast, en je ziet hem in het formulier voordat je bevestigt.",
        "Het formulier is er in het Nederlands en in het Engels, en online reserveer je voor maximaal 16 personen.",
        "Reserveren kan online of telefonisch.", "We zijn elke dag open vanaf vijf uur.",
        "Bel ons even als je later komt dan gepland.", "Kinderstoelen hebben we bij de ingang staan.",
        "Voor een verjaardag mag je zelf een taart meenemen.", "Honden zijn welkom op het terras.",
        "Op zondag is de keuken tot negen uur open.",
    ]

    def test_the_same_joint_in_sentence_after_sentence(self):
        got = report(page("".join(f"<p>{s}</p>" for s in self.RESERVE)))["joints"]
        self.assertEqual([(j["joint"], j["count"], j["of"]) for j in got], [(", en", 5, 12)])
        self.assertIn("same joint (5 of 12", ca.render_scan(report(page("".join(f"<p>{s}</p>" for s in self.RESERVE)))))
        two = self.RESERVE[:2] + self.RESERVE[5:]
        self.assertEqual(report(page("".join(f"<p>{s}</p>" for s in two)))["joints"], [])

    def test_list_tails_are_not_joints(self):
        lists = ["We look at solar, wind, and storage projects in the north.",
                 "Buyers ask about land, grid, and permits before anything else.",
                 "We check the budget, the schedule, and the contracts.",
                 "The first call covers the site, the connection, and the timing.",
                 "Funding can be equity, mezzanine, or debt."]
        self.assertEqual(report(page("".join(f"<p>{s}</p>" for s in lists), "en"))["joints"], [])

    def test_the_clipped_note_does_not_prescribe_connectives(self):
        self.assertNotRegex(ca.RHYTHM_NOTES["clipped"], r"because, so, but|omdat, dus, maar")

    def test_a_menu_is_not_running_copy_and_a_price_is_not_an_eyebrow(self):
        dish = "<h3>Nigiri zalm (2 st.)</h3><p>Rauwe zalm met avocado, sesam en wasabimayonaise.</p><span>{}</span>"
        prices = ("\u20ac 6,00", "EUR 6,00", "6,00", "\u20ac\u00a012,50")
        html = page("<h2>Kaart</h2>" + "".join(dish.format(p) for p in prices)
                    + "<h2>Over ons</h2><p>Een avond vol smaak, vuur en verrassingen.</p>")
        got = report(html)
        self.assertEqual([f["text"] for f in got["findings"] if f["check"] == "triad"],
                         ["Een avond vol smaak, vuur en verrassingen."])
        self.assertEqual((got["headings"]["eyebrows"], got["rhythm"]["sentences"]), (0, 1))

    def test_alt_limit_comes_from_the_project(self):
        alt = "A pale blue cocktail with mint in a tall glass on the bar beside a candle"  # 16 words
        found, info = ca.blocks_from_html(page(f'<img alt="{alt}">', "en"), "t.html")
        self.assertEqual([f["note"] for f in ca.analyse("t", found, info, alt_words=15)["findings"]], ["16 words, limit 15"])
        self.assertEqual(ca.analyse("t", found, info)["findings"], [])
        inventory = "Tafels, banken, lampen, planten, kaarsen en een bar."  # short, but a list of what is in the room
        found, info = ca.blocks_from_html(page(f'<img alt="{inventory}">'), "t.html")
        self.assertEqual([f["note"] for f in ca.analyse("t", found, info)["findings"]], ["8 words, 4 commas"])

    def test_sentence_case_label_ending_in_a_name_is_not_title_case(self):
        got = flagged(page("<a href='#'>Route naar de Anna Bijnsgarage</a><h2>Bekijk Onze Hele Kaart</h2>"))
        self.assertEqual(got["title-case"], ["Bekijk Onze Hele Kaart"])
        got = flagged(page("<h2>Sushi met Liefde Bereid</h2><h2>Parkeren bij de Anna Bijnsgarage</h2>"))
        self.assertEqual(got["title-case"], ["Sushi met Liefde Bereid"])

    def test_hours_may_repeat(self):
        self.assertEqual(report(page("<dl><dd>Elke dag 12:00 tot 22:00</dd></dl><p>Elke dag 12:00 tot 22:00</p>"))["repeats"], [])

    def test_answers_and_places_are_not_lists_of_three(self):
        got = flagged(page("<p>Kom even binnen kijken, in de zaal en bij de bar.</p>"
                           "<p>Nee, we bezorgen niet en afhalen kan ook niet.</p>"
                           "<p>Een avond vol smaak, vuur en verrassingen.</p>"))
        self.assertEqual(got["triad"], ["Een avond vol smaak, vuur en verrassingen."])
        self.assertNotIn("triad", flagged(page("<p>All you can eat is available every day, at lunch and at dinner.</p>", "en")))
        self.assertIn("triad", flagged(page("<p>No fuss, no frills and no hidden costs.</p>", "en")))

    def test_a_dash_between_names_in_a_printed_list_is_a_separator(self):
        got = flagged(page("<ul><li>Huiswijn wit - Domaine Exemple (Frankrijk - Languedoc)</li></ul>"
                           f"<p>Sushi {EN_DASH} en meer.</p><h3>COCKTAILS - SWEET</h3>"))
        self.assertEqual(got["dash"], [f"Sushi {EN_DASH} en meer.", "COCKTAILS - SWEET"])
        got = flagged(page("<ul><li>Fast setup - No code needed</li><li>Sake - Junmai 300 ml</li></ul>", "en"))
        self.assertEqual(got["dash"], ["Fast setup - No code needed"])

    def test_visible_words_in_main_by_section(self):
        html = page('<header><nav><a href="/">Menu</a></nav></header><main><h1>Wind and solar sites, checked</h1>'
                    "<p>We find projects that need funding.</p><section><h2>What we do</h2><ul><li>Project search</li>"
                    "<li>Checks on both sides</li></ul></section><section><h2>Contact</h2><p>Tell us what you are "
                    'working on.</p><input placeholder="Your name"></section></main><footer><p>Registered in the '
                    "Netherlands.</p></footer>", "en")
        got = report(html)
        self.assertEqual((got["visible_words"], got["words_by_section"]), (28, {"top": 11, "What we do": 9, "Contact": 8}))

    def test_lone_short_lines_are_listed_for_a_reread(self):
        lines = ["Builders and funding arrive together.", "Including early-stage project companies.",
                 "Deals sourced through direct relationships.", "He checks both sides himself."]
        self.assertEqual(len(report(page("".join(f"<p>{s}</p>" for s in lines), "en"))["lone_short"]), 4)
        self.assertEqual(report(page(f"<p>{lines[0]}</p><p>We check every number against its source.</p>", "en"))["lone_short"], [])

    def test_a_short_sentence_left_beside_a_lock_is_not_alone(self):
        html = page("".join(f"<p>Nothing on this website is an offer. {s}</p>" for s in ("See the imprint.", "Ask us first.",
                                                                                         "Read the terms.")), "en")
        found, info = ca.blocks_from_html(html, "t.html")
        self.assertEqual(ca.analyse("t.html", found, info, locked=("nothing on this website",))["lone_short"], [])


class Revisions(unittest.TestCase):
    """Fixes after the scanner was run over real before-and-after pages."""

    LEGAL = [
        "We keep your message for as long as we need it to answer you, and we delete it within twelve months after that.",
        "You may ask us at any time which personal data we hold about you, and we will answer within one month.",
        "We do not sell your data to anyone, and we share it only with the processors named in this policy below.",
        "Our hosting provider stores the website files in the European Union, and it has signed a processing agreement.",
        "If you think we handle your data wrongly, you can complain to us first, and you can also go to the regulator.",
        "The website sets no tracking cookies, and the one functional cookie expires when you close the browser window.",
        "These terms apply to every introduction we make, and they take precedence over any terms you send us yourself.",
        "We may change this policy when the law or our services change, and the date at the top shows the latest version.",
    ]

    def test_joints_stay_quiet_on_long_form_pages(self):
        self.assertEqual(report(page("".join(f"<p>{s}</p>" for s in self.LEGAL), "en"))["joints"], [])

    def test_a_long_joint_list_is_cut_short_in_the_report(self):
        many = NewChecks.RESERVE[:5] * 2 + NewChecks.RESERVE[5:]
        text = ca.render_scan(report(page("".join(f"<p>{s}</p>" for s in many))))
        self.assertIn("... and 5 more", text)

    def test_clipped_note_when_a_quarter_of_the_sentences_are_short(self):
        lines = ["We look for energy projects that need funding.", "Then we check them.",
                 "Investors go through the same checks first.", "Both sides are checked.",
                 "A number without a source stops the process.", "We say why.",
                 "The first call takes about half an hour.", "Our fee is agreed in writing first.",
                 "We answer every message ourselves, in English.", "Most projects are in the north.",
                 "Grid connection and land come first.", "Then the costs and the revenue figures."]
        got = report(page("".join(f"<p>{s}</p>" for s in lines), "en"))["rhythm"]
        self.assertEqual((got["short_share"], got["note"]), (0.25, "clipped"))

    def test_initialisms_are_not_short_lines(self):
        cognac = "".join(f"<p>{s}</p>" for s in ("Hennessy V.S.", "Hennessy V.S.O.P.", "Remy Martin V.S.O.P."))
        self.assertEqual(report(page(cognac, "en"))["lone_short"], [])

    def test_sentences_either_side_of_a_locked_one_are_not_neighbours(self):
        html = page("<p>We check both sides. Nothing on this website is an offer or a solicitation to anyone at all. "
                    "Ask us first.</p>", "en")
        found, info = ca.blocks_from_html(html, "t.html")
        got = ca.analyse("t.html", found, info, locked=("nothing on this website",))
        self.assertEqual((got["findings"], got["locked_blocks"], got["locked_words"]), ([], 1, 14))

    def test_a_dish_per_list_item_with_its_price_below_is_a_menu(self):
        dishes = "".join(f"<li>{n} DISH NUMBER {n} with a short gloss in English</li><li>6,00 euro</li>" for n in range(12))
        html = page(f"<ul>{dishes}</ul><p>We are open every day from five, and on Sundays from noon.</p>", "en")
        self.assertEqual(report(html)["rhythm"]["sentences"], 1)

    def test_a_script_is_not_scanned_as_a_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            js = Path(tmp) / "copy.js"
            js.write_text("var copy = { nl: { lead: 'Reserveer online.' } };", encoding="utf-8")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(ca.main(["scan", str(js)]), 2)
                self.assertEqual(ca.main(["locks", "--locked", str(js), "https://example.org/"]), 2)


class Locks(unittest.TestCase):
    def test_locked_lines_are_not_counted_in_rhythm_or_repeats(self):
        consent = "By sending this you agree that we may store your message and reply to it."
        prose = ["We look for energy projects that need funding before anyone else hears about them.",
                 "Most of the work goes into the grid connection and the land under the site.",
                 "If a number cannot be traced to a published source, we stop and say why.",
                 "Investors go through the same checks, so both sides know who they are dealing with.",
                 "Then we introduce the two sides and step back.",
                 "A first call usually takes about half an hour.",
                 "We answer every message ourselves, in English or in Dutch.",
                 "Our fee is agreed in writing before any work starts."]
        html = page("<p>Our role.</p><p>See the imprint.</p><p>See the privacy policy.</p>"
                    + f"<form><p>{consent}</p></form>" * 2 + "".join(f"<p>{s}</p>" for s in prose), "en")
        found, info = ca.blocks_from_html(html, "t.html")
        got = ca.analyse("t.html", found, info,
                         locked=("our role", "see the imprint", "see the privacy policy", "by sending this you agree"))
        self.assertEqual((got["rhythm"]["sentences"], got["repeats"], got["locked_blocks"]), (8, [], 5))
        self.assertGreater(got["locked_words"], 0)

    def test_a_lock_exempts_its_sentence_not_its_neighbours(self):
        html = page("<p>Nothing on this website is an offer or investment advice. "
                    "No client is named on this page, and both sides are checked.</p>", "en")
        found, info = ca.blocks_from_html(html, "t.html")
        got = ca.analyse("t.html", found, info, locked=("nothing on this website",))
        self.assertEqual([(f["check"], f["text"]) for f in got["findings"]],
                         [("meta-talk", "No client is named on this page, and both sides are checked.")])
        self.assertEqual(got["locked_blocks"], 1)

    def test_locked_fence_in_a_rules_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.md"
            rules.write_text("Prices stay.\n\n```locked\nSfeerbeelden: eigen fotografie.\nDe keuken sluit om 22:00.\n```\n",
                             encoding="utf-8")
            self.assertEqual(ca.read_locks([str(rules)]), ("Sfeerbeelden: eigen fotografie.", "De keuken sluit om 22:00."))
            rules.write_text("No fence here.\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                ca.read_locks([str(rules)])


class Api(unittest.TestCase):
    """Project checks import this module; these names and keys must not change."""

    def test_public_names_and_report_keys(self):
        import inspect
        for name in ("load", "analyse", "render_scan", "render_extract", "blocks_from_html", "sentences", "words"):
            self.assertTrue(callable(getattr(ca, name)), name)
        self.assertEqual(list(inspect.signature(ca.analyse).parameters)[:4], ["source", "blocks", "info", "locked"])
        self.assertRegex(ca.__version__, r"^\d{4}-\d{2}-\d{2}$")
        got = report(page("<p>We zijn elke dag open vanaf vijf uur, ook op zondag en op feestdagen.</p>"))
        self.assertLessEqual({"source", "lang", "scanner", "body_words", "rhythm", "findings", "repeats", "headings",
                              "locked_blocks"}, set(got))
        self.assertLessEqual({"sentences", "mean_words", "sd_words", "short_share"}, set(got["rhythm"]))


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

    def test_locks_counts_every_copy_and_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            site, lock = Path(tmp) / "site", Path(tmp) / "locked.txt"
            site.mkdir()
            line1, line2 = "Het kost EUR 1,60 per 15 minuten.", "De video's zijn met AI gemaakt van onze eigen foto's."
            (site / "index.html").write_text(page("<p>Het kost EUR&nbsp;1,60 per 15 minuten.</p><p>De video&#39;s zijn met AI "
                                                  "gemaakt van onze eigen foto&#39;s.</p><script>var nl = {park: 'Het kost EUR 1,60 "
                                                  "per 15 minuten.'};</script>"), encoding="utf-8")
            (site / "menu.html").write_text(page(f"<p>{line2}</p>"), encoding="utf-8")
            (site / "copy.js").write_text("var en = {call: 'Or call us on 020 \u2013\u2060 123 45 67.'};\n"
                                          "var nl = {ai: 'De video\\'s zijn met AI gemaakt van onze eigen foto\\'s.',"
                                          " park: 'Het kost EUR\\u00a01,60 per 15 minuten.'};", encoding="utf-8")
            line3 = "Or call us on 020 \u2013 123 45 67."  # the script keeps the dash on the number's line
            lock.write_text(f"{line1}\n{line2}\n{line3}\n", encoding="utf-8")
            code, text = self.run_cli("locks", "--json", "--locked", str(lock), str(site))
            self.assertEqual((code, json.loads(text)), (0, {line1: 3, line2: 3, line3: 1}))
            lock.write_text(f"{line1}\nDeze regel staat nergens meer op de site.\n", encoding="utf-8")
            self.assertEqual(self.run_cli("locks", "--locked", str(lock), str(site))[0], 1)
            lock.write_text("# only a comment\n", encoding="utf-8")
            self.assertEqual(self.run_cli("locks", "--locked", str(lock), str(site))[0], 2)

    def test_locks_reads_a_line_through_an_inline_link_and_keeps_attribute_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            site, lock = Path(tmp) / "site", Path(tmp) / "locked.txt"
            site.mkdir()
            consent, dish = "By sending this you agree that we may store it. See the privacy policy.", "Slices of beef with garlic."
            (site / "index.html").write_text(page(
                '<p>By sending this you agree that we may store it. See the <a href="/privacy/">privacy policy</a>.</p>'
                f'<span class="dish" data-desc-en="{dish}">Plakjes rund met knoflook.</span>', "en"), encoding="utf-8")
            (site / "form.js").write_text("var en = {consent: 'By sending this you agree that we may store it. "
                                          "See the <a href=\"/privacy/\">privacy policy</a>.'};", encoding="utf-8")
            lock.write_text(f"{consent}\n{dish}\n", encoding="utf-8")
            code, text = self.run_cli("locks", "--json", "--locked", str(lock), str(site))
            self.assertEqual((code, json.loads(text)), (0, {consent: 2, dish: 1}))

    def test_a_short_lock_line_is_warned_about(self):
        with tempfile.TemporaryDirectory() as tmp:
            html, lock = Path(tmp) / "p.html", Path(tmp) / "locked.txt"
            html.write_text(page("<p>All you can eat kost EUR 30,00 per persoon.</p>"), encoding="utf-8")
            lock.write_text("per persoon\n", encoding="utf-8")
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                ca.main(["scan", "--locked", str(lock), str(html)])
            self.assertIn("under four words (first: 'per persoon')", err.getvalue())

    def test_missing_input_exits_2(self):
        self.assertEqual(self.run_cli("scan", "/no/such/page.html")[0], 2)

    def test_version_flag(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            ca.main(["--version"])
        self.assertEqual(out.getvalue().strip(), f"copy_audit {ca.__version__}")

    def test_locked_file_on_the_command_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            html, locked = Path(tmp) / "p.html", Path(tmp) / "locked.txt"
            html.write_text(page("<p>Ontdek de kaart in het hart van de stad.</p>"), encoding="utf-8")
            locked.write_text("# owner-approved wording\nin het hart van de stad\n", encoding="utf-8")
            code, text = self.run_cli("scan", "--json", "--locked", str(locked), str(html))
            self.assertEqual((code, json.loads(text)[0]["findings"], json.loads(text)[0]["locked_blocks"]), (0, [], 1))


if __name__ == "__main__":
    unittest.main(verbosity=1)
