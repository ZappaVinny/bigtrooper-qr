"""Generate the BigTrooper collar tag and write the files to ./output.

Run with: uv run python examples/make_tag.py
"""

from __future__ import annotations

from pathlib import Path

from bigtrooper_qr import QRType, generate_qr_model

OUTPUT_DIR = Path("output")


def main() -> None:
    """Generate the collar tag STL and a PNG preview."""
    result = generate_qr_model(
        data="https://8bt/Hs3Sr",
        qr_type=QRType.URL,
        base_width=28,
        base_height=28,
        base_thickness=1.6,
        qr_depth=0.6,
        mount="keychain",
        hole_diameter=3.5,
        back_text="SCAN ME\nIN LIGHT",
        back_text_size=4,
        size=300,
        formats=("stl", "png"),
    )

    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / "collar_tag.stl").write_bytes(result.stl or b"")
    (OUTPUT_DIR / "collar_tag.png").write_bytes(result.png or b"")
    for warning in result.warnings:
        print(f"Warning: {warning}")
    print(f"Wrote {OUTPUT_DIR / 'collar_tag.stl'} and {OUTPUT_DIR / 'collar_tag.png'}")


if __name__ == "__main__":
    main()
