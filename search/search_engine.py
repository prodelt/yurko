"""Text parsing and relevance search helpers."""

from __future__ import annotations

import re
from typing import Any

#: Kinds of score `match_query` can return. They were previously all called
#: "score" and silently sorted together while being different units: a per-word
#: density (~11 for one hit in a short article) always beat the fallback's raw
#: token count (1-2) and the caller's fixed 1.0 for alias candidates, so an
#: unparsed law could never win however well it matched.
#:
#: `direct_reference`, `article_density` and `line_fallback` now share one unit
#: (see `_density`), so the ordering rule is: sort by `score`, and use
#: SCORE_KIND_RANK only to break a tie. `alias_only` is the exception — it is
#: not a measurement at all, just "this law's alias matched", so it belongs
#: after every scored result rather than inside the same ordering:
#:
#:     key=lambda hit: (hit.kind != SCORE_KIND_ALIAS, hit.score, SCORE_KIND_RANK[hit.kind])
#:
#: Ranking by kind first would put the defect back one layer up.
SCORE_KIND_DIRECT = "direct_reference"
SCORE_KIND_ARTICLE = "article_density"
SCORE_KIND_LINE = "line_fallback"
#: Not produced here: the caller's own "this law's alias matched, take its head"
#: candidate. Named in this table so its rank is decided in one place.
SCORE_KIND_ALIAS = "alias_only"

SCORE_KIND_RANK: dict[str, int] = {
    SCORE_KIND_DIRECT: 3,
    SCORE_KIND_ARTICLE: 2,
    SCORE_KIND_LINE: 1,
    SCORE_KIND_ALIAS: 0,
}


