import base64
import logging
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel

from bigtrooper_qr import QRModelError, QRType, generate_qr_model

logger = logging.getLogger(__name__)

PET_BASE: dict[str, Any] = {
    "base_width": 28,
    "base_height": 28,
    "base_thickness": 1.6,
    "qr_depth": 0.6,
    "mount": "keychain",
    "hole_diameter": 3.5,
    "size": 300,
}

TAG_PRESETS: dict[str, dict[str, dict[str, Any]]] = {
    "pet": {
        "plain": {**PET_BASE, "formats": ("stl",), "back_text": "SCAN ME\nIN LIGHT", "back_text_size": 4},
        "color": {**PET_BASE, "formats": ("3mf",)},
    },
}

FORMAT_OUTPUTS: dict[str, tuple[str, str]] = {
    "stl": ("stl", "model/stl"),
    "3mf": ("three_mf", "model/3mf"),
}

app = FastAPI()
app.add_middleware(GZipMiddleware)


@app.get("/up")
def read_root() -> dict[str, str]:
    return {"status": "ok"}


class Tag(BaseModel):
    link: str
    type: str


@app.post("/")
def create_tag(tag: Tag) -> dict[str, Any]:
    """Generate every model variant for a tag.

    Plain ``def`` so FastAPI runs the CPU-bound generation in its threadpool
    instead of blocking the event loop.

    Args:
        tag: The link to encode and the tag type, which picks the presets.

    Returns:
        The base64-encoded models, one per variant, and any warnings.

    Raises:
        HTTPException: 422 for an unknown tag type or options the library rejects.
    """
    variants = TAG_PRESETS.get(tag.type)
    if variants is None:
        raise HTTPException(status_code=422, detail=f"unknown tag type: {tag.type}")

    models = []
    warnings = []
    for variant, options in variants.items():
        try:
            result = generate_qr_model(data=tag.link, qr_type=QRType.URL, **options)
        except QRModelError as e:
            raise HTTPException(status_code=422, detail=f"{variant}: {e}") from e

        extension = options["formats"][0]
        attribute, content_type = FORMAT_OUTPUTS[extension]
        data = getattr(result, attribute)
        models.append(
            {
                "variant": variant,
                "content_type": content_type,
                "extension": extension,
                "data": base64.b64encode(data).decode(),
            }
        )
        warnings.extend(f"{variant}: {warning}" for warning in result.warnings)
        logger.info("tag %s: generated %s %s, %d bytes", tag.link, tag.type, variant, len(data))

    for warning in warnings:
        logger.warning("tag %s: %s", tag.link, warning)

    return {"models": models, "warnings": warnings}