"""ЄДРСР: HTTP 200 не є доказом того, що прийшло рішення (T432, FR-661).

Три речі, які виглядають як успіх і успіхом не є, і всі три мають давати
відмову, а не текст:

* сторінка захисту від автоматичного доступу з кодом 200;
* сторінка входу або помилки замість документа;
* формально повний текст, який рішенням не є.

Конституція, принцип III: впізнається текст, а не код відповіді.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from registries import court_registry  # noqa: E402
from core.contracts import SourceAdapterError  # noqa: E402

DECISION_HTML = (
    "<html><body><h1>УХВАЛА</h1>"
    "<p>Господарський суд міста Києва у справі № 910/12/26 постановив таке. "
    "Розглянувши матеріали справи, суд встановив обставини, що мають значення "
    "для вирішення спору, та дійшов висновку про наявність підстав для "
    "задоволення заяви. Ця ухвала набирає законної сили з моменту її "
    "проголошення і може бути оскаржена в апеляційному порядку. "
    "Документ у реєстрі має номер 123456789.</p>"
    "</body></html>"
)

ANTIBOT_HTML = (
    "<html><body>Just a moment... Checking your browser before accessing "
    "reyestr.court.gov.ua. Enable JavaScript and cookies to continue. "
    "Ця перевірка потрібна, щоб підтвердити, що ви не робот, і триватиме "
    "кілька секунд. Будь ласка, зачекайте, сторінку буде перезавантажено "
    "автоматично після завершення перевірки безпеки з'єднання.</body></html>"
)

LOGIN_HTML = (
    "<html><body><h1>Вхід до системи</h1>"
    "<p>Access denied. Для перегляду документа потрібна авторизація. "
    "Введіть логін і пароль. Якщо у вас немає облікового запису, зверніться "
    "до адміністратора реєстру для його отримання у встановленому порядку."
    "</p></body></html>"
)

WRONG_PAGE_HTML = (
    "<html><body><h1>Про реєстр</h1>"
    "<p>Цей ресурс призначений для оприлюднення інформації. Тут ви знайдете "
    "відомості про роботу системи, контакти технічної підтримки, графік "
    "оновлення даних та інші загальні відомості, які можуть бути корисними "
    "відвідувачам ресурсу під час користування ним у повсякденній роботі."
    "</p></body></html>"
)


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None


class _Session:
    """Сесія, що завжди відповідає 200 і заданим тілом."""

    def __init__(self, html: str) -> None:
        self._html = html
        self.calls = 0

    def get(self, *args: Any, **kwargs: Any) -> _Response:
        self.calls += 1
        return _Response(self._html)

    def post(self, *args: Any, **kwargs: Any) -> _Response:
        self.calls += 1
        return _Response(self._html)


def _registry(tmp_path: Path, html: str) -> court_registry.CourtDecisionsRegistry:
    return court_registry.CourtDecisionsRegistry(tmp_path, session=_Session(html))


def test_a_real_decision_is_read_with_provenance(tmp_path: Path) -> None:
    answer = _registry(tmp_path, DECISION_HTML).get_decision("123456789")

    assert "910/12/26" in answer["text"]
    assert answer["language"] == "uk", "мова не заповнена"
    assert answer["publisher"], "видавець не названий"
    assert answer["source_channel"], "канал отримання не названий"
    assert answer["retrieved_at"], "час читання не заповнений"


def test_an_antibot_page_with_status_200_is_a_refusal(tmp_path: Path) -> None:
    with pytest.raises(SourceAdapterError, match="antibot"):
        _registry(tmp_path, ANTIBOT_HTML).get_decision("123456789")


def test_a_login_page_is_a_refusal_not_a_text(tmp_path: Path) -> None:
    with pytest.raises(SourceAdapterError):
        _registry(tmp_path, LOGIN_HTML).get_decision("123456789")


def test_a_page_that_is_not_a_decision_is_a_refusal(tmp_path: Path) -> None:
    """Найгірший випадок: формально документ, але інший."""
    with pytest.raises(SourceAdapterError, match="identity"):
        _registry(tmp_path, WRONG_PAGE_HTML).get_decision("123456789")


def test_a_refused_page_is_never_cached(tmp_path: Path) -> None:
    """Відмова не залишає по собі «прочитаного» тексту."""
    registry = _registry(tmp_path, ANTIBOT_HTML)

    with pytest.raises(SourceAdapterError):
        registry.get_decision("123456789")

    assert not list(tmp_path.glob("*.json")), "заглушка потрапила в кеш як рішення"


def test_an_invalid_identifier_is_refused_before_the_network(tmp_path: Path) -> None:
    session = _Session(DECISION_HTML)
    registry = court_registry.CourtDecisionsRegistry(tmp_path, session=session)

    with pytest.raises(ValueError):
        registry.get_decision("не-число")

    assert session.calls == 0, "невалідний ідентифікатор дійшов до мережі"


def test_the_identity_check_does_not_reject_a_decision_without_its_own_id() -> None:
    """Перевірка слабка в один бік навмисно: вона відкидає не-рішення.

    Текст рішення не зобов'язаний містити свій реєстровий номер, і вимагати
    цього означало б відмовляти на справжніх документах.
    """
    body = "Верховний Суд у складі колегії суддів розглянув справу та ухвалив постанову."

    assert court_registry._identity_mismatch(body, "999999999") == ""