class SearchEngine:
    """Parses law text into articles and finds relevant snippets."""

    _MAX_RANKED_ARTICLES = 6
    _MAX_FALLBACK_LINES = 8

    #: Prefix rule for query tokens, the same one local_index.rewrite_query uses
    #: for FTS5: a token of at least `_MIN_PREFIX_CHARS` characters is cut back
    #: by up to `_STEM_TRIM_CHARS` to a stem-shaped prefix, a shorter one is
    #: required verbatim. There is no Ukrainian stemmer on either path, so this
    #: is what lets `_density` demand every token (AND) without the demand being
    #: unsatisfiable: «ліцензії» has to be able to reach «ліцензій».
    #:
    #: Spelled out here instead of imported: local_index imports this module
    #: (for `SearchEngine` and for `_stop_words`), so the dependency cannot point
    #: both ways. That makes this the copy to keep in step with the other.
    _MIN_PREFIX_CHARS = 5
    _STEM_TRIM_CHARS = 3

    _stop_words = frozenset(
        {
            "та",
            "або",
            "що",
            "як",
            "для",
            "про",
            "при",
            "до",
            "від",
            "на",
            "в",
            "у",
            "з",
            "і",
            "а",
            "але",
            "це",
            "є",
            "не",
            "ні",
            "після",
            "між",
            "через",
            "над",
            "під",
            "перед",
            "без",
            "по",
            "із",
            "зі",
        }
    )

    # The number may carry whitespace around its dash. Rada's print layout wraps
    # «Стаття 179-1.» as "Стаття 179\n-\n1\n.", so a pattern that required the
    # dash to touch the digits captured only "179" — see _collect_sections.
    _article_header = re.compile(
        r"^[Сс]таття\s+(\d+(?:\s{0,3}[-–]\s{0,3}\d+)?)\s*[.)]?", re.MULTILINE
    )
    _article_number_spacing = re.compile(r"\s+")
    _point_header = re.compile(r"^(\d+)\.\s+[А-ЯІЇЄҐа-яіїєґ]", re.MULTILINE)
    _tokenizer = re.compile(r"[A-Za-zА-Яа-яІіЇїЄєҐґ0-9-]+")
    _quoted_excerpt_markers = '"«„'

    def parse_articles(self, text: str) -> dict[str, str]:
        if not text:
            return {}

        articles = self._collect_sections(self._article_header, text)
        if len(articles) >= 2:
            return articles

        fallback = self._collect_sections(self._point_header, text)
        return fallback if len(fallback) >= 3 else {}

    def chunk_text(self, text: str, target_chars: int = 1500) -> dict[str, str]:
        """Split free-form text into sequential, FTS/vector-friendly fragments.

        Used as an ingestion fallback for laws whose structure ``parse_articles``
        cannot detect (e.g. КМУ постанови with «пункти», not «статті»). Keys are
        ``frag-0001`` … so they never collide with real article numbers in the
        in-memory MCP path. Paragraph boundaries are preserved; oversized
        paragraphs are hard-split so no fragment grossly exceeds ``target_chars``.
        """
        if not text or not text.strip():
            return {}

        max_chars = max(target_chars, 200)
        paragraphs = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]

        pieces: list[str] = []
        buffer = ""
        for paragraph in paragraphs:
            while len(paragraph) > max_chars:
                pieces.append(paragraph[:max_chars])
                paragraph = paragraph[max_chars:].strip()
            if not paragraph:
                continue
            if not buffer:
                buffer = paragraph
            elif len(buffer) + len(paragraph) + 2 <= max_chars:
                buffer = f"{buffer}\n\n{paragraph}"
            else:
                pieces.append(buffer)
                buffer = paragraph
        if buffer:
            pieces.append(buffer)

        return {f"frag-{idx:04d}": piece for idx, piece in enumerate(pieces, start=1) if piece}

    def _collect_sections(self, pattern: re.Pattern[str], text: str) -> dict[str, str]:
        matches = list(pattern.finditer(text))
        if not matches:
            return {}

        result: dict[str, str] = {}
        for idx, match in enumerate(matches):
            start = match.start()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
            preceding = text[:start].rstrip()
            if preceding and preceding[-1] in self._quoted_excerpt_markers:
                # ponytail: quoted excerpt inserted into ANOTHER act by this law's
                # Прикінцеві положення (e.g. "статтю 164-14 КУпАП викласти в такій
                # редакції: "Стаття 164-14. ..."") — not this law's own article.
                continue
            # The key must keep the FULL number. While the header pattern read
            # the wrapped "Стаття 179\n-\n1\n." as plain "179", this assignment
            # stored ст. 179-1 «Поняття цифрової речі» under the key "179" and
            # overwrote ст. 179 «Поняття речі» — so get_article("179") returned
            # the wrong article, and a search hit labelled "[Стаття 179]" showed
            # the text of another one. Across cache/texts this collapsed 2814
            # articles onto 2323 keys (КУпАП alone: 473 -> 197).
            article_num = self._article_number_spacing.sub("", match.group(1)).replace("–", "-")
            result[article_num] = text[start:end].strip()
        return result

    def fuzzy_match_article(
        self, article_query: str, articles: dict[str, str]
    ) -> tuple[str, str] | None:
        target = article_query.strip().replace("–", "-")
        if not target:
            return None
        if target in articles:
            return target, articles[target]

        for article_num, article_text in articles.items():
            if article_num.startswith(f"{target}-") or article_num.startswith(f"{target}."):
                return article_num, article_text
        return None

    def search_in_text(
        self,
        text: str,
        query: str,
        context_chars: int = 3000,
        articles: dict[str, str] | None = None,
    ) -> str:
        """Return the matching snippet, or an empty string when nothing matched.

        The empty string is the whole point: to a lawyer «нічого не знайдено» and
        «ось початок акта» are two different statements, and handing over the
        second while the first is true is not allowed. This used to answer a
        query it could not match with `text[:context_chars]` — the preamble of
        the act, indistinguishable from a real hit — so a search for a term the
        law does not contain came back as a confident quotation from that law.

        The empty query keeps its old meaning on purpose: asking without a term
        is asking for the document, and the head of the text is the answer to
        that question, not a failed match dressed up as one.
        """
        if not text:
            return ""

        if not query.strip():
            return text[:context_chars]

        matched = self.match_query(text, query, context_chars=context_chars, articles=articles)
        if matched is None:
            return ""
        return str(matched["snippet"])

    def match_query(
        self,
        text: str,
        query: str,
        context_chars: int = 3000,
        articles: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        if not text:
            return None

        tokens = self._tokenize(query)
        if not tokens:
            return None

        direct = self._extract_article_reference(query)
        if direct and articles and direct in articles:
            return {
                "score": 10_000.0,
                "score_kind": SCORE_KIND_DIRECT,
                "snippet": articles[direct][:context_chars],
            }

        if articles:
            scored = self._score_articles(tokens, articles)
            if scored:
                return {
                    "score": float(scored[0][0]),
                    "score_kind": SCORE_KIND_ARTICLE,
                    "snippet": self._join_ranked_articles(scored, context_chars),
                }

        fallback = self._line_based_fallback(text, tokens, context_chars)
        if fallback is None:
            return None
        return {
            "score": float(fallback["score"]),
            "score_kind": SCORE_KIND_LINE,
            "snippet": fallback["snippet"],
        }

    def _tokenize(self, query: str) -> list[str]:
        """The searchable words of the query — and nothing when there are none.

        Was `filtered or raw`: a question built entirely of service words («та
        або що») kept those words as its tokens, and the scorer went on to rank
        passages by how often they say «та». The lexical index refuses the very
        same query instead of answering it (local_index.py, `IndexUnavailable`
        with reason `unusable_query`: «у запиті немає слів, придатних для
        пошуку»), and the two paths must not disagree about whether a question is
        searchable at all. An empty list makes `match_query` return None, which
        every caller already reads as "no match".
        """
        tokens = [token.lower() for token in self._tokenizer.findall(query)]
        # The tokenizer admits the dash on its own, so «ст. 179 - 1» yields a
        # bare "-". Harmless while tokens were summed; under the AND rule of
        # `_density` a token no passage contains would veto every passage.
        return [token for token in tokens if token.strip("-") and token not in self._stop_words]

    def _extract_article_reference(self, query: str) -> str | None:
        match = re.search(r"(?:ст\.?\s*|стаття\s+)(\d+(?:[-–]\d+)?)", query.lower())
        return match.group(1).replace("–", "-") if match else None

    def _score_articles(
        self, tokens: list[str], articles: dict[str, str]
    ) -> list[tuple[float, str, str]]:
        scored: list[tuple[float, str, str]] = []
        for article_num, article_text in articles.items():
            score = self._density(article_text, tokens)
            if score > 0:
                scored.append((score, article_num, article_text))
        scored.sort(reverse=True)
        return scored

    def _density(self, passage: str, tokens: list[str]) -> float:
        """Token hits per 1000 words — or 0.0 unless EVERY token is present.

        The all-tokens requirement comes first and is a veto. Summing hits over
        the tokens without it made a two-word query answer confidently out of
        a passage that holds only one of the words: exactly the OR fan-out local_index.py
        forbids itself ("Terms are joined with AND, never OR"), and a confident
        answer about the wrong thing is worse than an empty one (Constitution
        III). The veto is expressed as 0.0, so `_score_articles`' existing
        `score > 0` stays the one place that filters.

        A hit is a word of the passage that STARTS with the token's stem prefix
        (`_MIN_PREFIX_CHARS` / `_STEM_TRIM_CHARS`), not a substring anywhere in
        it. Without the prefix the AND would be unsatisfiable in Ukrainian — no
        stemmer here, and «ліцензії» would never reach «ліцензій»; with a plain
        substring it would be too loose in the other direction.

        Ranking among the passages that pass the veto is unchanged: hits per
        1000 words, the single unit `match_query` compares its score kinds on.
        """
        lowered = passage.lower()
        words = self._tokenizer.findall(lowered)
        size = max(len(lowered.split()), 1)
        total = 0.0
        for token in tokens:
            hits = self._count_hits(words, token)
            if not hits:
                return 0.0
            total += hits * 1000 / size
        return total

    def _count_hits(self, words: list[str], token: str) -> int:
        """How many words of a passage the query token reaches, by prefix."""
        if len(token) < self._MIN_PREFIX_CHARS:
            # Too short to trim: a two- or three-character prefix would match
            # half the corpus, so the token is required as a whole word instead.
            return sum(1 for word in words if word == token)
        stem = token[: max(self._MIN_PREFIX_CHARS, len(token) - self._STEM_TRIM_CHARS)]
        return sum(1 for word in words if word.startswith(stem))

    def _join_ranked_articles(
        self, scored: list[tuple[float, str, str]], context_chars: int
    ) -> str:
        chunks: list[str] = []
        total = 0
        for _, article_num, article_text in scored[: self._MAX_RANKED_ARTICLES]:
            chunk = f"[Стаття {article_num}]\n\n{article_text}"
            remaining = context_chars - total
            if remaining <= 0:
                break
            if len(chunk) <= remaining:
                chunks.append(chunk)
                total += len(chunk)
            else:
                chunks.append(chunk[:remaining])
                break
        return "\n\n---\n\n".join(chunks)

    def _line_based_fallback(
        self, text: str, tokens: list[str], context_chars: int
    ) -> dict[str, Any] | None:
        lines = text.splitlines()
        matches: list[tuple[float, int]] = []
        for index, line in enumerate(lines):
            # Was `sum(1 for token in tokens if token in lowered)`: how many of
            # the query tokens the line contained. Under the AND rule of
            # `_density` every surviving line contains all of them, so that count
            # would be the constant len(tokens) and the sort below would decide
            # by line number — "the last matching line wins". Density keeps the
            # ordering meaningful and is the unit the article path ranks by.
            score = self._density(line, tokens)
            if score > 0:
                matches.append((score, index))

        if not matches:
            return None

        matches.sort(reverse=True)
        snippets: list[str] = []
        total = 0
        for _, line_index in matches[: self._MAX_FALLBACK_LINES]:
            fragment = self._window(lines, line_index)
            remaining = context_chars - total
            if remaining <= 0:
                break
            if len(fragment) <= remaining:
                snippets.append(fragment)
                total += len(fragment)
            else:
                snippets.append(fragment[:remaining])
                break
        # Was `matches[0][0]`: how many query tokens the best line contained, an
        # integer 1..len(tokens). Mixed by the caller with _score_articles' ~11+
        # density it lost every comparison, so a law whose articles did not parse
        # ranked below a parsed law that matched one word of the question. Scoring
        # the returned window with the same density puts both on one scale, so
        # the comparison is now decided by the match; `score_kind` only breaks a
        # tie (it must not be the primary sort key — see SCORE_KIND_RANK).
        return {
            "score": self._density(self._window(lines, matches[0][1]), tokens),
            "snippet": "\n\n---\n\n".join(snippets),
        }

    def _window(self, lines: list[str], line_index: int) -> str:
        start = max(0, line_index - 3)
        end = min(len(lines), line_index + 8)
        return "\n".join(lines[start:end])
