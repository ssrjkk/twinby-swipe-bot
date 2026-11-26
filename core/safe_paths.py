from pathlib import Path


def resolve_within(base: str | Path, candidate: str | Path) -> Path:
    base_path = Path(base).resolve()
    path = Path(candidate)
    resolved = (path if path.is_absolute() else base_path / path).resolve()
    try:
        resolved.relative_to(base_path)
    except ValueError as exc:
        raise ValueError("Путь выходит за пределы разрешённого каталога") from exc
    return resolved
