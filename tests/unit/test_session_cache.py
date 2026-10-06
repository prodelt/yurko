"""Сессионный кеш текстов (T034, FR-010, принцип VI).

Текст, полученный в ходе сессии, остаётся доступным для сверки до конца этой
сессии — и не дольше. Содержимое не переживает сессию и не связывается
с пользователем. Материалы конкретного дела сюда не попадают, потому что
не загружаются вовсе.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.citations import CachedText, SessionTextCache  # noqa: E402
from core.contracts import DocumentType, Revision, Unit  # noqa: E402

TEXT = (
    "All products and packaging materials made, imported, held or distributed by any "
    "operator ... shall be recorded."
)


def _unit(path: str = "2(1)", text: str = TEXT) -> Unit:
    return Unit(
        document_id="32014R8888",
        path=path,
        text=text,
        revision=Revision(document_id="32014R8888", valid_from="2014-03-10"),
        doc_type=DocumentType.ACT,
    )


@pytest.fixture()
def cache() -> SessionTextCache:
    return SessionTextCache()


# ---------------------------------------------------------------------------
# Доступность до конца сессии
# ---------------------------------------------------------------------------


def test_text_fetched_in_the_session_is_available_for_verification(
    cache: SessionTextCache,
) -> None:
    cache.remember_unit("s1", _unit())

    found = cache.find("s1", "32014R8888", "2(1)")
    assert found is not None
    assert found.text == TEXT


def test_lookup_is_by_normalised_subdivision(cache: SessionTextCache) -> None:
    cache.remember_unit("s1", _unit(path="Article 2(1)"))

    assert cache.find("s1", "32014r8888", "art. 2(1)") is not None


def test_a_whole_document_can_be_remembered_without_a_subdivision(
    cache: SessionTextCache,
) -> None:
    cache.remember("s1", "32016R0679", TEXT, source_url="https://eur-lex.europa.eu/")

    assert cache.has_document("s1", "32016R0679")
    assert cache.find("s1", "32016R0679", "") is not None


def test_repeated_identical_fetch_is_one_entry_but_a_new_hash_is_a_new_evidence(
    cache: SessionTextCache,
) -> None:
    cache.remember_unit("s1", _unit())
    cache.remember_unit("s1", _unit())
    assert len(cache.texts_for("s1", "32014R8888")) == 1

    cache.remember_unit("s1", _unit(text=TEXT + " (consolidated)"))
    # Другой хеш под тем же адресом — другое доказательство, ничего не вытесняется.
    assert len(cache.texts_for("s1", "32014R8888")) == 2


def test_fetched_at_is_recorded(cache: SessionTextCache) -> None:
    entry = cache.remember_unit("s1", _unit())

    assert entry.fetched_at
    assert entry.session_id == "s1"
    assert entry.unit_ref == "32014R8888#2(1)"


# ---------------------------------------------------------------------------
# Не переживает сессию
# ---------------------------------------------------------------------------


def test_sessions_do_not_see_each_others_texts(cache: SessionTextCache) -> None:
    cache.remember_unit("s1", _unit())

    assert cache.find("s2", "32014R8888", "2(1)") is None
    assert cache.has_document("s2", "32014R8888") is False


def test_ending_a_session_removes_its_texts(cache: SessionTextCache) -> None:
    cache.remember_unit("s1", _unit())
    cache.remember_unit("s2", _unit())

    removed = cache.end_session("s1")

    assert removed == 1
    assert cache.find("s1", "32014R8888", "2(1)") is None
    assert cache.find("s2", "32014R8888", "2(1)") is not None
    assert cache.sessions() == ("s2",)


def test_the_cache_holds_nothing_after_clear(cache: SessionTextCache) -> None:
    cache.remember_unit("s1", _unit())
    cache.clear()

    assert len(cache) == 0
    assert cache.sessions() == ()


def test_an_unnamed_session_stores_nothing(cache: SessionTextCache) -> None:
    """Без идентификатора сессии текст некуда положить и нечем ограничить."""
    with pytest.raises(ValidationError):
        cache.remember("", "32014R8888", TEXT)


# ---------------------------------------------------------------------------
# Не связывается с пользователем (принцип VI)
# ---------------------------------------------------------------------------


def test_the_entry_has_no_field_that_identifies_a_person() -> None:
    names = " ".join(CachedText.model_fields).lower()
    for word in ("user", "client", "person", "account", "case", "matter", "ip", "email"):
        assert word not in names.split(), f"запись кеша несёт поле {word!r}"

    assert set(CachedText.model_fields) == {
        "session_id",
        "document_id",
        "path",
        "text",
        "source_url",
        "fetched_at",
        "legal_order",
        "version_id",
        "language",
        "content_hash",
        "evidence_id",
        "publication_kind",
        "content_kind",
        "locator",
        "aliases",
        # Реквизиты документа (ECLI, название дела, суд, дата, редакция) —
        # свойства публичного документа, а не человека: они приходят от
        # источника и печатаются в ссылке. ``CitationMeta`` тоже ``extra="forbid"``,
        # поэтому расширить схему через него так же нельзя.
        "meta",
    }


def test_the_requisites_of_the_document_are_not_the_requisites_of_a_person() -> None:
    """Белый список реквизитов закрыт и не содержит ничего о человеке."""
    from core.citations import CitationMeta

    names = " ".join(CitationMeta.model_fields).lower()
    for word in ("user", "client", "person", "account", "matter", "ip", "email"):
        assert word not in names.split(), f"реквизиты несут поле {word!r}"
    with pytest.raises(ValidationError):
        CitationMeta(client_id="x")  # type: ignore[call-arg]


def test_the_requisites_do_not_rename_the_evidence() -> None:
    """Реквизиты не входят в ``content_hash`` и ``evidence_id``.

    Идентичность доказательства стоит на тексте фрагмента. Если бы добавление
    метаданных меняло ``evidence_id``, каждое уже выданное юристу подтверждение
    указывало бы на доказательство, которого в сеансе больше нет.
    """
    from core.citations import CitationMeta

    bare = CachedText(
        session_id="s1",
        document_id="62016CJ0064",
        path="57",
        text=TEXT,
        fetched_at="2026-09-09T00:00:00+00:00",
        legal_order="EU",
        language="en",
    )
    with_meta = bare.model_copy(
        update={
            "meta": CitationMeta(
                ecli="ECLI:EU:C:2018:117",
                case_name="Associação Sindical dos Juízes Portugueses v Tribunal de Contas",
                judgment_date="2021-11-11",
            )
        }
    )

    assert with_meta.content_hash == bare.content_hash
    assert with_meta.evidence_id == bare.evidence_id
    assert with_meta.meta.case_name.startswith("Associação Sindical")


def test_an_attempt_to_attach_a_user_is_refused() -> None:
    with pytest.raises(ValidationError):
        CachedText(
            session_id="s1",
            document_id="32014R8888",
            path="2(1)",
            text=TEXT,
            fetched_at="2026-09-04T10:00:00+00:00",
            user_id="advocate-42",  # type: ignore[call-arg]
        )


def test_the_public_api_takes_no_user_argument() -> None:
    for method in (SessionTextCache.remember, SessionTextCache.remember_unit):
        parameters = " ".join(inspect.signature(method).parameters).lower()
        for word in ("user", "client", "account", "subject_id"):
            assert word not in parameters


def test_the_cache_is_in_memory_only(cache: SessionTextCache) -> None:
    """Кеш не умеет писать на диск: то, чего некуда записать, сессию не переживёт."""
    for name in ("save", "load", "flush", "persist", "dump", "path", "file"):
        assert not hasattr(cache, name), f"кеш несёт {name!r} — содержимое переживёт сессию"


def test_a_fresh_cache_is_a_fresh_start() -> None:
    """Новый экземпляр не наследует содержимое предыдущего."""
    first = SessionTextCache()
    first.remember_unit("s1", _unit())

    second = SessionTextCache()
    assert len(second) == 0
    assert second.find("s1", "32014R8888", "2(1)") is None


# ---------------------------------------------------------------------------
# Точка, которую вызывают читающие инструменты (T044)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Квитанция чтения: доказательство приходит от источника (T179)
# ---------------------------------------------------------------------------


def test_a_receipt_exists_only_for_text_a_source_actually_returned() -> None:
    """Квитанцию даёт штамп живого или индексного чтения — и ничто другое."""
    from core import citations
    from core.provenance import ProvenanceStamp, SourceChannel

    def _stamp(channel: SourceChannel) -> ProvenanceStamp:
        return ProvenanceStamp(
            source_channel=channel,
            source_url="https://eur-lex.europa.eu/eli/reg/2014/8888/oj",
            language="en",
            citation_format="32014R8888, Article 2(1)",
            stale=False,
            is_authentic_version=True,
            is_translation=False,
            # Выверенный нами набор обязан нести дату сверки (принцип IV).
            verified_at="2026-09-07" if channel is SourceChannel.CURATED else None,
        )

    store = SessionTextCache(require_receipt=True)
    receipt = citations.issue_receipt(_stamp(SourceChannel.LIVE))
    assert store.remember("s1", "32014R8888", TEXT, path="2(1)", receipt=receipt) is not None

    # Выверенный нами набор — не ответ источника на этот запрос.
    with pytest.raises(citations.EvidenceWriteForbidden):
        citations.issue_receipt(_stamp(SourceChannel.CURATED))

    # Собрать квитанцию мимо issue_receipt конструктор не даёт.
    with pytest.raises(citations.EvidenceWriteForbidden):
        citations.FetchReceipt(
            object(),
            source_channel="live",
            source_url="https://example.invalid/",
            fetched_at="",
            content_hash="",
        )


# ---------------------------------------------------------------------------
# Квота агента внутри сессии (T180)
# ---------------------------------------------------------------------------


def test_one_agent_cannot_spend_the_whole_session_memory() -> None:
    """Исчерпавший квоту исследователь назван, остальные продолжают читать.

    До квоты лимит сессии был общим: четыре исследователя по сотне фрагментов
    выбирали 400 на всех, и каждый следующий получал `evidence_capacity`, из
    которого не следовало, чьё чтение его израсходовало.
    """
    store = SessionTextCache(max_entries_per_agent=3)

    for i in range(3):
        store.remember("s1", "32014R8888", f"{TEXT} {i}", path=f"2({i})", agent_id="researcher-eu")

    with pytest.raises(Exception) as exhausted:
        store.remember("s1", "32014R8888", f"{TEXT} 3", path="2(3)", agent_id="researcher-eu")
    assert "researcher-eu" in str(exhausted.value)

    # Соседний агент не пострадал, и общий лимит сессии не исчерпан.
    assert store.remember("s1", "280278", TEXT, path="12.1", agent_id="researcher-ua") is not None
    assert store.agent_usage("s1") == {"researcher-eu": 3, "researcher-ua": 1}


def test_a_read_without_an_agent_label_spends_only_the_session_limit() -> None:
    """Чтение без метки роли квотой агента не ограничено."""
    store = SessionTextCache(max_entries_per_agent=1)

    for i in range(5):
        store.remember("s1", "32014R8888", f"{TEXT} {i}", path=f"2({i})")

    assert len(store) == 5
    assert store.agent_usage("s1") == {}


def test_ending_the_session_forgets_the_agent_quota_too() -> None:
    store = SessionTextCache(max_entries_per_agent=2)
    store.remember("s1", "32014R8888", TEXT, path="2(1)", agent_id="researcher-eu")

    store.end_session("s1")

    assert store.agent_usage("s1") == {}
    assert store.count("s1") == 0
