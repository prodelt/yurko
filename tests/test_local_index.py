"""The local FTS5 index: relevant first, or an honest refusal — never both wrong.

Everything runs on a temporary directory built by this file. No network, no
cache/ of the repository is read or written (Constitution V).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from search import local_index
from search.local_index import IndexUnavailable, LocalSearchResult

#: Contains «Відкликання ліцензій». This is the article a lawyer asking about
#: revoked licences wants, and the one a substring scorer would never find.
LAW_ON_LICENCES = (
    "Стаття 21.\n"
    "Видача ліцензій\n"
    "\n"
    "1. Ліцензія видається за заявою суб'єкта господарювання.\n"
    "\n"
    "Стаття 22.\n"
    "Відкликання ліцензій на провадження господарської діяльності\n"
    "\n"
    "1. Орган ліцензування зобов'язаний відкликати ліцензію, видану з\n"
    "порушенням закону, без попереднього погодження.\n"
    "\n"
    "Стаття 23.\n"
    "Порядок оскарження\n"
    "\n"
    "1. Рішення може бути оскаржено в судовому порядку.\n"
)

#: The lure. It mentions «ліцензії» and nothing about revocation, and on the
#: substring density scorer it takes first place for the query below, because
#: «ліцензій» in the licensing act does not contain «ліцензії».
LAW_ON_THINGS = (
    "Стаття 179.\n"
    "Поняття речі\n"
    "\n"
    "1. Річчю є предмет матеріального світу.\n"
    "\n"
    "Стаття 179\n"
    "-\n"
    "1\n"
    ".\n"
    "Поняття цифрової речі\n"
    "\n"
    "1. Цифровою річчю є благо, що створюється та існує виключно у цифровому\n"
    "середовищі; права на неї передаються, зокрема, на умовах ліцензії.\n"
    "\n"
    "Стаття 180.\n"
    "Тварини\n"
    "\n"
    "1. Тварини є особливим об'єктом цивільних прав.\n"
)

#: КМУ-style act: пункти, not статті, so parse_articles finds nothing and the
#: builder has to fall back on fragments instead of dropping the document.
RESOLUTION_WITHOUT_ARTICLES = (
    "ПОРЯДОК\n"
    "використання коштів резервного фонду\n"
    "\n"
    "1. Цей Порядок визначає механізм використання коштів.\n"
    "\n"
    "2. Кошти спрямовуються на невідкладні потреби.\n"
)


def _write_law(
    directory: Path,
    law_id: str,
    title: str,
    text: str,
    articles: dict[str, str] | None = None,
) -> None:
    payload = {
        "law_id": law_id,
        "title": title,
        "url": f"https://example.invalid/{law_id}",
        "text": text,
        "articles": articles or {},
        "char_count": len(text),
    }
    (directory / f"{law_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    texts = tmp_path / "texts"
    texts.mkdir()
    _write_law(texts, "9001-20", "Про ліцензування (тестовий текст)", LAW_ON_LICENCES)
    # The cached "articles" dict is deliberately wrong — it is what the old
    # parser produced. The builder must ignore it and re-parse the text.
    _write_law(
        texts,
        "435-15",
        "Цивільний кодекс України",
        LAW_ON_THINGS,
        articles={"179": "Стаття 179 - 1 . Поняття цифрової речі"},
    )
    _write_law(texts, "1178-2022-п", "Про здійснення закупівель", RESOLUTION_WITHOUT_ARTICLES)
    return texts


@pytest.fixture()
def index_path(tmp_path: Path) -> Path:
    return tmp_path / "index" / local_index.INDEX_FILENAME


def test_revoked_licences_query_does_not_return_the_digital_thing_article(
    corpus: Path, index_path: Path
) -> None:
    """The measured defect, stated as a test.

    On the substring scorer a two-word query put ЦК ст. 179-1 «Поняття
    цифрової речі» at the top, because the only literal hit in the whole corpus
    was one of the words inside it. The index must put the licensing article
    first — and, since it AND-joins stem prefixes, must not return the
    digital-thing article at all.
    """
    local_index.build_index(corpus, index_path=index_path)
    result = local_index.search_local("відкликані ліцензії", index_path=index_path, limit=5)

    assert isinstance(result, LocalSearchResult)
    assert result.hits, "релевантна стаття є в корпусі — порожня видача була б хибною"
    assert result.hits[0].law_id == "9001-20"
    assert result.hits[0].article_num == "22"
    assert "Відкликання ліцензій" in result.hits[0].heading
    assert all(hit.article_num != "179-1" for hit in result.hits)
    assert all(hit.law_id != "435-15" for hit in result.hits)


def test_prefix_rewriting_is_what_makes_the_query_reach_the_article(corpus: Path) -> None:
    """Without trimming there is no morphology at all: the query matches nothing.

    «відкликані» must reach «Відкликання» and «ліцензії» must reach «ліцензій»;
    there is no Ukrainian stemmer here (nor in the Postgres path, which is
    'simple' + 'unaccent'), so the compensation is a stem-shaped prefix.
    """
    assert local_index.rewrite_query("відкликані ліцензії") == '"відклик"* AND "ліцен"*'
    # Stop words are dropped; a token shorter than the floor stays verbatim,
    # because a 2-3 letter prefix would match a large share of the corpus. A
    # token at the floor keeps all its letters and only gains the prefix star.
    assert local_index.rewrite_query("про суд та вирок") == '"суд" AND "вирок"*'


def test_documents_without_articles_are_still_covered(corpus: Path, index_path: Path) -> None:
    """КМУ постанови use пункти; dropping them would shrink coverage silently."""
    report = local_index.build_index(corpus, index_path=index_path)

    assert "1178-2022-п" in report.covered_law_ids
    result = local_index.search_local("резервного фонду", index_path=index_path)
    assert isinstance(result, LocalSearchResult)
    assert result.hits[0].law_id == "1178-2022-п"
    assert result.hits[0].article_num.startswith("frag-")


def test_index_reparses_text_instead_of_trusting_cached_articles(
    corpus: Path, index_path: Path
) -> None:
    """cache/texts carries articles built by the broken parser; re-parse fixes it."""
    local_index.build_index(corpus, index_path=index_path)
    result = local_index.search_local("цифрової речі", index_path=index_path)

    assert isinstance(result, LocalSearchResult)
    assert result.hits[0].article_num == "179-1"


def test_missing_index_refuses_instead_of_returning_nothing(index_path: Path) -> None:
    """An empty list would read as «searched everything, found nothing»."""
    result = local_index.search_local("відкликані ліцензії", index_path=index_path)

    assert isinstance(result, IndexUnavailable)
    assert result.reason == "not_built"
    assert result.manual_path.strip()
    assert "zakon.rada.gov.ua" in result.manual_path


def test_empty_index_refuses_and_names_the_manual_path(tmp_path: Path, index_path: Path) -> None:
    empty = tmp_path / "empty-texts"
    empty.mkdir()
    report = local_index.build_index(empty, index_path=index_path)
    assert report.indexed_documents == 0

    result = local_index.search_local("відкликані ліцензії", index_path=index_path)
    assert isinstance(result, IndexUnavailable)
    assert result.reason == "empty"
    assert result.manual_path.strip()


def test_index_of_another_schema_version_refuses(corpus: Path, index_path: Path) -> None:
    """A layout this code does not understand must say so, not answer emptily."""
    local_index.build_index(corpus, index_path=index_path)
    connection = sqlite3.connect(index_path)
    connection.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")
    connection.commit()
    connection.close()

    result = local_index.search_local("відкликані ліцензії", index_path=index_path)
    assert isinstance(result, IndexUnavailable)
    assert result.reason == "schema_mismatch"


def test_failed_build_leaves_no_half_written_file(corpus: Path, index_path: Path) -> None:
    """A crash mid-build must not park a partial index next to the live one."""
    index_path.parent.mkdir(parents=True, exist_ok=True)

    def _explode(*_args: object, **_kwargs: object) -> dict[str, str]:
        raise RuntimeError("збій під час розбору")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(local_index.SearchEngine, "parse_articles", _explode)
        with pytest.raises(RuntimeError):
            local_index.build_index(corpus, index_path=index_path)

    assert not list(index_path.parent.glob("*.tmp-*"))
    assert not index_path.exists()


def test_query_of_service_words_only_refuses(corpus: Path, index_path: Path) -> None:
    local_index.build_index(corpus, index_path=index_path)
    result = local_index.search_local("та або що", index_path=index_path)

    assert isinstance(result, IndexUnavailable)
    assert result.reason == "unusable_query"


def test_result_states_the_boundary_of_what_was_searched(corpus: Path, index_path: Path) -> None:
    """The lawyer must not read a hot-cache hit as a search of all Ukrainian law."""
    local_index.build_index(corpus, index_path=index_path)
    result = local_index.search_local("відкликані ліцензії", index_path=index_path)

    assert isinstance(result, LocalSearchResult)
    assert set(result.covered_law_ids) == {"9001-20", "435-15", "1178-2022-п"}
    assert "не пошук по всьому праву України" in result.scope_note
    assert str(result.indexed_documents) in result.scope_note


def test_rebuild_is_idempotent(corpus: Path, index_path: Path) -> None:
    """Constitution IX: running it twice must not double the corpus or fail."""
    first = local_index.build_index(corpus, index_path=index_path)
    second = local_index.build_index(corpus, index_path=index_path)

    assert first.indexed_documents == second.indexed_documents
    assert first.covered_law_ids == second.covered_law_ids
    assert not list(index_path.parent.glob("*.tmp-*")), "тимчасовий файл не прибрано"
    assert not list(index_path.parent.glob("*-journal")), "журнал пережив перейменування"

    result = local_index.search_local("відкликані ліцензії", index_path=index_path)
    assert isinstance(result, LocalSearchResult)
    assert len(result.hits) == 1


def test_rebuild_replaces_the_live_index_atomically(corpus: Path, index_path: Path) -> None:
    """A rebuild over a smaller corpus must leave one consistent index, not two."""
    local_index.build_index(corpus, index_path=index_path)
    smaller = corpus.parent / "smaller"
    smaller.mkdir()
    _write_law(smaller, "9001-20", "Про ліцензування", LAW_ON_LICENCES)

    report = local_index.build_index(smaller, index_path=index_path)
    assert report.covered_law_ids == ("9001-20",)

    result = local_index.search_local("цифрової речі", index_path=index_path)
    assert isinstance(result, LocalSearchResult)
    assert result.hits == ()
    assert result.covered_law_ids == ("9001-20",)


def test_default_location_comes_from_the_shared_cache_dir(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No second opinion about where the cache lives.

    The owner names the cache directory; an index that computed its own place
    would sit where nothing else in the product looks for it — or inside a case
    folder, which is exactly what source_cache exists to prevent.
    """
    monkeypatch.setenv("YURKO_CACHE_DIR", str(tmp_path / "owner-cache"))

    assert local_index.default_index_path() == tmp_path / "owner-cache" / "local_index.sqlite3"
    local_index.build_index(corpus)
    result = local_index.search_local("відкликані ліцензії")

    assert isinstance(result, LocalSearchResult)
    assert result.hits[0].article_num == "22"
