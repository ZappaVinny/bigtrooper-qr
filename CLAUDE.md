# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

bigtrooper-qr (import name `bigtrooper_qr`) is a Python library that generates 3D printable QR code tags (STL, 3MF) and QR images (PNG, SVG) entirely in memory. It is a fork of paulrobello/par_qr_3d, converted from a CLI into a library so a FastAPI service (in Docker, called by a Go backend) can import it. The API service itself lives elsewhere; this repo is only the library.

The full user-facing reference is [LIBRARY.md](LIBRARY.md). Keep it in sync with any option or behavior change.

## Development Commands
- `uv sync`: install dependencies (Python 3.12 is pinned in `.python-version`; 3.14 breaks some dependency builds)
- `make checkall`: format, lint and type check. Run after any code change.
- `make test`: run the pytest suite
- `make example`: run `examples/make_tag.py`, which writes the collar tag to `./output/`
- `uv add <package>` / `uv remove <package>`: manage dependencies

## Architecture

### Package layout (`src/bigtrooper_qr/`)
- `__init__.py`: public exports (`generate_qr_model`, `QRModelOptions`, `QRModelResult`, `QRModelError`, `QRType`, `ErrorCorrectionLevel`), `__version__`, and a logging `NullHandler`
- `api.py`: the public entry point
  - `QRModelOptions` is a pydantic model holding every option, its range and color checks
  - `generate_qr_model()` runs the pipeline
  - `QRModelResult` is a dataclass of output bytes plus warnings
- `errors.py`: `QRModelError(ValueError)` for invalid combinations and content that doesn't fit
- `qr_generator.py`: the 2D steps. QR types and formatting, artistic modules, border crop, label, center text, overlay, frames, PNG encoding, SVG generation.
- `stl_converter.py`: the 3D steps
  - height maps and the STL mesh (`create_stl_from_heightmap`), with `add_recessed_bottom` for back text
  - the rounded keychain tab (`generate_keychain_mount_geometry`)
  - the 3MF builder (lib3mf)
  - `convert_qr_to_stl` and `convert_qr_to_3mf`, which return bytes
- `utils/`: `color_utils` (color parsing, 3MF colors), `font_utils` (bundled font loading), `image_utils` (image mode helpers)
- `fonts/`: Roboto-Black (labels, back text), DejaVuSans (Unicode), NotoEmoji (emoji center text)

### Pipeline
`generate_qr_code` → `crop_qr_border` → optional label, center text, overlay and frame → image outputs (PNG, SVG) and/or model outputs (`convert_qr_to_stl`, `convert_qr_to_3mf`).

### Key design rules
- **Nothing touches the filesystem.** Outputs are bytes. `overlay_image` takes bytes, not a path, so API input can't read server files.
- **Output is deterministic.** The STL is written by hand without numpy-stl's timestamped header.
- **No runtime mesh repair.** Faces must be wound correctly when generated (normals pointing out of the solid). `tests/test_api.py::test_mesh_is_consistently_oriented` checks this using signed volume, so keep it passing whenever geometry code changes.
- **Size limits:** the STL face buffer is preallocated in `create_stl_from_heightmap`, so any new geometry must add its triangle count there (for example `KEYCHAIN_TRIANGLE_COUNT`).
- **Back text** is STL only, mirrored, and auto-shrinks with a warning. It must leave at least 0.4mm of material above it.
- **Errors:** library code raises `QRModelError` for caller mistakes. Never print or exit.

### Key Dependencies
- Runtime: `qrcode[pil]`, `numpy-stl`, `scipy` (frame detection for multi-layer), `lib3mf` (3MF), `pydantic` (options validation)
- Dev: `pytest`, `trimesh` (tests only), `ruff`, `pyright`

### Configuration
- ruff: line length 120, Google-style docstrings
- pyright: basic mode, checks `src/`, `tests/`, `examples/`

### Development Standards
- Type annotations on all functions and methods, and Google-style docstrings
- Use `logging.getLogger(__name__)`. Never configure logging handlers in library code.
- When adding an option, update `QRModelOptions`, the pipeline in `api.py`, the tests, and LIBRARY.md.
