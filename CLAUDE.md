# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

bigtrooper-qr generates 3D printable QR code tags. It has two parts:
- **`bigtrooper_qr/`:** the library. It produces STL, 3MF, PNG and SVG as bytes, entirely in memory.
- **`app/`:** a FastAPI service. It runs as one container in the web app's docker compose, and the Go backend calls it over the internal network.

The intended service flow:
1. Go POSTs QR options plus presigned R2 PUT URLs.
2. The service generates the files and uploads them to R2.
3. It replies with file info, and Go hands a download link to the frontend.

**Only a `GET /` status route exists so far.** The wiring is specified in [PLAN.md](PLAN.md); follow it when building the endpoint.

The repo is a fork of paulrobello/par_qr_3d. It was deliberately stripped to only what produces QR codes: no tests, no uv/pyproject packaging, no lint/type-check tooling. Don't reintroduce them unless asked.

## Running
- **Setup:** `python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt` (Python 3.12; 3.14 breaks some dependency builds)
- **Service:** `uvicorn app.main:app --reload`, then check `GET /` (returns `{"status": "ok"}`)
- **Docker:** `docker compose up -d --build`. The service is internal only (`expose`, no `ports`); check it with `docker compose ps`.
- **Library:** run Python from the repo root and `import bigtrooper_qr`. There's no package install.

## Layout
- `bigtrooper_qr/__init__.py`: public exports (`generate_qr_model`, `QRModelOptions`, `QRModelResult`, `QRModelError`, `QRType`, `ErrorCorrectionLevel`), `__version__`, and a logging NullHandler
- `bigtrooper_qr/api.py`: `QRModelOptions` (pydantic: every option, its range, color checks), `generate_qr_model()` (the pipeline), and `QRModelResult`
- `bigtrooper_qr/errors.py`: `QRModelError(ValueError)`
- `bigtrooper_qr/qr_generator.py`: 2D steps (QR types, styles, crop, label, center text, overlay, frames, PNG, SVG)
- `bigtrooper_qr/stl_converter.py`: 3D steps (height-map STL mesh, recessed back text, rounded keychain tab, 3MF via lib3mf); returns bytes
- `bigtrooper_qr/utils/`: color, font and image helpers
- `bigtrooper_qr/fonts/`: Roboto-Black, DejaVuSans, NotoEmoji
- `app/main.py`: a minimal FastAPI app with only `GET /`, which the Docker healthcheck also uses. Keep it simple.
- `.env.example`: the `BTQR_*` settings planned for the wiring step (nothing reads them yet)
- `requirements.txt`: exact pins for every package, targeting Python 3.12 on linux/amd64
- `Dockerfile`, `docker-compose.yml`, `.dockerignore`: one simple service, `qr-api`, built to be `include:`d by the web app's compose
- `LIBRARY.md` is the library reference and `PLAN.md` the wiring plan. Keep both in sync with changes.

## Key Rules
- **Nothing touches the filesystem;** outputs are bytes. `overlay_image` takes bytes, never a path, and isn't exposed by the API.
- **Output is deterministic.** The STL header is written by hand, with no timestamp.
- **No runtime mesh repair.** Faces must be generated with outward-facing winding. After any geometry change, check that the mesh's signed volume (the sum of v0·(v1×v2)/6 over all triangles) equals the volume implied by the height map plus the tab. The collar tag at `size=300` with back text "SCAN ME\nIN LIGHT" gives 308,700 faces and a volume of 1602.717 mm³. Then slice a generated STL.
- **Preallocated face buffer:** new geometry must add its triangle count in `create_stl_from_heightmap`.
- **Back text** is STL only and mirrored. It auto-shrinks with a warning, and must leave at least 0.4mm of material above it.
- **Errors:** library code raises `QRModelError` for caller mistakes. It never prints or exits.
- **Never log presigned URLs** (the query string is a credential); log only the host.
- Use `logging.getLogger(__name__)`. Don't configure handlers in library code; `app/main.py` does that.
- **Code style:** type annotations and Google-style docstrings on all functions, line length 120.
- **Adding a library option** means updating `QRModelOptions`, the pipeline in `api.py`, and LIBRARY.md.
