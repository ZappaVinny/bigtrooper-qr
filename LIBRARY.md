# bigtrooper_qr Library Reference

`bigtrooper_qr` turns data (a URL, text, WiFi credentials, a contact card...) into a 3D printable QR code tag. Everything happens in memory: you pass options to one function and get the file contents back as bytes, ready to save, upload, or return from a web endpoint.

| Output | Contents | Use it for |
|---|---|---|
| **STL** | Binary STL mesh | 3D printing (single material, or color changes by layer) |
| **3MF** | 3MF package with per-component colors | Multi-color printing |
| **PNG** | Flat image of the finished QR design | Previews in a UI |
| **SVG** | Vector image of the QR modules | Print or web graphics |

## Contents
- [Install](#install)
- [Quick start](#quick-start)
- [The `generate_qr_model` function](#the-generate_qr_model-function)
- [Option reference](#option-reference)
  - [QR content](#qr-content)
  - [Output formats](#output-formats)
  - [3D dimensions](#3d-dimensions)
  - [Keychain tab](#keychain-tab)
  - [Arch mount](#arch-mount)
  - [Back text](#back-text)
  - [Front label](#front-label)
  - [Center text and emoji](#center-text-and-emoji)
  - [Overlay image](#overlay-image)
  - [Frames](#frames)
  - [Module styles](#module-styles)
  - [Multi-layer heights](#multi-layer-heights)
  - [Colors and 3MF options](#colors-and-3mf-options)
- [Results and warnings](#results-and-warnings)
- [Errors](#errors)
- [Printing guidance](#printing-guidance)
- [Using it behind an API](#using-it-behind-an-api)

## Install

Requires Python 3.12 (the version the Docker image uses; 3.11 and 3.13 also work, but 3.14 is not supported by every dependency yet). The library is the `bigtrooper_qr/` folder at the repo root, and its dependencies are pinned in `requirements.txt`.

```bash
git clone https://github.com/ZappaVinny/bigtrooper-qr.git
cd bigtrooper-qr
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run scripts from the repo root (so `import bigtrooper_qr` finds the folder), or copy the `bigtrooper_qr/` folder into another project. `requirements.txt` also includes the FastAPI service's packages; the library itself only needs `qrcode[pil]`, `numpy-stl`, `scipy`, `lib3mf` and `pydantic`.

## Quick start

The BigTrooper collar tag: a 28mm QR code with a rounded keychain tab and two lines of text on the back.

```python
from pathlib import Path
from bigtrooper_qr import QRType, generate_qr_model

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

Path("collar_tag.stl").write_bytes(result.stl)
Path("collar_tag.png").write_bytes(result.png)
print(result.warnings)  # [] when everything fit as requested
```

Open the STL in your slicer to check it before printing.

## The `generate_qr_model` function

```python
def generate_qr_model(options: QRModelOptions | None = None, /, **kwargs) -> QRModelResult
```

You can call it two ways. Both run the same validation.

```python
# 1. Keyword arguments
result = generate_qr_model(data="hello", base_width=30, base_height=30)

# 2. An options object (handy when the options come from a request body or config)
from bigtrooper_qr import QRModelOptions
options = QRModelOptions(data="hello", base_width=30, base_height=30)
result = generate_qr_model(options)
```

Passing both an options object and keyword arguments raises `TypeError`.

`QRModelOptions` is a [pydantic](https://docs.pydantic.dev/) model:
- Every option below is a field on it, with the defaults and ranges listed.
- An unknown field name (a typo, for example) or an out-of-range value raises `pydantic.ValidationError`.
- Enum fields also accept their string values, so `qr_type="url"` and `error_correction="H"` work.

**Same options, same bytes.** The same options always produce exactly the same output, so a hash of `options.model_dump_json()` makes a reliable cache key.

## Option reference

Only `data` is required.

### QR content

| Option | Type | Default | Range / values | Description |
|---|---|---|---|---|
| `data` | `str` | (required) | non-empty | What to encode. Its meaning depends on `qr_type`. |
| `qr_type` | `QRType` | `TEXT` | `text`, `url`, `email`, `phone`, `sms`, `wifi`, `contact` | How `data` is formatted (see below). |
| `error_correction` | `ErrorCorrectionLevel` | `LOW` | `L` (~7%), `M` (~15%), `Q` (~25%), `H` (~30%) | How much damage the code tolerates. Higher levels make the code denser. Use `H` when covering the center with text or an image. |
| `size` | `int` | `200` | 100–1000 | Resolution of the QR image in pixels. **This is also the mesh resolution**, so higher values give smoother text and edges but much larger STL files (see [timings](#using-it-behind-an-api)). |
| `border_crop` | `int` | `15` | 0–20 | Pixels trimmed from each edge of the QR image, which reduces the white margin (quiet zone). Larger values trim more. Too little margin makes scanning harder. |

How each `qr_type` uses `data` and the extra fields:

| `qr_type` | `data` is | Extra fields | Encoded as |
|---|---|---|---|
| `text` | any text | none | the text as-is |
| `url` | a URL | none | `https://` is added if no scheme is given |
| `email` | an email address | `email_subject`, `email_body` | `mailto:addr?subject=...&body=...` |
| `phone` | a phone number | none | `tel:+15551234` (non-digits other than `+` are removed) |
| `sms` | a phone number | `sms_message` | `smsto:number:message` |
| `wifi` | the network name (SSID) | `wifi_password`, `wifi_security` (`WPA`, `WEP` or `nopass`, default `WPA`) | `WIFI:T:WPA;S:ssid;P:password;H:false;;` |
| `contact` | the contact's full name | `contact_phone`, `contact_email`, `contact_org` | a vCard 3.0 |

Extra fields that don't match the chosen `qr_type` are ignored.

```python
generate_qr_model(data="GuestNet", qr_type="wifi", wifi_password="hunter22", error_correction="M")
generate_qr_model(data="Jane Smith", qr_type="contact", contact_phone="+15551234", contact_email="jane@example.com")
```

### Output formats

| Option | Type | Default | Values |
|---|---|---|---|
| `formats` | `tuple[str, ...]` | `("stl",)` | any non-empty combination of `"stl"`, `"3mf"`, `"png"`, `"svg"` |

Only the requested outputs are generated, and the others are `None` on the result. Rules to know:

- **PNG** is the finished 2D design: label, center text, overlay and frame included.
- **SVG** contains only the QR modules, with `base_color`, `qr_color` and `module_style` applied. Label, frame, center text and overlay are not included.
- **Grayscale conversion:** when any 3D format (`stl` or `3mf`) is requested, center text and overlay images are converted to grayscale, since the model is built from brightness. Request only `png` or `svg` to keep them in full color.
- **3MF limits:** it doesn't support `back_text` (that raises an error) and ignores `multi_layer`.

### 3D dimensions

| Option | Type | Default | Range | Description |
|---|---|---|---|---|
| `base_width` | `float` | `50.0` | 10–500 mm | Width of the QR plate. |
| `base_height` | `float` | `50.0` | 10–500 mm | Height of the QR plate. A mount (tab or arch) is added on top of this. |
| `base_thickness` | `float` | `2.0` | 0.5–20 mm | Thickness of the solid plate under everything. |
| `qr_depth` | `float` | `1.0` | 0.5–20 mm | How far the dark QR modules rise above the plate. |
| `corner_radius` | `float` | `0.0` | 0 to half the shorter side | Rounds the plate's four corners, so the tag has no sharp points. `0` keeps square corners. |
| `invert` | `bool` | `False` | | Raise the light areas instead, so the dark modules become recessed. |

Total model thickness is `base_thickness + qr_depth`.

**Rounded corners.** 3–4mm takes the sharp points off a collar tag. Rounding only cuts into the plate's corners, so the QR keeps its full size. Keep it small enough not to clip the QR's corner squares: the cut reaches about 0.29 × `corner_radius` in from each side, and a 28mm tag at `size=300` has about 2.7mm of margin, so stay under about 9mm. In 3MF the outline is a smooth curve. In STL it follows the pixel grid in steps of about 0.1mm, which is far below what a 0.4mm nozzle can print. With a keychain tab, the tab must still fit on the straight part of the top edge.

### Keychain tab

| Option | Type | Default | Range / values | Description |
|---|---|---|---|---|
| `mount` | `"keychain"`, `"arch"` or `None` | `None` | | `"keychain"` adds a small rounded tab with a hole, centered on the top edge. `"arch"` adds a full-width arch (see [Arch mount](#arch-mount)). |
| `hole_diameter` | `float` | `4.0` | 1–10 mm | Diameter of the hole. Use 3.5 for the small split rings on pet tags, or 4–5 for thicker rings. |
| `hole_band` | `float` | `2.5` | 1–6 mm | Width of the plastic ring around the hole, including between the hole and the plate. |

How the tab is shaped:
- **Shape:** a "D". Straight sides rise from the QR plate and close in a half-circle centered on the hole. The ring is `hole_band` wide all the way round, including toward the plate, so there's no thin neck.
- **Size:** `hole_diameter + 2 × hole_band` wide and tall (8.5 × 8.5mm for a 3.5mm hole with the default band), and always **2mm thick** whatever `base_thickness` is.
- **Strength:** 2.5mm of ring is about six nozzle widths, which holds up to a split ring. Go down to 2mm for a slimmer look, or up to 3mm for tags that take rough wear.
- **Footprint:** the tab adds `hole_diameter + 2 × hole_band` to the overall height. A 28mm tag with a 3.5mm hole comes out about 28 × 36.5mm.

### Arch mount

`mount="arch"` replaces the small tab with an arch as wide as the plate, leaving a large opening for a split ring, collar clip or lanyard.

| Option | Type | Default | Range | Description |
|---|---|---|---|---|
| `arch_band` | `float` | `4.0` | 1–10 mm | Width of the arch band. It's the same width everywhere along the curve. |
| `arch_height` | `float` | `12.0` | 4–100 mm | Total height of the mount above the plate edge, padding included. |
| `arch_padding` | `float` | `2.0` | 0.5–20 mm | Solid strip between the plate edge and the opening. It ties the two legs together and stiffens the plate's top edge. |

How the arch is shaped:
- **Shape:** a half-ellipse as wide as the plate. Its outer edges continue the plate's sides straight up, so the outline has no corners where the arch meets the plate. The opening's edge is the outer curve moved inward by `arch_band`.
- **Why a half-ellipse:** a ring pulls on the top of the arch. Vertical legs carry that pull straight down as tension instead of bending the arch, and the half-ellipse meets the sides vertically. A taller arch (bigger `arch_height`) makes the shape more upright and stronger.
- **Opening:** `base_width − 2 × arch_band` wide and `arch_height − arch_padding − arch_band` tall. That's 20 × 6mm on a 28mm tag with the defaults.
- **Size:** always **2mm thick**, like the keychain tab. A 28mm tag comes out about 28 × 40mm.
- **Corners:** with `corner_radius`, only the plate's bottom corners are rounded. The top corners stay square because the arch's legs continue them.
- **Limit:** `arch_band` must be thinner than the arch's tightest bend, or the opening's edge would fold over itself. Thin bands, tall arches and wide plates all have more room.

### Back text

Text cut into the bottom (bed-side) face of the STL. Supported in STL only.

| Option | Type | Default | Range | Description |
|---|---|---|---|---|
| `back_text` | `str` or `None` | `None` | | The text. Separate lines with `\n` (a real newline or a literal backslash-n). Lines are centered. |
| `back_text_size` | `float` | `4.0` | 2–30 mm | Target height of capital letters. |
| `back_text_depth` | `float` | `0.6` | 0.2–5 mm | How deep the letters are cut. |

How back text behaves:
- **Mirroring:** the text is mirrored so it reads correctly when you flip the tag over. The keychain tab stays at the top.
- **Placement:** it's centered on the QR plate with at least 1.5mm of clearance from the edges, rendered in Roboto Black (the boldest bundled font).
- **Auto-shrink:** if the text doesn't fit at `back_text_size`, it shrinks until it does, and a message is added to `result.warnings` with the size actually used. If it can't fit even at the smallest size, a `QRModelError` is raised.
- **Depth limit:** at least 0.4mm of plastic must remain above the letters. A `back_text_depth` greater than the thinnest part of the model minus 0.4mm raises `QRModelError`. With `base_thickness=1.6`, the deepest allowed is 1.2mm.
- **Line width decides the size.** The longest line is usually what limits the letter height. On a 28mm tag, lines of about 7–8 characters fit at 4mm. See [Printing guidance](#printing-guidance).
- **Backlighting:** the letters are where the tag is thinnest, so they glow brighter when light shines through translucent filament.

```python
generate_qr_model(data="https://8bt/Hs3Sr", base_width=28, base_height=28, base_thickness=1.6,
                  back_text="POINT AT\nLIGHT\nSCAN QR\nCODE", back_text_size=4, size=300)
```

### Front label

A text label added above or below the QR code on the front. The label makes the image taller, and pixels stay square, so the model is no longer exactly `base_width` × `base_height`: a 30 × 30mm plate with a label comes out about 28.6 × 31.6mm. Frames don't have this effect because they add the same amount on every side.

| Option | Type | Default | Range / values | Description |
|---|---|---|---|---|
| `label` | `str` or `None` | `None` | | Label text, rendered in Roboto Black. |
| `label_position` | `str` | `"top"` | `"top"`, `"bottom"` | Where the label goes. |
| `label_threshold` | `int` | `128` | 0–255 | Brightness cutoff for turning the text into solid black and white. Lower makes the text thinner; higher makes it bolder. |

### Center text and emoji

Text or an emoji drawn on a background square in the middle of the QR code. Can't be combined with `overlay_image`. Use `error_correction="H"` (or at least `"Q"`) so the code still scans.

| Option | Type | Default | Range | Description |
|---|---|---|---|---|
| `center_text` | `str` or `None` | `None` | | Text or emoji. Emoji use the bundled Noto Emoji font. |
| `center_text_size` | `int` | `24` | 8–100 px | Starting font size. It shrinks automatically to fit the area. |
| `center_text_area` | `int` | `20` | 10–30 % | Size of the background square, as a percentage of the QR code. |
| `center_text_color` | `str` | `"black"` | | Text color. |
| `center_text_bg` | `str` | `"white"` | | Background square color. |
| `center_text_threshold` | `int` or `None` | `128` | 0–255 | For 3D outputs, the cutoff for turning the text into pure black and white. `None` keeps gray levels. |

### Overlay image

A logo or picture in the middle of the QR code, on a white padded background. Can't be combined with `center_text`. Use a high `error_correction`.

| Option | Type | Default | Range | Description |
|---|---|---|---|---|
| `overlay_image` | `bytes` or `None` | `None` | | The encoded image file (PNG, JPEG, ...). Transparency is respected. |
| `overlay_size_percent` | `int` | `20` | 10–30 % | Overlay size as a percentage of the QR code. |

The option takes **bytes, not a file path**. That way, a request passed straight through from an API can't make the server read files from its own disk. Read local files yourself:

```python
generate_qr_model(data="https://example.com", error_correction="H",
                  overlay_image=Path("logo.png").read_bytes(), formats=("stl", "png"))
```

Bytes that aren't a readable image raise `QRModelError`.

### Frames

A decorative border around the QR code.

| Option | Type | Default | Range / values | Description |
|---|---|---|---|---|
| `frame_style` | `str` or `None` | `None` | `"square"`, `"rounded"`, `"hexagon"`, `"octagon"` | Frame shape. Hexagon and octagon frames mask off the corners. |
| `frame_width` | `int` | `10` | 5–50 px | Border width in image pixels, relative to `size`. |
| `frame_color` | `str` | `"black"` | | Frame color. Dark frames become raised in 3D. |

### Module styles

The shape of the individual QR squares (modules).

| Option | Type | Default | Range / values | Description |
|---|---|---|---|---|
| `module_style` | `str` | `"square"` | `"square"`, `"circle"`, `"dot"`, `"rounded"` | Module shape. `dot` draws smaller circles than `circle`. |
| `module_size_ratio` | `float` | `0.8` | 0.5–1.0 | Size of each styled module relative to its grid cell. Has no effect on `square`. |

Square modules scan most reliably on small prints. Circles and dots leave gaps between modules, which can blur together at small sizes.

### Multi-layer heights

STL only. Prints the base, the QR modules and the frame at three distinct heights.

| Option | Type | Default | Range | Description |
|---|---|---|---|---|
| `multi_layer` | `bool` | `False` | | Turn on multi-layer mode. |
| `layer_heights` | `list[float]` or `None` | `None` | 2–3 positive values, in mm | Absolute heights `[base, qr]` or `[base, qr, frame]`. Used only when `multi_layer=True`. |

If `layer_heights` isn't given, the heights are `[base_thickness, base_thickness + qr_depth]`. When a frame is present, a third height of `base_thickness + qr_depth + 2` is added for it. The frame is detected as the dark region touching the image edges.

```python
generate_qr_model(data="Layered", frame_style="hexagon", multi_layer=True, layer_heights=[2, 3, 4])
```

### Colors and 3MF options

| Option | Type | Default | Description |
|---|---|---|---|
| `base_color` | `str` | `"white"` | Background color: a CSS name (`"lightblue"`) or hex (`"#f0f0f0"`). Used by PNG, SVG and 3MF. All color options are checked up front, and an unrecognized color raises `ValidationError`. |
| `qr_color` | `str` | `"black"` | Module color. Used by PNG, SVG and 3MF. |
| `separate_components` | `bool` | `False` | 3MF only: export the base and the QR modules as separate objects, which some slicers handle better for color assignment. |

STL files carry no color. For a two-color STL print, add a filament change in your slicer at the layer where the raised modules begin.

## Results and warnings

`generate_qr_model` returns a `QRModelResult`:

| Field | Type | Contents |
|---|---|---|
| `stl` | `bytes` or `None` | Binary STL file |
| `three_mf` | `bytes` or `None` | 3MF file (a zip package) |
| `png` | `bytes` or `None` | PNG image of the finished design |
| `svg` | `str` or `None` | SVG markup |
| `warnings` | `list[str]` | Non-fatal issues. Currently only: back text was shrunk to fit. |

Warnings are also sent to the `bigtrooper_qr` logger. The library adds a `NullHandler`, so nothing prints unless your application sets up logging (for example `logging.basicConfig(level=logging.INFO)`).

## Errors

| Exception | When |
|---|---|
| `pydantic.ValidationError` | An unknown option, a wrong type, an out-of-range value, an invalid choice, or an unrecognized color (for example `hole_diameter=20`, `mount="holes"`, `formats=()`, `layer_heights=[2]`, `qr_color="notacolor"`). |
| `QRModelError` (a subclass of `ValueError`) | Options that conflict, or content that can't be generated (list below). |
| `TypeError` | Passing both an options object and keyword arguments. |

`QRModelError` is raised when:
- `back_text` is combined with `"3mf"` in `formats`
- `center_text` is combined with `overlay_image`
- `back_text_depth` leaves less than 0.4mm of plastic above the letters
- `back_text` can't fit on the plate even at the smallest size, or is empty or only whitespace
- `data` is too long for a QR code at the chosen `error_correction` level
- `overlay_image` bytes aren't a readable image
- `corner_radius` is more than half the shorter side of the plate
- the keychain tab (`hole_diameter + 2 × hole_band` wide) doesn't fit on the straight top edge left by `corner_radius`
- `arch_band` is too thick for the arch's tightest bend (use a thinner band or a taller arch)

Both `ValidationError` and `QRModelError` describe problems with the caller's input. In an API, return them as 4xx responses with the error message.

## Printing guidance

Learned from test prints with a 0.4mm nozzle.

**Text size**
- Letters need to be **at least 4mm tall** to print cleanly with a 0.4mm nozzle. At about 2mm, each letter stroke is a single nozzle-width line and comes out garbled.
- On a 28mm tag, that means about **7–8 characters per line**, in up to 3–4 lines. For example, `"POINT AT LIGHT"` / `"SCAN QR CODE"` shrinks to about 2.4mm, while `"POINT AT"` / `"LIGHT"` / `"SCAN QR"` / `"CODE"` fits at about 4mm.
- A 0.25mm nozzle can print 2–2.5mm text.
- Raising `size` (to 300 or 400) smooths the letter edges, but the STL gets much larger.

**Slicer settings for back text.** The letters are formed by the first layers, so squish from the first layer fills them in:
- Turn on elephant foot compensation (0.1–0.2mm).
- Use a 0.2mm first layer, with first-layer flow at or slightly below 100%.
- Use the Arachne wall generator (the default in Orca, Bambu Studio and PrusaSlicer 2.6+).
- A smooth build plate gives crisper bed-side detail than a textured one.

**Size and scanning**
- About 20mm is the smallest practical QR size. Shorter data gives fewer, larger modules, which scan better.
- Print dark on light, using a filament change at the layer where the modules start. Contrast matters more than sharpness.
- Test-scan a print before committing to a design.

**Thickness and translucency**
- A `base_thickness` of 1.6mm is sturdy for a collar tag and still lets light through translucent or white filament. Around 1.8mm still works. Past about 2mm, light gets noticeably dimmer.

**Collar tag sizes**
- 22–28mm suits typical pet tags, and 30–32mm is the large end.
- Remember the mount adds to the height: about 8.5mm for the keychain tab (3.5mm hole, default `hole_band`), or 12mm for the arch with its defaults.

## Using it behind an API

This repo's own FastAPI service lives in `app/`, and how it will call the library is described in [PLAN.md](PLAN.md). The notes below apply to that service or any other wrapper.

`generate_qr_model` holds no state and is safe to call from many workers at once. Things to plan for when wrapping it in a web service:

**It's CPU-bound.** Don't call it directly inside an `async def` endpoint, because it will block the event loop. Either:
- declare the endpoint with plain `def` (FastAPI runs it in a threadpool), or
- send it to a `ProcessPoolExecutor`. Most of the mesh building is pure Python, which holds the GIL, so **processes give real parallelism and threads don't**.

**Typical timings and STL sizes** (28mm collar tag with keychain tab, measured on a desktop CPU):

| `size` | No back text | With back text |
|---|---|---|
| 200 | 0.08s, 3.3 MB | 0.15s, 6.3 MB |
| 300 | 0.20s, 7.9 MB | 0.36s, 15.4 MB |
| 400 | 0.36s, 14.5 MB | 0.67s, 28.5 MB |

A 3MF at `size=200` takes about 0.4s and is about 1.3 MB, since it's zip-compressed.

**Limit what callers can request.**
- STL size grows with `size` squared. Allowing `size=1000` invites very large responses, so consider capping `size` (400 is plenty) and the plate dimensions at the API layer.
- Rate-limit requests, since each one costs CPU time.

**Request bodies.**
- `QRModelOptions` can be used directly as a FastAPI request body model. Its validation then becomes your API validation.
- `overlay_image` is bytes, which don't fit well in JSON. This repo's service doesn't expose it. A wrapper that needs it should accept a multipart `UploadFile` and pass `await file.read()`.

**Responses.**
- Return the bytes directly: `Response(content=result.stl, media_type="model/stl")`, `"model/3mf"`, `"image/png"`, or `"image/svg+xml"`.
- STLs compress very well (5–10×). Enable gzip on the response, or cache files in object storage.

**Caching.** The same options always produce the same bytes, so `sha256(options.model_dump_json())` works as a cache key.
