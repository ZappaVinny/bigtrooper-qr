"""Tests for the bigtrooper_qr public API."""

from __future__ import annotations

import io
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError
from stl import mesh

import bigtrooper_qr
from bigtrooper_qr import QRModelError, QRModelOptions, QRType, generate_qr_model
from bigtrooper_qr.qr_generator import crop_qr_border, generate_qr_code
from bigtrooper_qr.stl_converter import generate_keychain_mount_geometry, image_to_3d_array, render_back_text_mask

TEST_IMAGES = Path(__file__).parent.parent / "test_images"

# The BigTrooper collar tag settings used throughout these tests
COLLAR_TAG = {
    "data": "https://8bt/Hs3Sr",
    "qr_type": QRType.URL,
    "base_width": 28,
    "base_height": 28,
    "base_thickness": 1.6,
    "qr_depth": 0.6,
    "mount": "keychain",
    "hole_diameter": 3.5,
}


def load_vectors(stl_bytes: bytes) -> np.ndarray:
    """Parse STL bytes into an (N, 3, 3) array of triangle vertices.

    Args:
        stl_bytes: Binary STL contents

    Returns:
        Triangle vertex array in float64
    """
    return mesh.Mesh.from_file("model.stl", fh=io.BytesIO(stl_bytes)).vectors.astype(np.float64)


def signed_volume(vectors: np.ndarray) -> float:
    """Compute the signed volume enclosed by triangles (positive when normals point outward).

    Args:
        vectors: (N, 3, 3) triangle vertices

    Returns:
        Signed volume in cubic mm
    """
    return float(np.einsum("ij,ij->i", vectors[:, 0], np.cross(vectors[:, 1], vectors[:, 2])).sum() / 6)


def test_package_exports() -> None:
    assert bigtrooper_qr.__version__
    assert callable(bigtrooper_qr.generate_qr_model)


def test_default_call_returns_only_stl() -> None:
    result = generate_qr_model(data="hello")
    assert result.stl is not None
    assert result.three_mf is None and result.png is None and result.svg is None
    vectors = load_vectors(result.stl)
    assert len(vectors) > 0
    mins, maxs = vectors.reshape(-1, 3).min(axis=0), vectors.reshape(-1, 3).max(axis=0)
    assert np.allclose(mins, [0, 0, 0])
    assert np.allclose(maxs, [50, 50, 3.0])  # default 50x50mm, 2mm base + 1mm QR


def test_options_object_and_kwargs_are_equivalent() -> None:
    from_kwargs = generate_qr_model(data="same", base_width=30, base_height=30)
    from_options = generate_qr_model(QRModelOptions(data="same", base_width=30, base_height=30))
    assert from_kwargs.stl == from_options.stl  # output is deterministic


def test_options_and_kwargs_together_rejected() -> None:
    with pytest.raises(TypeError):
        generate_qr_model(QRModelOptions(data="x"), base_width=30)


def test_keychain_tab_extends_model() -> None:
    vectors = load_vectors(generate_qr_model(**COLLAR_TAG).stl or b"")
    maxs = vectors.reshape(-1, 3).max(axis=0)
    assert maxs[0] == pytest.approx(28)
    assert 37 < maxs[1] < 40  # 28mm plate plus a ~10mm rounded tab


def test_all_formats() -> None:
    result = generate_qr_model(data="all formats", formats=("stl", "3mf", "png", "svg"))
    assert result.stl and result.three_mf and result.png and result.svg
    assert result.three_mf[:2] == b"PK"  # 3MF is a zip container
    assert result.svg.startswith("<svg")
    assert Image.open(io.BytesIO(result.png)).format == "PNG"


def test_mesh_is_consistently_oriented() -> None:
    """Signed volume must equal the volume implied by the height map, so every face points outward."""
    options = QRModelOptions(**COLLAR_TAG, back_text="SCAN ME\nIN LIGHT", size=200)
    vectors = load_vectors(generate_qr_model(options).stl or b"")

    generated = generate_qr_code(options.data, options.qr_type, size=options.size)
    assert isinstance(generated, Image.Image)
    image = crop_qr_border(generated, options.border_crop)
    height_map = np.flipud(image_to_3d_array(image, options.base_thickness, options.qr_depth))
    pixel_size = options.base_width / height_map.shape[1]
    text_mask = render_back_text_mask(options.back_text or "", height_map.shape, pixel_size, options.back_text_size)
    bottom_map = np.where(text_mask, options.back_text_depth, 0.0)
    expected = float(((height_map - bottom_map) * pixel_size**2).sum())

    tab_vertices, tab_triangles = generate_keychain_mount_geometry(
        height_map.shape[1] * pixel_size, height_map.shape[0] * pixel_size, height_map.max(), options.hole_diameter
    )
    expected += signed_volume(np.array(tab_vertices)[np.array(tab_triangles)])

    assert signed_volume(vectors) == pytest.approx(expected, rel=1e-6)


