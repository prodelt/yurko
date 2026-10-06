"""Регрессионный сторож реестра: cache/laws.json против официального каталога Рады.

Ловит то, чем реестр болел до тикета 16: правдоподобное название под чужим
идентификатором. «Порядок державної реєстрації лікарських засобів» открывался как
постанова про нагородження громадянина почесною грамотою — ссылка настоящая, ведёт
не туда. Отказ был бы безопаснее: пользователь увидел бы «не знайдено», а видел
уверенный ответ со ссылкой на официальный источник.

Эталон — `fixtures/rada_catalog_subset.json`, извлечённый из официального дампа
`data.rada.gov.ua/ogd/zak/laws/data/csv/doc.zip`, а НЕ написанный руками. Это принципиально:
первая версия этого теста сверялась с рукописной фикстурой, куда были вписаны выдуманные
названия — и «проходила», охраняя подделку.

Сеть не трогается (Constitution V): фикстура лежит в репозитории. Перегенерировать при
добавлении законов в реестр.
"""

import json
import re
from pathlib import Path

import pytest

# Доля значимых слов настоящего названия, которая должна найтись в нашем. Порог мягкий
# намеренно: наши названия бывают сокращены или снабжены пояснением в скобках. Он ловит
# не косметическую разницу, а подмену документа, где пересечение близко к нулю.
TITLE_MATCH_THRESHOLD = 0.6

# Названия актов, которые лишь меняют, отменяют или приостанавливают другие акты.
# Самостоятельного текста у них нет, и в реестре они бесполезны или прямо вредны:
# «Про призупинення дії пунктів 1, 2 наказу МОЗ N 360» попало сюда как «Правила
# зберігання ліків» — то есть вместо правил пользователь получал их приостановку.
_DERIVATIVE_PREFIXES = (
    "про внесення змін",
    "про внесення доповнень",
    "про призупинення дії",
    "про визнання такими, що втратили чинність",
    "про скасування",
)

_WORD = re.compile(r"[a-zа-яіїєґ0-9']+", re.IGNORECASE)
# Служебные слова, одинаковые у половины украинского законодательства.
_STOP = {
    "про",
    "щодо",
    "деякі",
    "деяких",
    "та",
    "для",
    "від",
    "України",
    "україни",
    "затвердження",
    "внесення",
    "змін",
    "актів",
    "питання",
}


def _tokens(title: str) -> set[str]:
    """Значимые слова названия, усечённые до основы (украинский склоняется)."""
    words = (w.lower() for w in _WORD.findall(title))
    return {w[:6] for w in words if len(w) > 4 and w not in _STOP}


@pytest.fixture(scope="module")
def rada_catalog() -> dict[str, str]:
    path = Path(__file__).parent / "fixtures" / "rada_catalog_subset.json"
    with open(path, encoding="utf-8") as f:
        return dict(json.load(f)["catalog"])


@pytest.fixture(scope="module")
def cache_laws() -> dict[str, dict]:
    path = Path(__file__).parent.parent / "cache" / "laws.json"
    with open(path, encoding="utf-8") as f:
        return dict(json.load(f)["laws"])


def test_every_registry_id_exists_in_catalog(cache_laws, rada_catalog):
    """Идентификатор, которого нет в каталоге Рады, — выдуманный."""
    missing = sorted(set(cache_laws) - set(rada_catalog))
    assert not missing, (
        f"{len(missing)} ID отсутствуют в каталоге Рады: {missing}. "
        "Либо ID выдуман, либо фикстуру нужно перегенерировать после добавления законов."
    )


def test_registry_titles_match_the_actual_documents(cache_laws, rada_catalog):
    """Название в реестре должно описывать тот документ, на который ведёт ID.

    Это тот самый сторож: до тикета 16 реестр подставлял под фармацевтические названия
    посторонние акты, и ни один тест этого не видел.
    """
    mismatches = []
    for law_id, meta in sorted(cache_laws.items()):
        real = rada_catalog.get(law_id)
        if real is None:
            continue  # покрыто предыдущим тестом
        real_tokens = _tokens(real)
        if not real_tokens:
            continue
        ours_tokens = _tokens(str(meta.get("title", "")))
        overlap = len(real_tokens & ours_tokens) / len(real_tokens)
        if overlap < TITLE_MATCH_THRESHOLD:
            mismatches.append(f"{law_id}: у нас {meta.get('title', '')!r} -> реально {real!r}")

    assert not mismatches, "Реестр ведёт на другие документы:\n" + "\n".join(mismatches)


