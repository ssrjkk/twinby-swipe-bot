import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from core.adapters_registry import GLOBAL_DATING_REGISTRY

logger = logging.getLogger(__name__)


@dataclass
class FaceCheckVerdict:
    passed: bool
    consistency_score: float
    faces_found: int
    reason: str
    skipped: bool = False


def cosine_similarity(vec_a: list[float] | Any, vec_b: list[float] | Any) -> float:
    dot = sum(float(a) * float(b) for a, b in zip(vec_a, vec_b))
    norm_a = sum(float(a) * float(a) for a in vec_a) ** 0.5
    norm_b = sum(float(b) * float(b) for b in vec_b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def evaluate_embeddings(embeddings: list[Any], threshold: float = 0.42) -> FaceCheckVerdict:
    if not embeddings:
        return FaceCheckVerdict(False, 0.0, 0, "Лицо не обнаружено ни на одном фото")
    if len(embeddings) == 1:
        return FaceCheckVerdict(True, 1.0, 1, "Подтверждено 1 фото с лицом")

    anchor = embeddings[0]
    sims = [cosine_similarity(anchor, emb) for emb in embeddings[1:]]
    avg_sim = round(sum(sims) / len(sims), 3)
    min_sim = min(sims)

    if min_sim < threshold:
        return FaceCheckVerdict(
            passed=False,
            consistency_score=avg_sim,
            faces_found=len(embeddings),
            reason=f"Подозрение на фейк: разные люди на фото (min sim={min_sim:.2f} < {threshold:.2f})",
        )
    return FaceCheckVerdict(
        passed=True,
        consistency_score=avg_sim,
        faces_found=len(embeddings),
        reason=f"Один человек подтверждён на {len(embeddings)} фото (avg sim={avg_sim:.2f})",
    )


class FaceConsistencyChecker:
    """Опциональный локальный антифейк-модуль на InsightFace (buffalo_l).
    При отсутствии [vision-local] мягко пропускает проверку (fail-open).
    Для Pure/Feeld/Vox уважает requires_face_on_primary_photo=False.
    """

    def __init__(self, enabled: bool = False, similarity_threshold: float = 0.42) -> None:
        self.enabled = enabled
        self.similarity_threshold = similarity_threshold
        self._face_app: Any = None
        self._unavailable = False

    def _init_model_sync(self) -> bool:
        if self._face_app is not None:
            return True
        if self._unavailable:
            return False
        try:
            from insightface.app import FaceAnalysis

            app = FaceAnalysis(name="buffalo_l", providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
            app.prepare(ctx_id=0, det_size=(640, 640))
            self._face_app = app
            return True
        except Exception as exc:
            logger.info("InsightFace недоступен (%s) — локальная сверка лиц отключена.", exc)
            self._unavailable = True
            return False

    def _verify_sync(self, images_bytes: list[bytes], require_primary_face: bool) -> FaceCheckVerdict:
        if not self._init_model_sync():
            return FaceCheckVerdict(True, 1.0, 0, "InsightFace не установлен (пропуск)", skipped=True)

        import cv2
        import numpy as np

        embeddings: list[Any] = []
        for idx, raw in enumerate(images_bytes):
            arr = np.frombuffer(raw, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                continue
            faces = self._face_app.get(img)
            if not faces:
                if idx == 0 and require_primary_face:
                    return FaceCheckVerdict(False, 0.0, 0, "На главном фото не найдено лицо")
                continue
            main_face = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
            embeddings.append(main_face.normed_embedding)

        if not embeddings and not require_primary_face:
            return FaceCheckVerdict(True, 1.0, 0, "Платформа допускает анкеты без лица (Soft Face-Gate)", skipped=True)

        return evaluate_embeddings(embeddings, threshold=self.similarity_threshold)

    async def verify_images(self, images_bytes: list[bytes], platform: str) -> FaceCheckVerdict:
        if not self.enabled or not images_bytes:
            return FaceCheckVerdict(True, 1.0, 0, "Локальный FaceCheck отключён", skipped=True)

        spec = GLOBAL_DATING_REGISTRY.get(platform.lower())
        require_primary = spec.requires_face_on_primary_photo if spec else True
        return await asyncio.to_thread(self._verify_sync, images_bytes, require_primary)
