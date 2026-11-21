"""Одна точка правки окружения для тестов, которым нужен честный loopback.

Системный прокси машины (HTTP(S)_PROXY + NO_PROXY со списком вида «[::1]») читается httpx при
создании клиента: брикетный IPv6-элемент он разбирает как порт и падает с `Invalid port: ':1]'`,
а заодно весь трафик живых моков уходил бы через чужой прокси с учётными данными.
Тесты говорят с 127.0.0.1 намеренно и проксируют только то, что задали сами.
"""
import os

PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def hermetic_proxy() -> None:
    for name in PROXY_VARS:
        os.environ.pop(name, None)
    os.environ["NO_PROXY"] = "*"
