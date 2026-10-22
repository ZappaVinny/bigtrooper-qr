"""BigTrooper QR: generate 3D printable QR code tags (STL, 3MF) and images (PNG, SVG) in memory."""

from __future__ import annotations

import logging

from .api import QRModelOptions, QRModelResult, generate_qr_model
from .errors import QRModelError
from .qr_generator import ErrorCorrectionLevel, QRType

__version__ = "0.3.0"

__all__ = [
    "ErrorCorrectionLevel",
    "QRModelError",
    "QRModelOptions",
    "QRModelResult",
    "QRType",
    "__version__",
    "generate_qr_model",
]

# Libraries should not configure logging; applications attach their own handlers
logging.getLogger(__name__).addHandler(logging.NullHandler())
