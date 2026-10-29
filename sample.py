"""Generate sample tag models into output/ for checking library changes.

Run from the repo root: python sample.py
"""

from pathlib import Path
from typing import Any

from bigtrooper_qr import QRType, generate_qr_model

OUTPUT_DIR = Path(__file__).parent / "output"

LINK = "https://8bt/Hs3Sr"

BASE: dict[str, Any] = {
    "base_width": 28,
    "base_height": 28,
    "base_thickness": 1.6,
    "corner_radius": 4.0,
    "qr_depth": 0.6,
    "mount": "arch",
    "arch_band": 4.0,
    "arch_height": 12.0,
    "arch_padding": 2.0,
    "size": 300,
}

VARIANTS: dict[str, dict[str, Any]] = {
    "plain": {**BASE, "formats": ("stl",), "back_text": "SCAN ME\nIN LIGHT", "back_text_size": 4},
    "color": {**BASE, "formats": ("3mf",)},
}

OUTPUTS: dict[str, str] = {"stl": "stl", "3mf": "three_mf"}


def main() -> None:
    """Generate every variant and write each file to output/."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    for variant, options in VARIANTS.items():
        result = generate_qr_model(data=LINK, qr_type=QRType.URL, **options)
        for fmt in options["formats"]:
            path = OUTPUT_DIR / f"sample_tag_{variant}.{fmt}"
            path.write_bytes(getattr(result, OUTPUTS[fmt]))
            print(path)
        for warning in result.warnings:
            print(f"  {variant}: {warning}")


if __name__ == "__main__":
    main()
