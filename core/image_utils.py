import io
import logging
import random

logger = logging.getLogger(__name__)


def is_blank_or_secure_screen(image_bytes: bytes, std_threshold: float = 3.0) -> bool:
    """Определяет чёрный/пустой экран (например Android FLAG_SECURE в Pure/банках),
    чтобы не сжигать токены VLM и сразу выдать понятную диагностику.
    """
    if not image_bytes or len(image_bytes) < 100:
        return True
    try:
        from PIL import Image, ImageStat

        with Image.open(io.BytesIO(image_bytes)) as img:
            gray = img.convert("L")
            stat = ImageStat.Stat(gray)
            stddev = float(stat.stddev[0]) if stat.stddev else 0.0
            return stddev < std_threshold
    except ImportError:
        return False
    except Exception as exc:
        logger.debug("Не удалось проверить яркость скриншота: %s", exc)
        return False


def prepare_image_for_vlm(image_bytes: bytes, max_side: int = 1024, quality: int = 82) -> bytes:
    """Сжимает тяжёлые мобильные PNG-скриншоты (2-5 МБ) до компактного JPEG (~80-150 КБ)
    для быстрого и дешёвого анализа VLM без потери читаемости текста анкеты.
    """
    if not image_bytes:
        return image_bytes
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as img:
            rgb = img.convert("RGB")
            w, h = rgb.size
            longest = max(w, h)
            if longest > max_side:
                scale = max_side / float(longest)
                rgb = rgb.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=quality, optimize=True)
            return buf.getvalue()
    except ImportError:
        return image_bytes
    except Exception as exc:
        logger.debug("Сжатие скриншота пропущено: %s", exc)
        return image_bytes


def denormalize_point(
    norm_xy: tuple[int, int] | list[int],
    screen_w: int,
    screen_h: int,
    jitter_px: int = 4,
) -> tuple[int, int]:
    """Переводит нормализованные координаты VLM [0..1000, 0..1000] в пиксели экрана с безопасным клампингом."""
    nx = max(0, min(1000, int(norm_xy[0])))
    ny = max(0, min(1000, int(norm_xy[1])))
    px = int(round((nx / 1000.0) * screen_w))
    py = int(round((ny / 1000.0) * screen_h))
    if jitter_px > 0:
        px += random.randint(-jitter_px, jitter_px)
        py += random.randint(-jitter_px, jitter_px)
    px = max(2, min(screen_w - 2, px))
    py = max(2, min(screen_h - 2, py))
    return px, py
