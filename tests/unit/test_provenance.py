"""T011 — конверт происхождения.

`contracts/envelope-and-errors.md`: отсутствие обязательного поля — провал,
`curated` без `verified_at` — провал, а `stale = true` обязано попадать в текст,
который юрист видит, а не только в служебные поля (принцип IV).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.provenance import (  # noqa: E402
    ENVELOPE_FIELDS,
    ProvenanceStamp,
    SourceChannel,
    stamp_from_cache_entry,
)


def _stamp(**overrides: object) -> ProvenanceStamp:
    payload: dict[str, object] = {
        "source_channel": SourceChannel.LIVE,
        "source_url": "https://zakon.rada.gov.ua/laws/show/922-19",
        "language": "uk",
        "citation_format": "ст. N Закону України № 922-VIII",
        "stale": False,
        "is_authentic_version": True,
        "is_translation": False,
    }
    payload.update(overrides)
    return ProvenanceStamp(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Обязательные поля
# ---------------------------------------------------------------------------


def test_a_complete_stamp_validates() -> None:
    stamp = _stamp()

    assert stamp.source_channel is SourceChannel.LIVE
    assert stamp.notice.strip(), "конверт без готовой фразы юристу нечего показать"


@pytest.mark.parametrize(
    "missing",
    [
        "source_channel",
        "source_url",
        "language",
        "citation_format",
        "stale",
        "is_authentic_version",
        "is_translation",
    ],
)
def test_missing_required_field_fails_validation(missing: str) -> None:
    payload = {
        "source_channel": SourceChannel.LIVE,
        "source_url": "https://example.org",
        "language": "uk",
        "citation_format": "ст. N",
        "stale": False,
        "is_authentic_version": True,
        "is_translation": False,
    }
    payload.pop(missing)

    with pytest.raises(ValidationError):
        ProvenanceStamp(**payload)  # type: ignore[arg-type]


def test_envelope_fields_lists_every_field_of_the_stamp() -> None:
    """Контракты валидируют ответ по этому перечню — расхождение ловится здесь."""
    assert ENVELOPE_FIELDS == frozenset(ProvenanceStamp.model_fields)


def test_empty_source_url_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _stamp(source_url="   ")


# ---------------------------------------------------------------------------
# curated обязывает к verified_at
# ---------------------------------------------------------------------------


def test_curated_without_verified_at_fails() -> None:
    """В этом канале свежесть гарантируется нами, а не источником."""
    with pytest.raises(ValidationError):
        _stamp(source_channel=SourceChannel.CURATED)


def test_curated_with_verified_at_validates_and_says_so() -> None:
    stamp = _stamp(source_channel=SourceChannel.CURATED, verified_at="2026-09-04")

    assert stamp.verified_at == "2026-09-04"
    assert "2026-09-04" in stamp.notice


def test_live_and_index_do_not_require_verified_at() -> None:
    assert _stamp(source_channel=SourceChannel.LIVE).verified_at is None
    assert _stamp(source_channel=SourceChannel.INDEX, cached_at="2026-01-01T00:00:00+00:00")


# ---------------------------------------------------------------------------
# stale обязан быть виден юристу
# ---------------------------------------------------------------------------


def test_stale_answer_carries_the_warning_in_the_lawyer_facing_text() -> None:
    stamp = _stamp(
        source_channel=SourceChannel.INDEX,
        stale=True,
        cached_at="2026-01-01T00:00:00+00:00",
        age_days=247.0,
    )

    assert stamp.stale is True
    assert "застаріл" in stamp.notice.lower() or "недоступн" in stamp.notice.lower()


def test_a_hand_written_notice_that_hides_staleness_is_rejected() -> None:
    """Служебное поле само по себе не является пометкой: юрист читает текст."""
    with pytest.raises(ValidationError):
        _stamp(
            source_channel=SourceChannel.INDEX,
            stale=True,
            cached_at="2026-01-01T00:00:00+00:00",
            notice="Відповідь із локального кешу.",
        )


def test_a_fresh_answer_does_not_claim_to_be_stale() -> None:
    stamp = _stamp()

    assert "застаріл" not in stamp.notice.lower()


# ---------------------------------------------------------------------------
# Перевод и аутентичность
# ---------------------------------------------------------------------------


def test_a_translation_cannot_also_be_the_authentic_version() -> None:
    with pytest.raises(ValidationError):
        _stamp(is_translation=True, is_authentic_version=True)


def test_a_translation_says_so_in_the_notice_and_is_not_quotable_as_norm() -> None:
    stamp = _stamp(is_translation=True, is_authentic_version=False, language="uk")

    assert stamp.may_be_quoted_as_norm is False
    assert "переклад" in stamp.notice.lower()


def test_an_outdated_translation_says_that_too() -> None:
    stamp = _stamp(
        is_translation=True,
        is_authentic_version=False,
        translation_outdated=True,
    )

    assert "застаріл" in stamp.notice.lower()


def test_an_authentic_version_is_quotable_as_norm() -> None:
    """Только полный текст/фрагмент известного вида; неизвестный вид содержимого — нет (T129)."""
    assert _stamp().may_be_quoted_as_norm is False
    assert (
        _stamp(content_kind="fragment", publication_kind="official_journal").may_be_quoted_as_norm
        is True
    )
    assert _stamp(content_kind="fragment", publication_kind="card").may_be_quoted_as_norm is False


def test_a_stale_authentic_version_is_not_quotable_as_current() -> None:
    stamp = _stamp(
        source_channel=SourceChannel.INDEX,
        stale=True,
        cached_at="2026-01-01T00:00:00+00:00",
    )

    assert stamp.may_be_quoted_as_norm is False


# ---------------------------------------------------------------------------
# Атрибуция
# ---------------------------------------------------------------------------


def test_required_attribution_is_reproduced_in_the_notice() -> None:
    stamp = _stamp(attribution="© European Union, 1998-2026")

    assert "© European Union, 1998-2026" in stamp.notice


def test_a_hand_written_notice_that_drops_the_attribution_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _stamp(
            attribution="© European Union, 1998-2026",
            notice="Відповідь отримано наживо з офіційного джерела.",
        )


# ---------------------------------------------------------------------------
# Мост к существующему горячему кешу
# ---------------------------------------------------------------------------


def test_stamp_from_cache_entry_marks_a_live_answer() -> None:
    stamp = stamp_from_cache_entry(
        {"from_cache": False, "retrieved_at": "2026-09-03T19:00:00+00:00"},
        source_url="https://zakon.rada.gov.ua/laws/show/922-19",
    )

    assert stamp.source_channel is SourceChannel.LIVE
    assert stamp.stale is False
    assert "наживо" in stamp.notice


def test_stamp_from_cache_entry_marks_a_cached_answer_with_its_age() -> None:
    stamp = stamp_from_cache_entry(
        {"from_cache": True, "scraped_at": "2020-01-01T00:00:00+00:00"},
        source_url="https://zakon.rada.gov.ua/laws/show/922-19",
    )

    assert stamp.source_channel is SourceChannel.INDEX
    assert stamp.cached_at == "2020-01-01T00:00:00+00:00"
    assert stamp.age_days is not None and stamp.age_days > 1000


def test_stamp_from_cache_entry_of_nothing_is_not_a_crash() -> None:
    stamp = stamp_from_cache_entry(None, source_url="https://example.org")

    assert stamp.source_channel is SourceChannel.LIVE
    assert stamp.cached_at is None
    assert stamp.age_days is None