def test_no_derivative_acts_posing_as_standalone_laws(cache_laws, rada_catalog):
    """Акт-поправка не имеет самостоятельного текста и в реестре бесполезен.

    Так в реестр попал `2002-19`: назван «ЗУ Про заклади охорони здоров'я», а на деле
    «Про внесення змін до деяких законодавчих актів...». Совпадение слов маскировало подмену.
    """
    amending = [
        f"{law_id}: {rada_catalog[law_id]}"
        for law_id in sorted(cache_laws)
        if law_id in rada_catalog and rada_catalog[law_id].lower().startswith(_DERIVATIVE_PREFIXES)
    ]
    assert (
        not amending
    ), "Акты-поправки в реестре (самостоятельного текста не имеют):\n" + "\n".join(amending)


# ---------------------------------------------------------------------------
# T046 — конверт и обрубки: заглушка не подаётся как документ
#
# Общий сторож этого файла ловит подмену внутри украинского реестра; здесь —
# тот же принцип (обрубок опаснее отсутствия записи, CONTEXT.md) на уровне
# общего для всех источников контракта `sources/stub_detection.py` (T014):
# страница проверки на автоматический доступ, оглавление и страница ошибки
# обязаны давать `upstream_stub_detected`, а не текст документа.
# ---------------------------------------------------------------------------


class TestUpstreamStubDetectedNotContent:
    def _detected_by(self, text: str) -> str:
        from sources.stub_detection import detect_stub

        verdict = detect_stub(text)
        assert verdict is not None, f"обрубок не распознан: {text[:80]!r}"
        return verdict.as_detected_by()

    def test_antibot_challenge_page_is_a_stub_not_a_document(self):
        marker = self._detected_by(
            "Будь ласка, підтвердьте, що ви не робот, перш ніж продовжити перегляд сторінки."
        )
        assert "antibot_challenge" in marker

    def test_table_of_contents_is_a_stub_not_a_document(self):
        toc = "\n".join(f"{i}. Стаття {i} — короткий заголовок" for i in range(1, 20))
        marker = self._detected_by(toc)
        assert "table_of_contents" in marker

    def test_error_page_served_with_a_200_status_is_a_stub(self):
        marker = self._detected_by("Помилка 404: Сторінку не знайдено на цьому сервері.")
        assert "error_page" in marker

    def test_a_real_looking_short_document_is_still_flagged_too_short(self):
        marker = self._detected_by("Стаття 1. Короткий текст.")
        assert "too_short" in marker

    def test_an_ordinary_long_article_is_not_flagged_as_a_stub(self):
        from sources.stub_detection import is_stub

        real_text = "Стаття 1. Основні положення\n\n" + "Це положення регулює відносини. " * 60
        assert not is_stub(real_text)

    def test_the_typed_failure_names_which_marker_triggered_it(self):
        """`upstream_stub_detected` обязан назвать признак подмены (T014):
        отклонение неотличимо от произвола, если признак не виден."""
        from core.contracts import FailureCode, UpstreamStubDetected
        from sources.stub_detection import detect_stub

        verdict = detect_stub("please verify you are a human before continuing")
        assert verdict is not None

        failure = UpstreamStubDetected(
            source="test_source",
            detected_by=verdict.as_detected_by(),
            document_id="42",
        )
        output = failure.as_output()
        assert output["code"] == FailureCode.UPSTREAM_STUB_DETECTED.value
        assert output["details"]["detected_by"]
        assert "42" in output["error"]

    def test_registry_adapter_rejects_a_stub_instead_of_returning_it(self):
        """`sources/base.py` встраивает `stub_detection` (T014); adapter'ы,
        унаследованные от `RegistryAdapter`, не могут вернуть заглушку в
        обход этой проверки (FR-012)."""
        from core.contracts import SourceHealth, SourcePolicy
        from sources.base import AdapterResult, RegistryAdapter

        class _ProbeAdapter(RegistryAdapter):
            source_id = "probe_source"
            legal_order = "EU"
            source_policy = SourcePolicy.API

            def policy(self):  # pragma: no cover - not exercised here
                raise NotImplementedError

            def fetch(self, document_id: str, **options) -> AdapterResult:  # pragma: no cover
                raise NotImplementedError

            def search(self, query: str, **options) -> AdapterResult:  # pragma: no cover
                raise NotImplementedError

            def health(self) -> SourceHealth:  # pragma: no cover
                raise NotImplementedError

        adapter = _ProbeAdapter()
        refusal = adapter.reject_stub(
            "checking your browser before accessing the site", document_id="123"
        )
        assert refusal is not None
        assert refusal.as_output()["code"] == "upstream_stub_detected"
        assert refusal.source == "probe_source"

        # A real document produces no refusal — the guard does not cry wolf.
        assert adapter.reject_stub("Стаття 1. Основні положення. " * 60, document_id="123") is None
