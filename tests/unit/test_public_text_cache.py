"""Тести кешу публічних текстів між сеансами (T297, D-14, FR-530).

Межа, яку кеш не перетинає: доказом лишається текст, звірений **у цьому**
сеансі. Тому тут не перевіряється «доказ ожив» — перевіряється інше:

* публічний текст переживає процес і адресується редакцією та мовною версією;
* повторне читання дає три різні відповіді — той самий текст, змінений текст,
  адреса вперше;
* обсяг перечитування рахується наперед, а не після.

Матеріалів справи в кеші немає за побудовою: у нього кладе тільки
``routing.perform_read``, тобто те, що прийшло з публічного джерела.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import source_cache  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Кеш кожного тесту свій: спільний каталог зробив би тести залежними."""
    monkeypatch.setenv(source_cache.CACHE_DIR_ENV, str(tmp_path))


KEY = source_cache.source_key(
    legal_order="EU",
    document_id="32014R8888",
    path="2(1)",
    version_id="02014R8888-20260807",
    language="en",
)


class TestSourceKey:
    """Адреса джерела: що входить у неї і чому."""

    def test_revision_is_part_of_the_address(self) -> None:
        """Дві редакції одного акта — дві різні адреси."""
        first = source_cache.source_key(document_id="32014R8888", version_id="v1")
        second = source_cache.source_key(document_id="32014R8888", version_id="v2")

        assert first != second

    def test_language_version_is_part_of_the_address(self) -> None:
        assert source_cache.source_key(document_id="x", language="en") != source_cache.source_key(
            document_id="x", language="uk"
        )

    def test_legal_order_is_normalised(self) -> None:
        assert source_cache.source_key(legal_order="eu") == source_cache.source_key(
            legal_order="EU"
        )


class TestStoreAndLoad:
    """Текст переживає процес."""

    def test_content_hash_with_a_path_separator_is_refused(self) -> None:
        """Ім'я файла кеша — хеш, а не шлях: інакше кеш пише куди завгодно."""
        with pytest.raises(ValueError):
            source_cache.public_text_cache_path("../../etc/passwd")

    def test_empty_content_hash_is_refused(self) -> None:
        with pytest.raises(ValueError):
            source_cache.public_text_cache_path("   ")


class TestCompareWithCached:
    """Три відповіді, і всі три названі (D-14)."""

    def test_unknown_address(self) -> None:
        assert source_cache.compare_with_cached(KEY, "текст")["state"] == "unknown"

    def test_same_text_after_a_new_process(self) -> None:
        text = "Стаття 2(1). Товари реєструються."
        first = source_cache.compare_with_cached(KEY, text)
        source_cache.remember_source(KEY, str(first["content_hash"]), text, session_id="s-1")

        second = source_cache.compare_with_cached(KEY, text)

        assert second["state"] == "same"
        assert second["previous_hash"] == first["content_hash"]

    def test_changed_source_is_a_visible_fact(self) -> None:
        """Розбіжність не гаситься: редакція змінилася — це окремий факт."""
        first_text = "Стаття 2(1). Товари реєструються."
        digest = str(source_cache.compare_with_cached(KEY, first_text)["content_hash"])
        source_cache.remember_source(KEY, digest, first_text, session_id="s-1")

        comparison = source_cache.compare_with_cached(KEY, "Стаття 2(1). Інший текст.")

        assert comparison["state"] == "changed"
        assert comparison["previous_hash"] == digest
        assert comparison["content_hash"] != digest

    def test_remember_updates_the_address_to_the_new_text(self) -> None:
        source_cache.remember_source(KEY, "a" * 64, "перший", session_id="s-1")
        new_digest = str(source_cache.compare_with_cached(KEY, "другий")["content_hash"])
        source_cache.remember_source(KEY, new_digest, "другий", session_id="s-2")

        assert source_cache.compare_with_cached(KEY, "другий")["state"] == "same"


class TestCacheNeverBreaksReading:
    """Відмова кеша не стає відмовою читання."""

    def test_broken_index_reads_as_empty(self, tmp_path: Path) -> None:
        source_cache.text_index_path().write_text("не json", encoding="utf-8")

        assert source_cache.load_text_index() == {}
        assert source_cache.lookup_source("k1") is None

    def test_remember_without_text_is_a_quiet_no(self) -> None:
        assert source_cache.remember_source("k1", "a" * 64, "") is False
        assert source_cache.remember_source("", "a" * 64, "текст") is False
        assert source_cache.remember_source("k1", "", "текст") is False


class TestThroughTheReadingPath:
    """Кеш наповнюється самим читанням, а не окремою командою."""

    @staticmethod
    def _read(text: str, session_id: str) -> dict:
        """Одне справжнє читання через ``routing.perform_read``."""
        import sources
        from core.legal_orders import DocumentClass, Operation
        from core.provenance import ContentKind, PublicationKind, SourceChannel
        from core.routing import ReadRequest, perform_read
        from sources.base import AdapterPayload
        from core.provenance import ProvenanceStamp

        class _Adapter:
            source_id = "eu_law_eurlex_cellar"

            def fetch(self, document_id: str, **options: object) -> AdapterPayload:
                return AdapterPayload(
                    data={"text": text, "title": "Regulation 8888/2014", "locator": "2(1)"},
                    provenance=ProvenanceStamp(
                        source_channel=SourceChannel.LIVE,
                        source_url="https://publications.europa.eu/resource/celex/32014R8888",
                        language="en",
                        version_id="02014R8888-20260807",
                        citation_format="CELEX 32014R8888, article 2(1)",
                        stale=False,
                        is_authentic_version=True,
                        is_translation=False,
                        publication_kind=PublicationKind.OFFICIAL_JOURNAL,
                        content_kind=ContentKind.FRAGMENT,
                    ),
                )

        original = sources.get_adapter("eu_law_eurlex_cellar")
        sources.register_adapter(_Adapter())
        try:
            return perform_read(
                ReadRequest(
                    legal_order="EU",
                    operation=Operation.READ_FRAGMENT,
                    document_class=DocumentClass.ACT,
                    document_id="32014R8888",
                    path="2(1)",
                    tool="get_article",
                ),
                session_id=session_id,
            )
        finally:
            if original is not None:
                sources.register_adapter(original)

    def test_second_read_of_the_same_text_is_an_instant_check(self) -> None:
        """Той самий текст у новому сеансі: доказ новий, звірка миттєва."""
        text = "Article 2(1). All products shall be recorded."
        first = self._read(text, "s-one")

        second = self._read(text, "s-two")

        assert second["cache_state"] == "same"
        assert second["previous_content_hash"] == second["content_hash"]
        # Доказ не «ожив»: він виданий заново пам'яттю цього сеансу.
        assert second["evidence_id"] != first["evidence_id"]

    def test_changed_source_is_named_on_the_next_read(self) -> None:
        self._read("Article 2(1). All products shall be recorded.", "s-one")

        second = self._read("Article 2(1). Products are exempted.", "s-two")

        assert second["cache_state"] == "changed"
        assert second["previous_content_hash"] != second["content_hash"]
