"""Measured defects of the in-memory scorer, each pinned by its own test.

No network, no cache/: the texts below are the smallest shapes that reproduce
what was observed on the real corpus.
"""

from __future__ import annotations

from search.search_engine import (
    SCORE_KIND_ALIAS,
    SCORE_KIND_ARTICLE,
    SCORE_KIND_LINE,
    SCORE_KIND_RANK,
    SearchEngine,
)

#: The exact shape Rada's print layout produces for «Стаття 179-1»: the number
#: is broken across four lines. Reproduced verbatim from cache/texts/435-15.json.
WRAPPED_ARTICLE_TEXT = (
    "Стаття 179.\n"
    "Поняття речі\n"
    "\n"
    "1. Річчю є предмет матеріального світу, щодо якого можуть виникати\n"
    "цивільні права та обов'язки.\n"
    "\n"
    "Стаття 179\n"
    "-\n"
    "1\n"
    ".\n"
    "Поняття цифрової речі\n"
    "\n"
    "1. Цифровою річчю є благо, що створюється та існує виключно у\n"
    "цифровому середовищі.\n"
    "\n"
    "Стаття 180.\n"
    "Тварини\n"
    "\n"
    "1. Тварини є особливим об'єктом цивільних прав.\n"
)


def test_wrapped_article_number_does_not_overwrite_the_plain_one() -> None:
    """Defect: ст. 179-1 was stored under the key "179" and erased ст. 179.

    The header pattern required the dash to touch the digits, so it read
    "Стаття 179\\n-\\n1\\n." as plain "179"; _collect_sections then assigned over
    the earlier entry. get_article("179") answered with «Поняття цифрової речі»
    and a search hit labelled "[Стаття 179]" carried another article's text.
    """
    articles = SearchEngine().parse_articles(WRAPPED_ARTICLE_TEXT)

    assert set(articles) == {"179", "179-1", "180"}
    assert "Поняття речі" in articles["179"]
    assert "цифрової" not in articles["179"]
    assert "Поняття цифрової речі" in articles["179-1"]


def test_direct_reference_still_reaches_the_full_number() -> None:
    """The wider key must not break lookup by the plain number."""
    engine = SearchEngine()
    articles = engine.parse_articles(WRAPPED_ARTICLE_TEXT)

    assert engine.fuzzy_match_article("179", articles)[0] == "179"  # type: ignore[index]
    assert engine.fuzzy_match_article("179-1", articles)[0] == "179-1"  # type: ignore[index]


#: Parsed law whose relevant article carries BOTH query words, but drowned in
#: ~91 words, so each word is worth about 1000/91 ≈ 11 and the article ≈ 22.
PARSED_LAW = (
    "Стаття 1.\n"
    "Загальні положення\n"
    "\n"
    "1. " + ("слово " * 80) + "відкликані ліцензії згадані тут один раз.\n"
    "\n"
    "Стаття 2.\n"
    "Прикінцеві положення\n"
    "\n"
    "1. Цей Закон набирає чинності з дня його опублікування.\n"
)

#: Unparsed law: no «статті» headers and too few numbered points to fall back
#: on, so match_query takes the line path — and both words sit in one short line.
UNPARSED_LAW = (
    "Порядок роботи\n"
    "У разі потреби відкликані ліцензії обліковуються окремо.\n"
    "Рішення ухвалює уповноважений орган.\n"
)


def test_line_fallback_scores_on_the_same_scale_as_articles() -> None:
    """Defect: the two score kinds were different units, silently sorted together.

    The fallback returned a raw count of matched tokens (here 2) while parsed
    articles returned a per-word density (here ≈ 22). The caller sorted both by
    "score", so the law that says both words in one line lost to the law that
    buries them in ninety — the ordering was decided by the scale, not by the
    match. Both sides now answer in hits per 1000 words.
    """
    engine = SearchEngine()
    query = "відкликані ліцензії"

    parsed = engine.match_query(PARSED_LAW, query, articles=engine.parse_articles(PARSED_LAW))
    unparsed = engine.match_query(UNPARSED_LAW, query, articles={})

    assert parsed is not None and unparsed is not None
    assert parsed["score_kind"] == SCORE_KIND_ARTICLE
    assert unparsed["score_kind"] == SCORE_KIND_LINE
    # Pre-fix numbers: unparsed 2.0 (a token count) vs parsed ≈ 22 — the wrong
    # way round, and by two orders of magnitude on the tightest possible hit.
    assert unparsed["score"] > parsed["score"]
    assert unparsed["score"] > 100.0


def test_score_kind_is_a_tie_break_and_not_the_primary_sort_key() -> None:
    """Ranking by kind first would put the same defect back one layer up.

    The three measured kinds share one unit, so `score` decides and the rank
    only separates equals. `alias_only` is not a measurement at all and sorts
    after every scored result.
    """
    engine = SearchEngine()
    query = "відкликані ліцензії"
    parsed = engine.match_query(PARSED_LAW, query, articles=engine.parse_articles(PARSED_LAW))
    unparsed = engine.match_query(UNPARSED_LAW, query, articles={})
    assert parsed is not None and unparsed is not None

    alias = {"score": 1.0, "score_kind": SCORE_KIND_ALIAS, "snippet": ""}
    ordered = sorted(
        [parsed, unparsed, alias],
        key=lambda hit: (
            hit["score_kind"] != SCORE_KIND_ALIAS,
            hit["score"],
            SCORE_KIND_RANK[str(hit["score_kind"])],
        ),
        reverse=True,
    )

    assert [hit["score_kind"] for hit in ordered] == [
        SCORE_KIND_LINE,
        SCORE_KIND_ARTICLE,
        SCORE_KIND_ALIAS,
    ]
    assert SCORE_KIND_RANK[SCORE_KIND_ARTICLE] > SCORE_KIND_RANK[SCORE_KIND_LINE]


