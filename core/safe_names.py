import os
import re


def safe_filename(value: str | os.PathLike[str]) -> str:
    raw = os.fspath(value)
    if raw != os.path.basename(raw) or "/" in raw or "\\" in raw:
        raise ValueError("Ожидалось только имя файла")
    name = raw.casefold()
    if not re.fullmatch(r"[a-z0-9_-]+", name):
        raise ValueError("Некорректное имя файла")
    return name
