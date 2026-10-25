# bigtrooper-qr

Generates 3D printable QR code tags for BigTrooper. The repo has two parts:

- **`bigtrooper_qr/`**, the library. Pass in a URL (or other data) and options, and get back STL, 3MF, PNG or SVG files as bytes.
- **`app/`**, a FastAPI service that runs as a container in the web app's docker compose, so the Go backend can generate tags over the internal network.

| Doc | What's in it |
|---|---|
| [LIBRARY.md](LIBRARY.md) | Every library option, errors, and printing guidance |
| [PLAN.md](PLAN.md) | How the service will be wired up: the endpoint, R2 uploads, and the Go side (not built yet) |

## Features
- **QR types:** text, URL, email, phone, SMS, WiFi, contact card (vCard)
- **Outputs:** binary STL, multi-color 3MF, PNG preview, SVG
- **Tag features:** rounded keychain tab with a configurable hole, and multi-line text recessed into the back
- **Styling:** front label, center text or emoji, logo overlay (library only), frames, module styles, multi-layer heights, colors
- **In memory and repeatable:** no files are written, and the same options always give the same bytes. A 28mm collar tag takes about 0.1–0.4 seconds.

## Run locally

Requires Python 3.12.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --reload        # service on http://127.0.0.1:8000 (GET /up returns {"status": "ok"})
```

## Service API

| Method | Path | Body | Success |
|---|---|---|---|
| GET | `/up` | – | `200 {"status": "ok"}` |
| POST | `/` | `{"link": "https://…/<code>", "type": "pet"}` | `200 {"models": [...], "warnings": [...]}` |

```json
{
  "models": [
    {"variant": "plain", "content_type": "model/stl", "extension": "stl", "data": "<base64>"},
    {"variant": "color", "content_type": "model/3mf", "extension": "3mf", "data": "<base64>"}
  ],
  "warnings": []
}
```

- `link` is encoded as-is; the caller builds the full URL.
- `type` picks a preset in `TAG_PRESETS` (`app/main.py`), which generates one model per variant.
- Responses are gzipped when the client accepts it (~25 MB raw, ~6.6 MB gzipped for `pet`).
- `422` for an unknown `type` or options the library rejects.

Use the library directly from the repo root:

```python
from pathlib import Path
from bigtrooper_qr import generate_qr_model

result = generate_qr_model(
    data="https://8bt/Hs3Sr", qr_type="url",
    base_width=28, base_height=28, base_thickness=1.6, qr_depth=0.6,
    mount="keychain", hole_diameter=3.5,
    back_text="SCAN ME\nIN LIGHT", size=300,
    formats=("stl", "png"),
)
Path("collar_tag.stl").write_bytes(result.stl)
```

## Run with Docker

```bash
cp .env.example .env          # optional; every setting has a default
docker compose up -d --build
docker compose ps             # qr-api should show (healthy)
```

- The container listens on port 8000 **inside the Docker network only**. There's no port mapping to the host, by design.
- To add it to the web app's stack, `include:` this repo's `docker-compose.yml` from the web app's compose (see PLAN.md).
- The image targets `linux/amd64`, because the lib3mf dependency has no ARM Linux build.
- Settings are environment variables, all documented in [.env.example](.env.example).

## Credits

Forked from [paulrobello/par_qr_3d](https://github.com/paulrobello/par_qr_3d) by Paul Robello, MIT licensed. This fork turns the original CLI into a library and service, and adds the rounded keychain tab, recessed back text, and in-memory output. See [LICENSE](LICENSE).
