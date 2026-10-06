"""Конфігурація прода — юридичне середовище, і це перевіряється, а не заявляється (T476).

Рішення власника 11.09.2026: прод несе повний набір інструментів МСП, щоб юрист
працював з міжнародним правом повноцінно. До того `render.yaml` описував
інженерний стенд — і найгірше в ньому було не те, що він інженерний, а те, що
**в юридичному профілі він не стартував узагалі**: hosted-ембединги й хостована
база дають `ProfileViolation` при старті. Прод піднімався лише з
`YURKO_PROFILE=engineering`, і тоді `list_coverage` чесно віддавав
`legal_acceptance: false`.

Тут перевіряється саме файл конфігурації, а не запущений сервер: помилку в
ньому видно тільки на деплої, а деплой — найдорожче місце, щоб її знайти.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

RENDER = ROOT / "render.yaml"


def _env_of_render() -> dict[str, str]:
    """Пари ключ-значення з `envVars`, без тих, що ставляться в дашборді.

    Повний парсер YAML тут не потрібен і не дозволений планом: формат файла
    плаский, а залежність заради читання шести рядків коштувала б дорожче за
    користь.
    """
    text = RENDER.read_text(encoding="utf-8")
    pairs: dict[str, str] = {}
    key: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- key:"):
            key = stripped.split(":", 1)[1].strip()
            continue
        if key and stripped.startswith("value:"):
            value = stripped.split(":", 1)[1].strip().strip('"')
            pairs[key] = value
            key = None
        elif key and stripped.startswith(("sync:", "generateValue:")):
            key = None
    return pairs


def test_the_production_config_starts_under_the_legal_profile() -> None:
    """Головна перевірка: це оточення не дає жодного порушення профілю.

    Якби дало — сервер не піднявся б, і дізналися б ми про це з деплою.
    """
    from core import yurko_profile

    env = _env_of_render()
    # DATABASE_URL ставиться в дашборді; підставляємо реальну форму хоста, щоб
    # перевірка бачила саме хостовану базу, а не її відсутність.
    env.setdefault(
        "DATABASE_URL",
        "postgresql://postgres.ref:pw@aws-0-eu-west-1.pooler.supabase.com:6543/postgres",
    )

    violations = yurko_profile.validate_environment(env)

    assert violations == [], f"прод не стартує в профілі legal: {violations}"


def test_the_production_profile_is_legal_and_declared() -> None:
    """Профіль названий явно: заява про середовище не робиться замовчуванням."""
    env = _env_of_render()

    assert env.get("YURKO_PROFILE") == "legal"


def test_hosted_core_declares_it_has_no_output_directory() -> None:
    """Каталог сервера сховищем справи не є (contracts/core.md §2, D5).

    Без `YURKO_HOSTED=1` розміщене ядро писало б у робочий каталог власного
    процесу — тиха підміна згоди юриста на обробку матеріалів.
    """
    env = _env_of_render()

    assert env.get("YURKO_HOSTED") == "1"


def test_the_owner_declaration_for_the_database_is_present() -> None:
    """Хостована база в потоці запиту дозволена лише свідченням власника.

    Воно тут є і стоїть поруч із поясненням, що саме в цій базі лежить:
    публічне право. Матеріалів справи в ній немає за конструкцією — ядро їх не
    отримує взагалі (принцип VI).
    """
    env = _env_of_render()

    assert env.get("YURKO_OWN_DATABASE") == "1"


def test_no_hosted_model_key_has_a_value_in_the_server_environment() -> None:
    """Ключ hosted-моделі в оточенні сервера — зайва поверхня, навіть невживана.

    Заборонене саме **непорожнє значення**, а не згадка ключа. Різниця тут
    істотна і працює в інший бік, ніж здається: змінна, якої блупринт не
    називає, лишається в дашборді такою, як була, — тобто мовчання конфігурації
    зберігає старий ключ, а не прибирає його.
    """
    env = _env_of_render()
    text = RENDER.read_text(encoding="utf-8")

    for name in ("GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        assert not env.get(name, "").strip(), f"{name} має непорожнє значення в конфігурації прода"
        assert not re.search(
            rf"^\s*-\s*key:\s*{name}\s*\n\s*sync:\s*false", text, re.MULTILINE
        ), f"{name} оголошений як керований дашбордом — конфігурація його не контролює"


def test_the_google_key_is_declared_empty_to_overwrite_the_dashboard() -> None:
    """Порожнє значення — не забутий рядок, а спосіб затерти ключ у дашборді.

    Сервіс має цей ключ від часів інженерного стенда. Після переходу на профіль
    `legal` непорожній ключ зупиняє старт (`ProfileViolation`) — тобто деплой
    зламав би працюючий прод, а виправити це міг би лише той, у кого є доступ до
    дашборда. Блупринт затирає значення сам, і дія з людини знімається.
    """
    text = RENDER.read_text(encoding="utf-8")

    assert re.search(
        r'^\s*-\s*key:\s*GOOGLE_API_KEY\s*\n\s*value:\s*""\s*$', text, re.MULTILINE
    ), "ключ не оголошений порожнім — старе значення в дашборді переживе деплой"


def test_embeddings_are_explicitly_disabled_not_merely_absent() -> None:
    """`null` сказаний вголос: відсутність ключа й заборона провайдера — різне.

    Без явного `EMBEDDING_PROVIDER` автовизначення вмикає Gemini, щойно в
    оточенні з'явиться `GOOGLE_API_KEY` — тобто конфігурація лишалася б за крок
    від порушення.
    """
    env = _env_of_render()

    assert env.get("EMBEDDING_PROVIDER") == "null"


def test_the_engineering_group_is_not_enabled_in_production() -> None:
    """Вільний текст у зовнішній пошук у юридичному профілі не працює.

    Оголошувати групу, яка не працює, — обіцянка операції, якої не буде.
    """
    env = _env_of_render()

    assert "engineering" not in env.get("YURKO_TOOL_GROUPS", "").split(",")


@pytest.mark.parametrize("code", ["EU", "ECHR", "ICJ", "UA"])
def test_every_declared_legal_order_has_a_wired_adapter(code: str) -> None:
    """Повнота роботи з міжнародним правом — це адаптери, а не намір.

    Прод, у якому правопорядок оголошений, а каналу до нього немає, відповість
    юристові «не покрито» там, де сам обіцяв покриття.
    """
    from core import legal_orders
    import sources

    # `server` імпортується навмисно, а не лише `load_adapters()`: українські
    # читачі оголошені в самому сервері (`server.py`, UaRadaReader і
    # UaCourtReader), а не окремим модулем у `sources/`, тому переліком
    # `_ADAPTER_MODULES` вони не піднімаються. На проді сервер імпортується
    # завжди — і тест має перевіряти те, що буде там, а не те, що дає
    # найкоротший шлях у тесті.
    import server  # noqa: F401

    sources.load_adapters()
    wired = [
        source.id
        for source in legal_orders.sources_for(code)
        if sources.get_adapter(source.id) is not None
    ]

    assert wired, f"{code}: жодного підключеного адаптера"


def test_the_image_carries_the_profile_itself() -> None:
    """Профіль їде разом із кодом, а не лише в змінних сервісу (T479).

    Render синхронізує `envVars` з `render.yaml` тільки при явному застосуванні
    блупринта; звичайний push деплоїть **код**, а змінні сервісу лишає як були.
    Виміряно 11.09.2026: перший деплой юридичного профілю впав саме через це —
    новий код зустрів старе оточення інженерного стенда й не стартував.

    `YURKO_OWN_DATABASE` тут свідомо відсутній: це свідчення власника про
    конкретну базу, і зашити його в образ означало б заявити за власника про
    будь-яку базу, до якої образ колись підключать.
    """
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    env = dict(re.findall(r"^ENV\s+([A-Z_]+)=(.*)$", text, re.MULTILINE))

    assert env.get("YURKO_PROFILE") == "legal"
    assert env.get("YURKO_HOSTED") == "1"
    assert env.get("EMBEDDING_PROVIDER") == "null"
    assert "YURKO_OWN_DATABASE" not in env, "свідчення власника зашите в образ"


def test_every_import_of_the_server_is_in_requirements() -> None:
    """Залежність, якої немає в образі, падає в юриста, а не на збірці.

    `yaml` імпортується ліниво — всередині читача конфігурації мут-корту. Образ
    без нього стартує нормально і ламається на першому ж виклику мут-корту,
    тобто найдорожчим способом із можливих.
    """
    import ast
    import sys as _sys

    source = (ROOT / "server.py").read_text(encoding="utf-8")
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module.split(".")[0])

    local = {path.stem for path in ROOT.glob("*.py")} | {
        path.name for path in ROOT.iterdir() if path.is_dir() and (path / "__init__.py").is_file()
    }
    external = modules - set(_sys.stdlib_module_names) - local

    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    #: Ім'я імпорту й ім'я дистрибутива збігаються не завжди.
    distribution = {"yaml": "pyyaml"}
    missing = [
        name for name in sorted(external) if distribution.get(name, name) not in requirements
    ]

    assert not missing, f"імпортується, але не встановлюється образом: {missing}"
