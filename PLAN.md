# PLAN: Wiring the QR service into the BigTrooper stack

**Status:** not built yet. The repo currently has the library (`bigtrooper_qr/`), a minimal FastAPI app (`app/main.py`, just `GET /`), `requirements.txt`, `.env.example` and the Docker files. This document describes the remaining work: the generation endpoint, uploads to R2, and how the Go backend calls it.

## How it fits together

```
Frontend ──► Go backend ──(1) presign PUT URLs on R2
                 │
                 ├──(2) POST http://qr-api:8000/v1/tags  { options, uploads }
                 │         qr-api: generate STL/PNG/... in memory
                 │                 PUT each file to its presigned URL ──► R2
                 │◄──(3) 200 { files: {stl: {size, sha256}, ...}, warnings }
                 │
                 └──(4) create a download link for the R2 object ──► Frontend
```

- `qr-api` runs as a container in the web app's docker compose, on the internal `backend` network only. It has no published ports.
- The call is **synchronous**. The Go backend waits for the 200, which means the files are already in R2. Generation takes about 0.1–0.7s, plus upload time.
- The service never holds R2 credentials. It only receives presigned PUT URLs, which are single-use, expiring, and limited to one object each.

## Request

`POST /v1/tags`

```json
{
  "request_id": "tag_123",
  "options": {
    "data": "https://8bt/Hs3Sr",
    "qr_type": "url",
    "base_width": 28,
    "base_height": 28,
    "base_thickness": 1.6,
    "qr_depth": 0.6,
    "mount": "keychain",
    "hole_diameter": 3.5,
    "back_text": "SCAN ME\nIN LIGHT",
    "size": 300
  },
  "uploads": {
    "stl": "https://<account>.r2.cloudflarestorage.com/<bucket>/<key>.stl?X-Amz-...",
    "png": "https://<account>.r2.cloudflarestorage.com/<bucket>/<key>.png?X-Amz-..."
  }
}
```

| Field | Required | Description |
|---|---|---|
| `options` | yes | Any `bigtrooper_qr.QRModelOptions` fields (see LIBRARY.md). Which of these come from the user and which are fixed by the Go backend is decided on the Go side; the service accepts and validates all of them. Two exceptions: `formats` is not allowed (it comes from `uploads`), and `overlay_image` is not supported in the API. |
| `uploads` | yes, at least one | A map from format (`stl`, `3mf`, `png`, `svg`) to a presigned PUT URL. **The keys decide which formats get generated.** |
| `request_id` | no | Any string, for example the tag or order ID. It's echoed in the response and included in the service's log lines, so a request can be traced from Go into the service. |

The service also enforces server-side caps from the environment: `size` ≤ `BTQR_MAX_QR_SIZE_PX`, and plate width and height ≤ `BTQR_MAX_BASE_MM`.

## Response

**200:** every file is uploaded.

```json
{
  "request_id": "tag_123",
  "files": {
    "stl": { "content_type": "model/stl", "size_bytes": 15435084, "sha256": "9f2c..." },
    "png": { "content_type": "image/png", "size_bytes": 456, "sha256": "41ab..." }
  },
  "warnings": ["Back text shrunk to fit: letters are 3.7mm tall instead of 4.0mm"],
  "duration_ms": 812
}
```

`warnings` is worth showing to the user, since it tells them their back text was printed smaller than requested.

**Errors** all use one body shape: `{ "request_id": "...", "error": { "code": "...", "message": "..." } }`

| Status | `code` | When | What Go should do |
|---|---|---|---|
| 401 | `unauthorized` | `X-Internal-Token` is missing or wrong (only when `BTQR_API_TOKEN` is set) | Fix the configuration |
| 422 | `invalid_request` | A bad option or range, an unknown field, `formats` or `overlay_image` in options, an upload host not on the allowlist, or a server cap exceeded | Show `message` to the user if the option came from them |
| 422 | `generation_error` | The library raised `QRModelError`: back text too deep or can't fit, data too long for a QR code, or back text with 3MF | Show `message` to the user |
| 502 | `upload_failed` | R2 rejected a PUT (for example an expired URL or a Content-Type mismatch) or was unreachable after retries. `message` names the format. | Presign fresh URLs and retry once |
| 503 | `busy` | Every worker stayed busy longer than the queue timeout | Retry with backoff |

## Presigning rules for the Go backend

1. **Sign each URL with the matching Content-Type.** The service uploads with these exact headers:

   | Format | Content-Type |
   |---|---|
   | `stl` | `model/stl` |
   | `3mf` | `model/3mf` |
   | `png` | `image/png` |
   | `svg` | `image/svg+xml` |

   If the URL was signed with a different Content-Type, R2 returns 403 and the service replies `upload_failed`.
2. **Use the R2 S3 endpoint host** (`<account>.r2.cloudflarestorage.com`), or add your host to `BTQR_ALLOWED_UPLOAD_HOSTS`.
3. **Give the URL enough time.** About 5 minutes covers queueing, generation and uploading.

Go example (aws-sdk-go-v2):

```go
presigner := s3.NewPresignClient(r2Client)

req, err := presigner.PresignPutObject(ctx, &s3.PutObjectInput{
    Bucket:      aws.String("bigtrooper-tags"),
    Key:         aws.String(fmt.Sprintf("tags/%s.stl", tagID)),
    ContentType: aws.String("model/stl"), // must match what the service sends
}, s3.WithPresignExpires(5*time.Minute))
if err != nil { /* handle */ }

body := map[string]any{
    "request_id": tagID,
    "options":    options, // built from user input plus your fixed defaults
    "uploads":    map[string]string{"stl": req.URL},
}
// POST to http://qr-api:8000/v1/tags with header X-Internal-Token: <BTQR_API_TOKEN>
// Use a client timeout of about 120s.
```

