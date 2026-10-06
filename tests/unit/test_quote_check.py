"""Звірка цитати з офіційним текстом: чиста текстова частина (тікет 17).

Тут перевіряється лише те, що не потребує мережі: як зіставляється рядок
цитати з рядком тексту одиниці. Читання джерела — окремий шар і окремі тести
(`tests/unit/test_verify_quote.py`).

Що саме перевіряється:

* дослівний збіг названий дослівним;
* збіг після нормалізації названий окремо і перелічує **застосовані** правила,
  а не весь перелік;
* розбіжність дає найближчий фрагмент такої ж довжини з позначеними
  відмінностями, а не мовчазне «не знайдено»;
* діакритика (латиська) і кирилиця не ламаються нормалізацією.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import quote_check  # noqa: E402

ARTICLE = (
    "1. All products and packaging materials made, imported, held or "
    "distributed by any operator referred to in Annex I shall be "
    "recorded."
)


class TestExact:
    """Дослівний збіг."""

    def test_literal_substring_is_exact(self) -> None:
        result = quote_check.match_quote(ARTICLE, "packaging materials made, imported")

        assert result.status == "exact"
        assert result.applied_rules == ()

    def test_position_points_into_the_original_text(self) -> None:
        quote = "packaging materials made, imported"

        result = quote_check.match_quote(ARTICLE, quote)

        assert ARTICLE[result.start : result.end] == quote

    def test_fragment_is_the_original_spelling_not_the_normalised_one(self) -> None:
        result = quote_check.match_quote(ARTICLE, "referred to in Annex I shall be recorded")

        assert result.fragment == "referred to in Annex I shall be recorded"

    def test_context_is_small_and_marked_on_both_sides(self) -> None:
        result = quote_check.match_quote(ARTICLE, "referred to in Annex I", context=20)

        assert len(result.context_before) <= 20
        assert len(result.context_after) <= 20
        assert ARTICLE.endswith(result.context_after)


class TestNormalized:
    """Збіг після нормалізації: правило називається, текст не підмінюється."""

    def test_line_break_inside_the_text_still_matches_a_one_line_quote(self) -> None:
        text = "All products and packaging\n    materials shall be recorded."

        result = quote_check.match_quote(
            text, "All products and packaging materials shall be recorded."
        )

        assert result.status == "normalized"
        assert "whitespace" in result.applied_rules

    def test_typographic_quotes_and_apostrophes(self) -> None:
        text = "Уживається поняття «зареєстровані товари» та слово ’як’."

        result = quote_check.match_quote(text, "\"зареєстровані товари\" та слово 'як'.")

        assert result.status == "normalized"
        assert "quotes" in result.applied_rules

    def test_en_dash_in_the_source_and_hyphen_in_the_quote(self) -> None:
        text = "Регламент № 8888/2014 — стаття 2(1) – реєстрація."

        result = quote_check.match_quote(text, "№ 8888/2014 - стаття 2(1) - реєстрація.")

        assert result.status == "normalized"
        assert "dashes" in result.applied_rules

    def test_soft_hyphen_in_the_source_is_invisible_to_the_quote(self) -> None:
        text = "man­da­tory registration of packaging materials"

        result = quote_check.match_quote(text, "mandatory registration of packaging materials")

        assert result.status == "normalized"
        assert "soft_hyphen" in result.applied_rules

    def test_non_breaking_space_matches_an_ordinary_one(self) -> None:
        text = "registrace podle nařízení č. 8888/2014"

        result = quote_check.match_quote(text, "registrace podle nařízení")

        assert result.status == "normalized"
        assert "nbsp" in result.applied_rules

    def test_only_the_rules_that_were_used_are_named(self) -> None:
        text = "man­datory registration of packaging materials"

        result = quote_check.match_quote(text, "mandatory registration of packaging materials")

        assert "quotes" not in result.applied_rules
        assert "dashes" not in result.applied_rules

    def test_diacritics_are_not_stripped_by_normalisation(self) -> None:
        """Нормалізація не чіпає літер: ``ř`` не стає ``r``."""
        text = "Účastníci řízení mají právo nahlížet do spisu."

        result = quote_check.match_quote(text, "Ucastnici rizeni maji pravo nahlizet do spisu")

        assert result.status == "mismatch"

    def test_cyrillic_quote_matches_after_normalisation(self) -> None:
        text = "Реєстрація\nтоварів є обов’язковим заходом."

        result = quote_check.match_quote(
            text,
            "Реєстрація " "товарів є обов'" "язковим заходом.",
        )

        assert result.status == "normalized"


class TestMismatch:
    """Розбіжність: найближчий фрагмент і позначені відмінності."""

    def test_altered_word_gives_mismatch(self) -> None:
        result = quote_check.match_quote(
            ARTICLE, "All products and packaging materials may be recorded"
        )

        assert result.status == "mismatch"

    def test_nearest_fragment_comes_from_the_source_text(self) -> None:
        result = quote_check.match_quote(
            ARTICLE, "All products and packaging materials may be recorded"
        )

        assert result.fragment
        assert result.fragment in ARTICLE

    def test_nearest_fragment_has_the_length_of_the_quote(self) -> None:
        quote = "All products and packaging materials may be recorded"

        result = quote_check.match_quote(ARTICLE, quote)

        assert abs(len(result.fragment) - len(quote)) <= 2

    def test_differences_name_both_sides(self) -> None:
        text = "The products of a registered operator shall be recorded without delay."

        result = quote_check.match_quote(
            text, "The products of a registered operator may be recorded"
        )

        assert "[-shall-]" in result.differences
        assert "{+may+}" in result.differences

    def test_quote_absent_from_the_text_is_still_a_mismatch_not_a_crash(self) -> None:
        result = quote_check.match_quote(ARTICLE, "жодного спільного слова тут немає")

        assert result.status == "mismatch"

    def test_empty_text_is_a_mismatch_with_an_empty_fragment(self) -> None:
        result = quote_check.match_quote("", "All products and packaging materials")

        assert result.status == "mismatch"
        assert result.fragment == ""

    def test_the_minimum_length_has_one_wording_everywhere(self) -> None:
        """Правило живе в одному місці: текст помилки той самий з обох входів."""
        from core import quote_check as module

        with pytest.raises(ValueError) as raised:
            module.match_quote(ARTICLE, "recorded")

        assert str(raised.value) == module.too_short_reason("recorded")


class TestValidation:
    """Занадто коротка цитата не перевіряється."""

    def test_quote_shorter_than_the_minimum_is_refused(self) -> None:
        with pytest.raises(ValueError):
            quote_check.match_quote(ARTICLE, "recorded")

    def test_the_minimum_is_fifteen_characters(self) -> None:
        assert quote_check.MIN_QUOTE_CHARS == 15

    def test_a_quote_of_exactly_the_minimum_is_accepted(self) -> None:
        quote = ARTICLE[:15]

        assert quote_check.match_quote(ARTICLE, quote).status == "exact"


class TestRulesAreEnumerated:
    """Перелік правил нормалізації закритий і названий."""

    def test_every_rule_has_an_identifier_and_a_ukrainian_description(self) -> None:
        for rule in quote_check.NORMALIZATION_RULES:
            assert rule.rule_id
            assert rule.description

    def test_the_declared_rules_are_exactly_the_seven_of_the_contract(self) -> None:
        assert {rule.rule_id for rule in quote_check.NORMALIZATION_RULES} == {
            "whitespace",
            "quotes",
            "apostrophes",
            "dashes",
            "soft_hyphen",
            "invisible",
            "nbsp",
        }

    def test_soft_hyphen_covers_only_the_soft_hyphen(self) -> None:
        """Нуль-ширинні символи мають власне правило: назва правила не бреше."""
        result = quote_check.match_quote(
            "registration​of packaging materials here", "registrationof packaging materials here"
        )

        assert result.applied_rules == ("invisible",)


class TestAppliedRulesAreTheOnesThatMattered:
    """Названі лише правила, без яких збігу не було б."""

    def test_a_rule_that_changed_nothing_in_the_match_is_not_named(self) -> None:
        """Тире стоїть у тексті **поза** фрагментом — до переліку не потрапляє."""
        text = "Стаття 1 — визначення.\nЗареєстровані товари означають товари."

        result = quote_check.match_quote(text, "Зареєстровані товари означають товари.")

        assert result.status == "normalized"
        assert "dashes" not in result.applied_rules
        assert "nbsp" in result.applied_rules

    def test_normalized_never_comes_back_with_an_empty_list(self) -> None:
        """Зайвий пробіл на початку — теж правило, і воно назване."""
        result = quote_check.match_quote(ARTICLE, "  All products and packaging materials")

        assert result.status == "normalized"
        assert result.applied_rules == ("whitespace",)


class TestNoCommonFragment:
    """Спільного місця немає — фрагмента теж, і причина названа."""

    def test_no_fragment_is_invented_from_the_document_header(self) -> None:
        result = quote_check.match_quote(ARTICLE, "жодного спільного слова тут немає")

        assert result.status == "mismatch"
        assert result.fragment == ""
        assert result.reason

    def test_a_huge_text_skips_the_fuzzy_search_and_says_so(self) -> None:
        """Найближчий фрагмент на мільйоні символів коштує секунди — і не рахується."""
        text = "а" * (quote_check.MAX_FUZZY_CHARS + 1)

        result = quote_check.match_quote(text, "зовсім інший рядок цитати")

        assert result.status == "mismatch"
        assert result.fragment == ""
        assert "не шукали" in result.reason


class TestDifferencesAndFragmentSpeakTheSameSpelling:
    """Фрагмент і розмітка беруться з того самого написання."""

    def test_the_marked_source_side_is_the_fragment_itself(self) -> None:
        text = "The products of a registered operator shall be recorded without delay."

        result = quote_check.match_quote(
            text, "The products of a registered operator may be recorded"
        )

        import re

        without_insertions = re.sub(r"\{\+.*?\+\}", "", result.differences)
        source_side = without_insertions.replace("[-", "").replace("-]", "")
        assert source_side == result.fragment