def test_a_query_that_matches_nothing_gets_nothing_not_the_head_of_the_act() -> None:
    """Defect: search_in_text answered a failed match with `text[:context_chars]`.

    «Нічого не знайдено» and «ось початок акта» are different statements to a
    lawyer, and the second must never be served in place of the first: the
    preamble of the law came back looking exactly like a found quotation, with
    nothing in the payload to say the query had matched no word of it.
    """
    engine = SearchEngine()
    articles = engine.parse_articles(PARSED_LAW)

    assert engine.search_in_text(PARSED_LAW, "криптовалютна біржа", articles=articles) == ""
    assert engine.search_in_text(UNPARSED_LAW, "криптовалютна біржа", articles={}) == ""


def test_an_empty_query_still_asks_for_the_document_itself() -> None:
    """The unchanged half of the same rule: no term means "give me the act".

    Asking without a search term is not a search that failed, so the head of the
    text stays the honest answer to it.
    """
    engine = SearchEngine()

    assert engine.search_in_text(PARSED_LAW, "", context_chars=40) == PARSED_LAW[:40]
    assert engine.search_in_text(PARSED_LAW, "   \n ", context_chars=40) == PARSED_LAW[:40]
    assert engine.search_in_text("", "відкликані ліцензії") == ""


def test_a_query_of_service_words_alone_is_not_a_searchable_query() -> None:
    """Defect: `_tokenize` returned `filtered or raw`, so «та або що» had tokens.

    The scorer then ranked passages by how often they say «та» and produced a
    confident-looking snippet out of it. The lexical index refuses that very
    query instead (local_index.py, IndexUnavailable reason `unusable_query`),
    and the two search paths must not disagree about whether a question is
    searchable at all.
    """
    engine = SearchEngine()
    articles = engine.parse_articles(PARSED_LAW)

    assert engine.match_query(PARSED_LAW, "та або що", articles=articles) is None
    assert engine.match_query(UNPARSED_LAW, "та або що", articles={}) is None
    assert engine.search_in_text(UNPARSED_LAW, "та або що", articles={}) == ""


#: One of the two query words, in a parsed law: «ліцензії» is there, «відкликані»
#: is nowhere in the act.
HALF_MATCH_PARSED_LAW = (
    "Стаття 1.\n"
    "Загальні положення\n"
    "\n"
    "1. Ліцензії боржника обліковуються окремо.\n"
    "\n"
    "Стаття 2.\n"
    "Прикінцеві положення\n"
    "\n"
    "1. Цей Закон набирає чинності з дня його опублікування.\n"
)

#: The same half-match with no structure at all, so the line path decides.
HALF_MATCH_TEXT = (
    "Порядок роботи\n"
    "Ліцензії боржника обліковуються окремо.\n"
    "Рішення ухвалює уповноважений орган.\n"
)


def test_a_two_word_query_does_not_take_a_passage_that_has_only_one_word() -> None:
    """Defect: `_density` summed hits per token without requiring them all.

    «відкликані ліцензії» produced a confident hit on a text that only says
    «ліцензії» — the OR fan-out local_index.py forbids itself ("Terms are joined
    with AND, never OR"). Both scoring paths have to hold the line: the article
    path here finds no article, and the line fallback under it finds no line, so
    the whole match is None rather than a half-relevant snippet.
    """
    engine = SearchEngine()
    query = "відкликані ліцензії"

    articles = engine.parse_articles(HALF_MATCH_PARSED_LAW)
    assert len(articles) == 2  # the article path really is the one being tested
    assert engine.match_query(HALF_MATCH_PARSED_LAW, query, articles=articles) is None
    assert engine.match_query(HALF_MATCH_TEXT, query, articles={}) is None
    assert engine.search_in_text(HALF_MATCH_TEXT, query, articles={}) == ""

    # And the single word alone still finds it: AND narrows the answer, it does
    # not switch the search off.
    assert engine.match_query(HALF_MATCH_TEXT, "ліцензії", articles={}) is not None


#: The words of the query in other grammatical forms — the ordinary case in a
#: Ukrainian act: «відкликані ліцензії» asked, «Відкликання ліцензій» written.
INFLECTED_TEXT = (
    "Порядок роботи\n"
    "Відкликання ліцензій здійснюється за рішенням суду.\n"
    "Рішення ухвалює уповноважений орган.\n"
)


def test_and_matching_still_works_across_word_endings() -> None:
    """AND is only usable because a token matches by stem prefix.

    There is no Ukrainian stemmer on this path, so requiring every token
    verbatim would make two-word questions answer nothing at all. Tokens are cut
    back to a stem-shaped prefix by the same rule local_index.rewrite_query uses
    for FTS5, so «відкликані» reaches «Відкликання» and «ліцензії» reaches
    «ліцензій» — and the prefix is anchored at the start of a word, not searched
    as a substring anywhere inside one.
    """
    engine = SearchEngine()

    matched = engine.match_query(INFLECTED_TEXT, "відкликані ліцензії", articles={})
    assert matched is not None
    assert "Відкликання ліцензій" in str(matched["snippet"])

    # The other half of the same rule, and the one that separates a prefix from
    # the substring counting this used to do: «відклик» sits inside
    # «невідкликані» and «ліцен» inside «субліцензійні», so `lowered.count(...)`
    # would have called this a hit on both words.
    inside_other_words = "Невідкликані субліцензійні договори обліковуються окремо."
    assert engine.match_query(inside_other_words, "відкликані ліцензії", articles={}) is None