After the 200, generate a presigned **GET** URL (or a public or custom-domain URL) for the same key and send it to the frontend.

## Service implementation (to build)

| File | Responsibility |
|---|---|
| `app/schemas.py` | `TagRequest`, `TagResponse`, `FileInfo`, `ErrorResponse`. `TagRequest` validators: reject `formats` and `overlay_image` in `options` (using `options.model_fields_set`); require at least one upload; check every upload URL is `https` and its host matches `BTQR_ALLOWED_UPLOAD_HOSTS`; enforce the `BTQR_MAX_QR_SIZE_PX` and `BTQR_MAX_BASE_MM` caps. Read these settings with `os.environ.get` (defaults as in `.env.example`); no settings framework needed. |
| `app/uploader.py` | `upload_files(client, uploads, files)`: concurrent `httpx.AsyncClient.put` calls with the Content-Types above. Retries twice with backoff on connection errors and 5xx responses, but never on 4xx, since a bad or expired signature won't recover. Raises `UploadError(format, status)`. |
| `app/main.py` | Adds a lifespan that creates one shared `httpx.AsyncClient` (timeout `BTQR_UPLOAD_TIMEOUT_S`); an `X-Internal-Token` dependency using a constant-time comparison (`hmac.compare_digest`); exception handlers that map errors to the table above; and the `POST /v1/tags` route. |
| `requirements.txt` | Add `httpx`, pinned. |

### Endpoint flow
1. FastAPI validates the body into `TagRequest`, which returns 422 on any problem.
2. Wait for a free slot on a per-worker semaphore (one job at a time). If no slot frees up within 30s, return 503 `busy`.
3. Run `generate_qr_model(options.model_copy(update={"formats": tuple(uploads)}))` with `anyio.to_thread.run_sync`, so the event loop stays free for uploads and health checks. Map `QRModelError` to 422 `generation_error`.
4. Compute size and sha256 for each file.
5. Upload all files concurrently. If any upload fails, return 502 `upload_failed`.
6. Return 200 with `files`, `warnings` and `duration_ms`.

### Concurrency
- Generation is CPU-bound Python, so threads don't run it in parallel. The container runs a single uvicorn process, so it generates one tag at a time (each takes about 0.1–0.7s). If that becomes a bottleneck, add `--workers N` to the Dockerfile's uvicorn command or run more `qr-api` replicas.
- Uploads are I/O-bound and run asynchronously alongside generation.
- Memory per job: the STL is held in memory, up to about 30 MB at `size=400` with back text. A compose memory limit of about 256–512 MB per container is plenty.

### Logging
- Each log line includes `request_id`, the formats, the timings, file sizes and any warnings.
- **Never log presigned URLs or full request bodies.** The query string is a credential, so log only the upload host.

## Security
- **Network isolation:** no `ports:` in compose, so only containers on the `backend` network can reach the service.
- **Shared token:** set `BTQR_API_TOKEN` in both services as a second layer.
- **SSRF protection:** the service only sends requests to allowlisted `https` upload hosts. It never fetches URLs supplied by callers, which is one reason the overlay image isn't offered.
- **Abuse limits:** server caps on `size` and plate size, a queue timeout, and the library's own range checks.
- **Container hardening:** non-root user, read-only filesystem, and the library never writes files.

## Adding it to the web app's compose

```yaml
include:
  - path: ../bigtrooper-qr/docker-compose.yml   # adds the qr-api service and the backend network

services:
  go-backend:
    # ...
    environment:
      QR_API_URL: http://qr-api:8000
      QR_API_TOKEN: ${BTQR_API_TOKEN}
    networks: [backend]
    depends_on:
      qr-api:
        condition: service_healthy
```

Alternatively, copy the `qr-api` block from `docker-compose.yml`. Either way, add resource limits in production, for example `deploy.resources.limits: { cpus: "2", memory: 512M }`.

## Testing the wiring
1. **Locally without Docker:** `uvicorn app.main:app --reload`, then POST with `curl`, using presigned URLs from MinIO or a real dev bucket.
2. **With compose:** add a `minio` service (S3-compatible, standing in for R2) to a dev-only compose override. Set `BTQR_ALLOWED_UPLOAD_HOSTS=minio`, and allow `http` for that host only in development. A small script presigns PUT URLs on MinIO with the right Content-Type, calls the service, downloads the objects back, and checks the sha256 values match the response.
3. **Failure cases:** an expired URL or wrong Content-Type should give 502 `upload_failed`; a host not on the allowlist, 422; back text that's too long, 200 with a warning; back text that's too deep, 422.
4. **Print check:** slice the STL from the end-to-end run.

## Open decisions
- **Which `options` fields users may set,** and which the Go backend fixes. This is decided on the Go side; the service accepts any valid option.
- **R2 object key layout and lifecycle,** for example deleting orphaned uploads after N days if a request fails partway through.
- **Whether to gzip STLs before upload.** STLs compress about 8.5×. It would need a `Content-Encoding: gzip` header in both the presigned signature and the upload.
- **ARM servers:** lib3mf has no ARM Linux wheel. Running on ARM would mean making 3MF optional.
