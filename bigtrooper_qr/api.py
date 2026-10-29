"""Public API: generate QR code models and images in memory.

Example:
    >>> from bigtrooper_qr import generate_qr_model, QRType
    >>> result = generate_qr_model(data="https://example.com", qr_type=QRType.URL, formats=("stl", "png"))
    >>> stl_bytes, png_bytes = result.stl, result.png
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import qrcode
from PIL import Image, ImageColor
from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, field_validator
from qrcode.exceptions import DataOverflowError

from .errors import QRModelError
from .qr_generator import (
    ErrorCorrectionLevel,
    QRType,
    add_center_text_to_qr,
    add_frame_to_qr,
    add_label_to_qr,
    add_overlay_to_qr,
    crop_qr_border,
    generate_qr_code,
    generate_qr_svg,
    qr_image_to_png_bytes,
)
from .stl_converter import convert_qr_to_3mf, convert_qr_to_stl

logger = logging.getLogger(__name__)

OutputFormat = Literal["stl", "3mf", "png", "svg"]
MODEL_FORMATS: frozenset[str] = frozenset({"stl", "3mf"})
_DATA_TOO_LONG = "Data is too long to fit in a QR code at this error correction level"


class QRModelOptions(BaseModel):
    """All options for generating a QR code model.

    Ranges are enforced on construction; out-of-range or unknown fields raise
    ``pydantic.ValidationError``. See LIBRARY.md for a full description of each option.
    """

    model_config = ConfigDict(extra="forbid")

    # QR content
    data: str = Field(min_length=1, description="Data to encode in the QR code")
    qr_type: QRType = Field(default=QRType.TEXT, description="How to format the data (text, url, wifi, ...)")
    error_correction: ErrorCorrectionLevel = Field(
        default=ErrorCorrectionLevel.LOW, description="QR error correction level"
    )
    size: int = Field(
        default=200, ge=100, le=1000, description="QR image resolution in pixels; also the mesh resolution"
    )
    border_crop: int = Field(
        default=15, ge=0, le=20, description="Pixels cropped from each side of the QR image border"
    )

    # Type-specific QR fields
    wifi_password: str | None = Field(default=None, description="WiFi password (qr_type=wifi)")
    wifi_security: Literal["WPA", "WEP", "nopass"] = Field(default="WPA", description="WiFi security (qr_type=wifi)")
    email_subject: str | None = Field(default=None, description="Email subject (qr_type=email)")
    email_body: str | None = Field(default=None, description="Email body (qr_type=email)")
    sms_message: str | None = Field(default=None, description="SMS message text (qr_type=sms)")
    contact_phone: str | None = Field(default=None, description="Contact phone (qr_type=contact)")
    contact_email: str | None = Field(default=None, description="Contact email (qr_type=contact)")
    contact_org: str | None = Field(default=None, description="Contact organization (qr_type=contact)")

    # Outputs
    formats: tuple[OutputFormat, ...] = Field(default=("stl",), min_length=1, description="Outputs to generate")

    # 3D dimensions
    base_width: float = Field(default=50.0, ge=10.0, le=500.0, description="Base width in mm")
    base_height: float = Field(default=50.0, ge=10.0, le=500.0, description="Base height in mm")
    base_thickness: float = Field(default=2.0, ge=0.5, le=20.0, description="Solid base plate thickness in mm")
    qr_depth: float = Field(default=1.0, ge=0.5, le=20.0, description="Height of QR modules above the base in mm")
    corner_radius: float = Field(
        default=0.0, ge=0.0, le=250.0, description="Radius of the base plate's rounded corners in mm (0 for square)"
    )
    invert: bool = Field(default=False, description="Raise the white areas instead of the black modules")
    multi_layer: bool = Field(default=False, description="STL only: use distinct layer heights for base, QR and frame")
    layer_heights: list[PositiveFloat] | None = Field(
        default=None,
        min_length=2,
        max_length=3,
        description="Layer heights [base, qr, frame?] in mm (multi_layer only)",
    )

    # Keychain tab
    mount: Literal["keychain", "arch"] | None = Field(
        default=None, description="Add a mount at the top: a small keychain tab, or a full-width arch"
    )
    hole_diameter: float = Field(default=4.0, ge=1.0, le=10.0, description="Keychain hole diameter in mm")
    hole_band: float = Field(
        default=2.5, ge=1.0, le=6.0, description="Width of the plastic ring around the keychain hole in mm"
    )

    # Arch mount
    arch_band: float = Field(default=4.0, ge=1.0, le=10.0, description="Width of the arch band in mm")
    arch_height: float = Field(
        default=12.0, ge=4.0, le=100.0, description="Total arch height above the plate edge in mm, padding included"
    )
    arch_padding: float = Field(
        default=2.0, ge=0.5, le=20.0, description="Solid strip between the plate edge and the arch opening in mm"
    )

    # Back text (STL only)
    back_text: str | None = Field(
        default=None, description="Text recessed (mirrored) into the back; '\\n' separates lines"
    )
    back_text_size: float = Field(default=4.0, ge=2.0, le=30.0, description="Capital letter height of back text in mm")
    back_text_depth: float = Field(default=0.6, ge=0.2, le=5.0, description="Depth of the back text recess in mm")

    # Colors
    base_color: str = Field(default="white", description="Background color (name or hex)")
    qr_color: str = Field(default="black", description="QR module color (name or hex)")
    separate_components: bool = Field(default=False, description="3MF only: separate objects per color")

    # Front label
    label: str | None = Field(default=None, description="Text label printed above or below the QR code")
    label_position: Literal["top", "bottom"] = Field(default="top", description="Label position")
    label_threshold: int = Field(default=128, ge=0, le=255, description="Label binarization threshold")

    # Center text / emoji
    center_text: str | None = Field(default=None, description="Text or emoji in the center of the QR code")
    center_text_size: int = Field(default=24, ge=8, le=100, description="Center text font size in pixels")
    center_text_area: int = Field(default=20, ge=10, le=30, description="Center text area as % of the QR code")
    center_text_color: str = Field(default="black", description="Center text color")
    center_text_bg: str = Field(default="white", description="Center text background color")
    center_text_threshold: int | None = Field(
        default=128,
        ge=0,
        le=255,
        description="Center text binarization threshold for 3D output (None keeps gray levels)",
    )

    # Overlay image
    overlay_image: bytes | None = Field(default=None, description="Encoded image (PNG, JPEG, ...) placed in the center")
    overlay_size_percent: int = Field(default=20, ge=10, le=30, description="Overlay size as % of the QR code")

    # Frame
    frame_style: Literal["square", "rounded", "hexagon", "octagon"] | None = Field(
        default=None, description="Frame shape"
    )
    frame_width: int = Field(default=10, ge=5, le=50, description="Frame width in pixels")
    frame_color: str = Field(default="black", description="Frame color")

    # Module style
    module_style: Literal["square", "circle", "dot", "rounded"] = Field(default="square", description="QR module shape")
    module_size_ratio: float = Field(default=0.8, ge=0.5, le=1.0, description="Styled module size relative to the grid")

    @field_validator("base_color", "qr_color", "center_text_color", "center_text_bg", "frame_color")
    @classmethod
    def _check_color(cls, value: str) -> str:
        """Reject color strings PIL cannot parse, so typos fail before any work is done.

        Args:
            value: Color name or hex code

        Returns:
            The unchanged color string

        Raises:
            ValueError: If the color is not recognized
        """
        try:
            ImageColor.getrgb(value)
        except ValueError as e:
            raise ValueError(f"unknown color {value!r}; use a CSS color name or hex code like '#1a2b3c'") from e
        return value


@dataclass
class QRModelResult:
    """Generated outputs. Each output is None unless it was requested in ``formats``.

    Attributes:
        stl: Binary STL file contents
        three_mf: 3MF file contents
        png: PNG image of the final QR code (with label, frame, etc.)
        svg: SVG markup of the QR code (modules only, no label/frame/overlay)
        warnings: Non-fatal messages, such as back text being shrunk to fit
    """

    stl: bytes | None = None
    three_mf: bytes | None = None
    png: bytes | None = None
    svg: str | None = None
    warnings: list[str] = field(default_factory=list)


def generate_qr_model(options: QRModelOptions | None = None, /, **kwargs: Any) -> QRModelResult:
    """Generate a QR code model and/or images in memory.

    Pass either a ``QRModelOptions`` instance or the same fields as keyword arguments.

    Args:
        options: Pre-built options. Mutually exclusive with keyword arguments.
        **kwargs: Fields of ``QRModelOptions``, used when ``options`` is not given.

    Returns:
        QRModelResult with the requested outputs filled in

    Raises:
        pydantic.ValidationError: If a keyword argument is unknown or out of range
        QRModelError: If the options conflict or the content cannot be generated
        TypeError: If both ``options`` and keyword arguments are given
    """
    if options is None:
        options = QRModelOptions(**kwargs)
    elif kwargs:
        raise TypeError("Pass either a QRModelOptions instance or keyword arguments, not both")
    o = options

    formats = set(o.formats)
    wants_model = bool(formats & MODEL_FORMATS)

    if o.back_text and "3mf" in formats:
        raise QRModelError("back_text is only supported for STL output; remove '3mf' from formats")
    if o.center_text and o.overlay_image:
        raise QRModelError("center_text and overlay_image cannot be used together")
    if o.corner_radius > min(o.base_width, o.base_height) / 2:
        raise QRModelError(
            f"corner_radius {o.corner_radius}mm is too large; use at most half the shorter side "
            f"({min(o.base_width, o.base_height) / 2}mm)"
        )
    tab_width = o.hole_diameter + 2 * o.hole_band
    if o.mount == "keychain" and tab_width > o.base_width - 2 * o.corner_radius:
        raise QRModelError(
            f"The {tab_width}mm keychain tab doesn't fit on the straight top edge; "
            f"use a corner_radius of at most {(o.base_width - tab_width) / 2}mm or a smaller hole"
        )
    if o.mount == "arch":
        # The opening's edge is the arch offset inward by the band, which only stays a
        # smooth curve while the band is thinner than the arch's tightest bend
        curve_height = o.arch_height - o.arch_padding
        half_width = o.base_width / 2
        tightest = min(half_width**2 / curve_height, curve_height**2 / half_width) if curve_height > 0 else 0.0
        if o.arch_band >= tightest:
            raise QRModelError(
                f"arch_band {o.arch_band}mm is too thick for a {o.arch_height}mm arch with {o.arch_padding}mm "
                f"padding on a {o.base_width}mm plate; use a thinner band or a taller arch"
            )

    format_kwargs: dict[str, Any] = _format_kwargs(o)
    try:
        generated = generate_qr_code(
            data=o.data,
            qr_type=o.qr_type,
            size=o.size,
            error_correction=o.error_correction,
            base_color=o.base_color,
            qr_color=o.qr_color,
            module_style=o.module_style,
            module_size_ratio=o.module_size_ratio,
            return_qr_object=True,
            **format_kwargs,
        )
    except DataOverflowError as e:
        raise QRModelError(_DATA_TOO_LONG) from e
    except ValueError as e:
        # qrcode reports data that exceeds the largest QR version as an invalid version
        if "Invalid version" in str(e):
            raise QRModelError(_DATA_TOO_LONG) from e
        raise
    qr_image, qr_object = cast(tuple[Image.Image, qrcode.QRCode], generated)

    if o.border_crop > 0:
        qr_image = crop_qr_border(qr_image, o.border_crop)

    if o.label:
        qr_image = add_label_to_qr(qr_image, o.label, o.label_position, threshold=o.label_threshold)

    # Center content is flattened to grayscale whenever a 3D model is requested
    if o.center_text:
        qr_image = add_center_text_to_qr(
            qr_image,
            o.center_text,
            font_size=o.center_text_size,
            size_percent=o.center_text_area,
            text_color=o.center_text_color,
            bg_color=o.center_text_bg,
            convert_to_grayscale=wants_model,
            threshold=o.center_text_threshold if wants_model else None,
        )

    if o.overlay_image:
        qr_image = add_overlay_to_qr(
            qr_image, o.overlay_image, o.overlay_size_percent, convert_to_grayscale=wants_model
        )

    if o.frame_style:
        qr_image = add_frame_to_qr(qr_image, o.frame_style, o.frame_width, o.frame_color)

    layer_heights: list[float] | None = None
    if o.multi_layer:
        layer_heights = list(o.layer_heights) if o.layer_heights else [o.base_thickness, o.base_thickness + o.qr_depth]
        if not o.layer_heights and o.frame_style:
            layer_heights.append(o.base_thickness + o.qr_depth + 2.0)

    result = QRModelResult()

    if "png" in formats:
        result.png = qr_image_to_png_bytes(qr_image)

    if "svg" in formats:
        result.svg = generate_qr_svg(qr_object, o.base_color, o.qr_color, o.module_style, o.module_size_ratio)

    if "stl" in formats:
        result.stl = convert_qr_to_stl(
            qr_image=qr_image,
            base_size_mm=(o.base_width, o.base_height),
            base_height_mm=o.base_thickness,
            qr_height_mm=o.qr_depth,
            invert=o.invert,
            multi_layer=o.multi_layer,
            layer_heights=layer_heights,
            has_frame=o.frame_style is not None,
            mount_type=o.mount,
            hole_diameter=o.hole_diameter,
            hole_band=o.hole_band,
            arch_band=o.arch_band,
            arch_height=o.arch_height,
            arch_padding=o.arch_padding,
            corner_radius=o.corner_radius,
            back_text=o.back_text,
            back_text_size_mm=o.back_text_size,
            back_text_depth_mm=o.back_text_depth,
            warnings=result.warnings,
        )

    if "3mf" in formats:
        result.three_mf = convert_qr_to_3mf(
            qr_image=qr_image,
            base_size_mm=(o.base_width, o.base_height),
            base_height_mm=o.base_thickness,
            qr_height_mm=o.qr_depth,
            invert=o.invert,
            base_color=o.base_color,
            qr_color=o.qr_color,
            separate_components=o.separate_components,
            mount_type=o.mount,
            hole_diameter=o.hole_diameter,
            hole_band=o.hole_band,
            arch_band=o.arch_band,
            arch_height=o.arch_height,
            arch_padding=o.arch_padding,
            corner_radius=o.corner_radius,
        )

    for warning in result.warnings:
        logger.warning(warning)

    return result


def _format_kwargs(o: QRModelOptions) -> dict[str, str]:
    """Collect the type-specific fields that ``format_qr_data`` expects for the chosen QR type.

    Args:
        o: Generation options

    Returns:
        Keyword arguments for the QR data formatter
    """
    if o.qr_type == QRType.WIFI:
        kwargs = {"security": o.wifi_security}
        if o.wifi_password:
            kwargs["password"] = o.wifi_password
        return kwargs
    candidates: dict[QRType, dict[str, str | None]] = {
        QRType.EMAIL: {"subject": o.email_subject, "body": o.email_body},
        QRType.SMS: {"message": o.sms_message},
        QRType.CONTACT: {"phone": o.contact_phone, "email": o.contact_email, "org": o.contact_org},
    }
    return {key: value for key, value in candidates.get(o.qr_type, {}).items() if value}
