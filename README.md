# bigtrooper-qr

A Python library that generates 3D printable QR code tags for BigTrooper. Pass in a URL (or other data) plus some options, and get back STL, 3MF, PNG or SVG files as bytes. It's built to sit behind a web API, but works in any Python script.

![qr_slicer.png](qr_slicer.png)

## Features

- **QR types:** text, URL, email, phone, SMS, WiFi, contact card (vCard)
- **3D output:** binary STL, or multi-color 3MF
- **2D output:** PNG preview, SVG vector
- **Tag features:** rounded keychain tab with a configurable hole, and multi-line text recessed into the back
- **Styling:** front label, center text or emoji, logo overlay, frames, circle/dot/rounded modules, multi-layer heights, colors, inverted mode
- **In memory:** no files are written; outputs come back as bytes
- **Repeatable:** identical options produce identical bytes
- **Fast:** a 28mm collar tag takes about 0.1–0.4 seconds

## Install

Requires Python 3.11–3.13.

```bash
uv add "bigtrooper-qr @ git+https://github.com/ZappaVinny/bigtrooper-qr"
```

## Quick start

```python
from pathlib import Path
from bigtrooper_qr import QRType, generate_qr_model

result = generate_qr_model(
    data="https://8bt/Hs3Sr",
    qr_type=QRType.URL,
    base_width=28, base_height=28,      # mm
    base_thickness=1.6, qr_depth=0.6,   # mm
    mount="keychain", hole_diameter=3.5,
    back_text="SCAN ME\nIN LIGHT",
    size=300,
    formats=("stl", "png"),
)

Path("collar_tag.stl").write_bytes(result.stl)
Path("collar_tag.png").write_bytes(result.png)
```

**See [LIBRARY.md](LIBRARY.md) for the full reference:** every option, errors, printing guidance, and notes on running it behind an API.

## Development

```bash
uv sync          # install dependencies
make checkall    # format, lint, type check
make test        # run the test suite
make example     # generate the example collar tag into ./output
```

## Credits

Forked from [paulrobello/par_qr_3d](https://github.com/paulrobello/par_qr_3d) by Paul Robello, MIT licensed. This fork turns the original CLI into a library and adds the rounded keychain tab, recessed back text, and in-memory output. See [LICENSE](LICENSE).
