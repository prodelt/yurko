"""Adapters for the sources behind each legal order.

The package holds one module per family of official sources. Everything they have
in common — the source-policy declaration, the obligation to answer with a typed
failure instead of raising, the obligation to carry a provenance envelope — lives
in :mod:`sources.base`; the shared "is this a document or a stub?" check lives in
:mod:`sources.stub_detection`.

This module is only the registry. It is deliberately empty at import time: an
adapter appears here when its module is imported and calls :func:`register_adapter`,
so a source that fails to import cannot take the server down with it (principle IX).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, sources.base imports us not
    from sources.base import RegistryAdapter

__all__ = [
    "register_adapter",
    "get_adapter",
    "adapters_for",
    "registered_adapters",
    "clear_adapters",
    "load_adapters",
]

#: Модули адаптеров, каждый из которых регистрирует себя при импорте.
#: Идентификатор адаптера обязан совпадать с ``Source.id`` в реестре покрытия
#: (``legal_orders.py``): маршрутизация ищет адаптер именно по нему, и два имени
#: для одной сущности означают, что карта покрытия обещает подключённый источник,
#: которого не найти.
_ADAPTER_MODULES = (
    "sources.eu_law",
    "sources.eu_case_law",
    "sources.echr",
    "sources.icj",
)

_ADAPTERS: dict[str, Any] = {}


def register_adapter(adapter: "RegistryAdapter") -> "RegistryAdapter":
    """Add an adapter to the registry, keyed by its ``source_id``.

    Idempotent by construction: re-registering the same id replaces the entry
    rather than accumulating duplicates, so a module imported twice does not
    double the registry (principle IX).
    """
    source_id = str(getattr(adapter, "source_id", "") or "").strip()
    if not source_id:
        raise ValueError("adapter must declare a non-empty source_id")
    _ADAPTERS[source_id] = adapter
    return adapter


def get_adapter(source_id: str) -> "RegistryAdapter | None":
    """Return the adapter registered under ``source_id``, or ``None``."""
    return _ADAPTERS.get(str(source_id or "").strip())


def adapters_for(legal_order: str) -> tuple["RegistryAdapter", ...]:
    """Return every registered adapter that serves ``legal_order``."""
    wanted = str(legal_order or "").strip().upper()
    return tuple(
        adapter
        for adapter in _ADAPTERS.values()
        if str(getattr(adapter, "legal_order", "")).upper() == wanted
    )


def registered_adapters() -> tuple[str, ...]:
    """Return the ids of every registered adapter, sorted."""
    return tuple(sorted(_ADAPTERS))


def clear_adapters() -> None:
    """Empty the registry. For tests that need a known starting point."""
    _ADAPTERS.clear()


def load_adapters() -> tuple[str, ...]:
    """Импортировать модули адаптеров, чтобы они себя зарегистрировали.

    Вызывается явно, а не при импорте пакета: реестр остаётся пустым, пока о нём
    не спросят, и тест может собрать своё окружение с нуля.

    Сбой одного источника не отменяет остальные (принцип IX): модуль, который не
    импортируется, пропускается с записью в журнал, а не роняет сервер. Молча это
    не проходит — источник останется без адаптера, и маршрутизация честно скажет,
    что подключённого канала нет, вместо того чтобы притвориться работающей.

    Возвращает идентификаторы адаптеров, зарегистрированных после загрузки.
    """
    import importlib
    import logging

    logger = logging.getLogger(__name__)
    for module_name in _ADAPTER_MODULES:
        try:
            importlib.import_module(module_name)
        except Exception as error:  # noqa: BLE001 — падение источника не роняет сервер
            logger.warning(
                "адаптер %s не загружен: %s: %s",
                module_name,
                type(error).__name__,
                error,
            )
    return registered_adapters()
