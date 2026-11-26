"""Uploaded photos on local disk (MEDIA_DIR). Keys are random, never derived from user input."""

import secrets
from pathlib import Path

from app.core.config import get_settings

MAX_BYTES = 5 * 1024 * 1024
TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


class InvalidUpload(ValueError):
    pass


def root() -> Path:
    root = Path(get_settings().media_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def save(folder: str, data: bytes, content_type: str | None) -> str:
    ext = TYPES.get((content_type or "").lower())
    if ext is None:
        raise InvalidUpload("Format non accepté : JPEG, PNG ou WebP.")
    if not data:
        raise InvalidUpload("Fichier vide.")
    if len(data) > MAX_BYTES:
        raise InvalidUpload("Photo trop lourde (5 Mo maximum).")
    key = f"{folder}/{secrets.token_urlsafe(18)}.{ext}"
    path = root() / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return key


def path_of(key: str) -> Path | None:
    base = root().resolve()
    p = (base / key).resolve()
    if base not in p.parents or not p.is_file():
        return None
    return p


def delete(key: str | None) -> None:
    if key and (p := path_of(key)):
        p.unlink(missing_ok=True)


def media_type(key: str) -> str:
    ext = key.rsplit(".", 1)[-1]
    return next((t for t, e in TYPES.items() if e == ext), "application/octet-stream")
