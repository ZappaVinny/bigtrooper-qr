"""Exceptions raised by bigtrooper_qr."""

from __future__ import annotations


class QRModelError(ValueError):
    """Raised when a QR model cannot be generated from the given options.

    Covers invalid option combinations and content that does not fit, such as
    back text that is too deep for the base or too long for the plate.
    """