def test_back_text_recessed_at_depth() -> None:
    plain = load_vectors(generate_qr_model(**COLLAR_TAG).stl or b"")
    with_text = load_vectors(generate_qr_model(**COLLAR_TAG, back_text="SCAN ME").stl or b"")
    assert not np.any(np.isclose(plain[:, :, 2], 0.6))
    assert np.any(np.isclose(with_text[:, :, 2], 0.6))  # default back_text_depth
    assert signed_volume(with_text) < signed_volume(plain)


def test_back_text_too_deep() -> None:
    with pytest.raises(QRModelError, match="too deep"):
        generate_qr_model(**COLLAR_TAG, back_text="HI", back_text_depth=1.4)


def test_back_text_shrink_warning() -> None:
    result = generate_qr_model(**COLLAR_TAG, back_text="POINT AT LIGHT\nSCAN QR CODE")
    assert any("shrunk" in warning for warning in result.warnings)


def test_back_text_fits_without_warning() -> None:
    assert generate_qr_model(**COLLAR_TAG, back_text="HI").warnings == []


def test_back_text_with_3mf_rejected() -> None:
    with pytest.raises(QRModelError, match="STL"):
        generate_qr_model(data="x", back_text="HI", formats=("3mf",))


def test_center_text_and_overlay_conflict() -> None:
    overlay = (TEST_IMAGES / "test_logo.png").read_bytes()
    with pytest.raises(QRModelError, match="together"):
        generate_qr_model(data="x", center_text="HI", overlay_image=overlay)


@pytest.mark.parametrize(
    "bad_option",
    [
        {"hole_diameter": 20},
        {"base_width": 5},
        {"size": 50},
        {"mount": "holes"},
        {"formats": ("obj",)},
        {"formats": ()},
        {"layer_heights": [2.0]},
        {"layer_heights": [2.0, -1.0]},
        {"not_an_option": True},
        {"qr_color": "notacolor"},
        {"frame_color": "nope"},
    ],
)
def test_invalid_options_raise_validation_error(bad_option: dict) -> None:
    with pytest.raises(ValidationError):
        generate_qr_model(data="x", **bad_option)


def test_overlay_image_from_bytes() -> None:
    overlay = (TEST_IMAGES / "color_logo.png").read_bytes()
    result = generate_qr_model(data="overlay", overlay_image=overlay, formats=("stl", "png"))
    assert result.stl and result.png


def test_invalid_overlay_bytes() -> None:
    with pytest.raises(QRModelError, match="overlay"):
        generate_qr_model(data="x", overlay_image=b"not an image")


def test_data_too_long() -> None:
    with pytest.raises(QRModelError, match="too long"):
        generate_qr_model(data="x" * 5000, error_correction="H")


@pytest.mark.parametrize(
    "options",
    [
        {"label": "BIGTROOPER", "label_position": "bottom"},
        {"module_style": "circle"},
        {"module_style": "rounded", "module_size_ratio": 0.9},
        {"frame_style": "hexagon", "multi_layer": True},
        {"frame_style": "octagon", "layer_heights": [2, 3, 4], "multi_layer": True},
        {"center_text": "SCAN", "error_correction": "H"},
        {"invert": True},
        {"qr_type": QRType.WIFI, "wifi_password": "secret"},
        {"qr_type": QRType.CONTACT, "contact_phone": "+15551234", "contact_email": "a@b.c"},
        {"base_color": "lightblue", "qr_color": "navy", "formats": ("3mf",), "separate_components": True},
    ],
)
def test_styling_features_generate(options: dict) -> None:
    formats = options.pop("formats", ("stl", "png"))
    result = generate_qr_model(data="BigTrooper", formats=formats, **options)
    for name in formats:
        assert getattr(result, "three_mf" if name == "3mf" else name)


def test_collar_tag_generation_is_fast() -> None:
    start = time.perf_counter()
    generate_qr_model(**COLLAR_TAG, back_text="SCAN ME\nIN LIGHT", size=300)
    assert time.perf_counter() - start < 1.5
