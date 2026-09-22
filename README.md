# AWS PDF Processing Backend

Official backend source of truth for the PDF platform's server-side processing.
Today: **PDF compression**. More operations (merge, split, …) will plug into
`src/operations/` later — none are implemented yet.

> Image convention: `pdf-compressor:latest` (ECR → Lambda). No `v1`/`v2`/`v3`
> tags, no versioned paths. The frontend repo is separate and untouched.

## Architecture

```text
S3 input bucket  (ObjectCreated, *.pdf)
  → Lambda `pdf-compressor` (container image :latest, 1024 MB, 300 s, us-east-2)
  → src/app.py :: lambda_handler
  → src/handler.py :: per-record pipeline
  → Ghostscript (pdfwrite, /ebook) in src/operations/compress.py
  → S3 output bucket
  → frontend polls HeadObject, downloads via presigned URL
```

```text
src/
  app.py                 Lambda entry (CMD ["app.lambda_handler"])
  handler.py             multi-record loop + per-record pipeline
  config.py              env config (no secrets)
  operations/
    compress.py          Ghostscript compression (settings unchanged)
  common/
    s3.py                head/download/upload (lazy boto3 import)
    filenames.py         key decoding, .pdf detection, output naming
    validation.py        size limits, %PDF- magic check
    cleanup.py           per-record temp workdirs
tests/                   stdlib unittest, all AWS calls mocked
Dockerfile               lambda/python:3.12 + ghostscript (unchanged)
requirements.txt         boto3 only (dead deps removed, see below)
```

## AWS Region

`us-east-2` (buckets, Lambda, ECR, Cognito all live there).

## ECR

`pdf-compressor:latest`. Build/push is manual for now — **do not deploy from
this task**; deployment happens after review.

## Input

* Bucket: `pdf-compressor-input-868942372673` (env `INPUT_BUCKET`).
* Trigger: S3 `ObjectCreated:*` with `.pdf` suffix filter → Lambda.
* The handler accepts `.pdf` / `.PDF` / `.Pdf` (case-insensitive) even though
  the bucket filter itself is case-sensitive.

## Output

* Bucket: `pdf-compressor-output-868942372673` (env `OUTPUT_BUCKET`).
* **Naming contract (load-bearing, frontend polls it):**
  `compressed-<basename of decoded input key>`.
  Example: input `uploads/xyz_My Report (Final).pdf` →
  output `compressed-xyz_My Report (Final).pdf`.

## Filename Handling

S3 event keys are URL-encoded (`" "` → `+`, `(` → `%28`, unicode → `%XX`).
`decode_s3_key()` (`unquote_plus`) decodes before **every** S3 call — the old
code used the raw key, so any file with parentheses/brackets/`+`/`&`/unicode
failed to download (and the output key never matched frontend polling).
Supported: spaces, parentheses, brackets, `+`, `&`, `=`, `%`, unicode.
Display names are the frontend's job; keys here are always the true S3 keys.

## Validation & Limits

* Extension check (case-insensitive) → skip non-PDFs without failing the batch.
* Size: event `size` if present, else `HeadObject`; rejects input over
  `MAX_FILE_SIZE_MB` (default **100**, env-overridable). Rationale: 512 MB of
  `/tmp` must hold input + output together, plus a 300 s timeout — 100 MB keeps
  typical Ghostscript runs inside both. Mirrors the frontend limit.
* Content: `%PDF-` magic bytes verified after download (extension is not trusted).
* One bad record never fails the batch; duplicates within an event run once.

## Temp Files

Each record gets a unique `mkdtemp` workdir, removed in a `finally`
(`common/cleanup.py`). Warm-container reuse can no longer leak or collide.

## Security

* No secrets/keys in source (IAM roles supply everything; verified by test run
  with zero AWS credentials — suite is fully mocked).
* Least privilege preserved: Lambda role = Get/List on input, Put on output,
  basic execution logs. Buckets stay private; the frontend uses short-lived
  Cognito credentials + 5-minute presigned downloads.
* See `tests/` for the executable form of these guarantees.

## Configuration

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `INPUT_BUCKET` | yes | — | event source bucket |
| `OUTPUT_BUCKET` | yes | — | compressed output bucket |
| `MAX_FILE_SIZE_MB` | no | `100` | input size cap |
| `TMP_DIR` | no | system temp | temp workdir base |
| `GHOSTSCRIPT_BIN` | no | `gs` | gs binary override (tests/CI) |
| `S3_ENDPOINT_URL` | no | — | S3-compatible endpoint override (local dev) |

## Testing

```bash
python3 -m unittest discover -s tests -v   # 48 tests, no AWS credentials needed
python3 -m py_compile src/app.py src/handler.py src/config.py \
  src/operations/compress.py src/common/*.py
```

Covers: key decoding (spaces/parens/brackets/`+`/`&`/unicode/`%`), `.pdf` case
variants, output naming, multi-record + dedupe + batch isolation, invalid and
oversized input, temp-dir isolation/cleanup, config defaults/validation,
entry-point success/error shape.

## Docker

Base `public.ecr.aws/lambda/python:3.12` (x86_64) + `ghostscript` via `dnf`,
`boto3` via pip, `CMD ["app.lambda_handler"]`. Unchanged from the proven
working image except the trimmed `requirements.txt`.

## Future Architecture

`src/operations/` will host `merge.py`, `split.py`, `rotate.py`, `extract.py`,
`convert.py`, `watermark.py`, `protect.py`, plus editor flattening — each
reusing `common/` (s3, filenames, validation, cleanup). Claimed by no code yet.

## Cost

Stays on S3 + Lambda + ECR only. No RDS/ECS/DynamoDB/servers. ECR lifecycle
tip: keep `:latest` + one fallback, expire untagged images.
