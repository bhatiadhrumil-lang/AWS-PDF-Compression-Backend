# AWS PDF Processing Backend

Official backend source of truth for the PDF platform's server-side processing.
Today: **PDF compression** and **PDF merge**. More operations (split, …) will
plug into `src/operations/` later.

> Image convention: `pdf-compressor:latest` (ECR → Lambda). No `v1`/`v2`/`v3`
> tags, no versioned paths. The frontend repo is separate and untouched.

## Architecture

```text
S3 input bucket  (ObjectCreated)
 ├── *.pdf             (CompressPDF notification)
 │     → Lambda `pdf-compressor` (container image :latest, 1024 MB, 300 s, us-east-2)
 │     → src/app.py :: lambda_handler
 │     → src/handler.py :: per-record pipeline
 │     → Ghostscript (pdfwrite, /ebook) in src/operations/compress.py
 │     → S3 output bucket (compressed-<basename>)
 └── *.merge.json      (MergePDF notification, prefix merge-requests/)
       → same Lambda `pdf-compressor`
       → src/handler.py :: process_merge_record
       → pypdf merge in src/operations/merge.py (inputs in manifest order)
       → S3 output bucket (merged-<name>.pdf)
→ frontend polls HeadObject, downloads via presigned URL
```

```text
src/
  app.py                 Lambda entry (CMD ["app.lambda_handler"])
  handler.py             multi-record loop + per-record pipeline + op routing
  config.py              env config (no secrets)
  operations/
    compress.py          Ghostscript compression (settings unchanged)
    merge.py             pypdf merge (multi-file -> one output)
  common/
    s3.py                head/download/upload (lazy boto3 import)
    filenames.py         key decoding, .pdf detection, output naming
    validation.py        size limits, %PDF- magic check
    cleanup.py           per-record temp workdirs
tests/                   stdlib unittest, all AWS calls mocked
Dockerfile               lambda/python:3.12 + ghostscript (unchanged)
requirements.txt         boto3 + pypdf
```

## AWS Region

`us-east-2` (buckets, Lambda, ECR, Cognito all live there).

## ECR

`pdf-compressor:latest` (repository `pdf-compressor`, x86_64, us-east-2).
Lambda runs the `:latest` image by digest; after pushing a new `:latest`,
update the function code and confirm `CodeSha256` matches the new digest —
`latest` does not auto-deploy. The previous image stays addressable by its
digest for rollback (e.g. pre-merge image
`sha256:13904bef51b69b993ee4ae0abb3f0d103edf6ee5520b9b5b8b1527247e31b099`).

## Deployed resources (us-east-2, account 868942372673)

* Lambda: `pdf-compressor` (Image/x86_64, 1024 MB, 300 s, 512 MB `/tmp`,
  role `pdf-compressor-lambda-role`, env `INPUT_BUCKET` /
  `OUTPUT_BUCKET` only).
* ECR: `868942372673.dkr.ecr.us-east-2.amazonaws.com/pdf-compressor:latest`.
* Input bucket: `pdf-compressor-input-868942372673` with two notifications
  on the same Lambda: `CompressPDF` (`ObjectCreated:*`, suffix `.pdf`) and
  `MergePDF` (`ObjectCreated:*`, suffix `.merge.json`).
* Output bucket: `pdf-compressor-output-868942372673`.
* Lambda execution role (`S3Access` inline): Get/List on the input bucket,
  Put on the output bucket — already covers manifests + merge inputs, so no
  IAM change was needed for merge. S3→Lambda invoke permission is
  bucket-scoped, so the new notification needed no permission change either.
* Memory headroom: a 3-file merge peaked at ~136 MB of 1024 MB; no memory,
  timeout, or storage changes were required.
* Cognito guest roles (`Cognito_pdf-compressor-identity-poolUnauth_Role`,
  `PDFCompressorGuestRole`): `PutObject` on `input-bucket/*` (covers
  `uploads/*` and `merge-requests/*`) + Get/List on the output bucket —
  compatible with the frontend merge flow, no change needed.

## Input

* Bucket: `pdf-compressor-input-868942372673` (env `INPUT_BUCKET`).
* Triggers: S3 `ObjectCreated:*` with `.pdf` suffix filter → Lambda
  (compression), and `ObjectCreated:*` with `.merge.json` suffix filter →
  same Lambda (merge).
* The handler accepts `.pdf` / `.PDF` / `.Pdf` (case-insensitive) even though
  the bucket filter itself is case-sensitive.

## Output

* Bucket: `pdf-compressor-output-868942372673` (env `OUTPUT_BUCKET`).
* **Naming contract (load-bearing, frontend polls it):**
  `compressed-<basename of decoded input key>`.
  Example: input `uploads/xyz_My Report (Final).pdf` →
  output `compressed-xyz_My Report (Final).pdf`.

## PDF Operations

### Compress PDF

Status: Implemented (unchanged).

Single-file operation: one `.pdf` upload → one `compressed-<basename>`
output. See Input/Output sections below.

### Merge PDF

Status: Backend implemented.

Multi-file operation: N ordered input PDFs → one `merged-<name>.pdf`
output, merged with pypdf in exactly the requested order.

## Single-file vs multi-file operations

Compression is naturally ONE INPUT → ONE OUTPUT, which fits the S3
`ObjectCreated`-per-file trigger directly: each uploaded `.pdf` is one
record, processed independently.

Merge is MULTIPLE INPUTS → ONE OUTPUT, which does **not** fit the
per-file model: N uploads would fire N independent Lambda invocations with
no ordering and race conditions (whichever file lands last "wins", partial
sets merge, duplicates, no clean error surface).

Chosen design: a **manifest/request JSON object in S3** (no new service,
no queue, no database — stays on S3 + Lambda + ECR):

1. The frontend uploads the N PDFs normally to the input bucket
   (e.g. `uploads/<request-id>/A.pdf`, `B.pdf`, …).
2. The frontend uploads one manifest last:
   `merge-requests/<request-id>.merge.json`:

```json
{
  "operation": "merge",
  "inputs": [
    "uploads/<request-id>/A.pdf",
    "uploads/<request-id>/B.pdf",
    "uploads/<request-id>/C.pdf"
  ],
  "output_name": "combined.pdf"
}
```

3. That single manifest upload fires ONE Lambda invocation, which
   downloads the inputs **in manifest order**, validates each, merges,
   and uploads one output object.
4. The frontend polls `HeadObject` on the known output key
   (`merged-combined.pdf`), same pattern as compression.

Input contract details:

* `operation` is optional; the `.merge.json` suffix already implies merge,
  but if present it must equal `"merge"`.
* `inputs` (required): 2..`MERGE_MAX_FILES` plain (decoded) S3 keys in the
  **same input bucket** as the manifest, in merge order. URL-encoded keys
  are also accepted (decoded once; decoding is idempotent for plain keys).
* `output_name` (optional): sanitized (see below); defaults to
  `merged-<request-id>.pdf` derived from the manifest key.
* Ordering is guaranteed: pages appear as A then B then C; nothing is
  sorted or reordered.

Infra note (deployed): the input bucket has an S3 `ObjectCreated:*`
notification with suffix `.merge.json` (`MergePDF`) pointing at the same
Lambda, alongside the existing `.pdf` one (`CompressPDF`). No other infra
change was needed. This manifest pattern also fits future Split/Rotate/Edit
batch operations.

Handler routing: `process_record` sends `*.merge.json` keys to
`process_merge_record` and `*.pdf` keys to the untouched compress
pipeline; anything else is skipped. A merge manifest never enters the
compress path (it is not a `.pdf`) and PDFs never enter the merge path.

## Merge limits

| Variable | Default | Purpose |
|---|---|---|
| `MERGE_MAX_FILES` | `20` | max input PDFs per merge request |
| `MERGE_MAX_TOTAL_MB` | `200` | max combined input size per request (MB) |
| `MAX_FILE_SIZE_MB` | `100` | max individual input size (shared with compression) |

Rationale: Lambda has 512 MB of `/tmp`, which must hold all inputs plus
the merged output (~2× total), so 2 × 200 = 400 MB stays inside budget
with headroom; the 300 s timeout bounds the 20-file default. All three
are env-overridable. The output is implicitly bounded by the total
(merged size ≈ sum of inputs).

## Merge validation & errors

Every input is checked before it reaches the merger: `.pdf` extension
(case-insensitive), per-file size (HeadObject upfront + post-download),
`%PDF-` magic bytes, non-empty page count, not encrypted. The first
invalid input fails the whole request — merging a silent subset would
break the ordering guarantee.

Result shape: `{"status": "ok"|"failed", "key": manifest, "operation":
"merge", "output_key": ..., "reason": ...}`. Covered failures: no inputs,
single input, too many files, invalid/non-PDF input, missing or
inaccessible S3 object, oversized file/total, merge failure, manifest
download failure, output upload failure. Reasons are user-safe strings;
tracebacks go to CloudWatch only.

## Merge output naming

`merged-<safe-basename>.pdf`, flat in the output bucket (no folder
prefix), e.g. `output_name: "Q3 Report.pdf"` →
`merged-Q3 Report.pdf`.

Sanitization: directory components are dropped (no path traversal —
`"../../etc/passwd.pdf"` → `merged-passwd.pdf`), control characters
stripped, `.pdf` extension enforced, spaces/parens/brackets/`&`/unicode
preserved so the frontend polls the exact key. Empty unusable names fall
back to the manifest id (`merge-requests/<id>.merge.json` →
`merged-<id>.pdf`).

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
| `MERGE_MAX_FILES` | no | `20` | max PDFs per merge request |
| `MERGE_MAX_TOTAL_MB` | no | `200` | max combined merge input size (MB) |
| `TMP_DIR` | no | system temp | temp workdir base |
| `GHOSTSCRIPT_BIN` | no | `gs` | gs binary override (tests/CI) |
| `S3_ENDPOINT_URL` | no | — | S3-compatible endpoint override (local dev) |

## Testing

```bash
python3 -m unittest discover -s tests -v   # 94 tests, no AWS credentials needed
python3 -m py_compile src/app.py src/handler.py src/config.py \
  src/operations/compress.py src/operations/merge.py src/common/*.py
```

Covers: key decoding (spaces/parens/brackets/`+`/`&`/unicode/`%`), `.pdf` case
variants, output naming, multi-record + dedupe + batch isolation, invalid and
oversized input, temp-dir isolation/cleanup, config defaults/validation,
entry-point success/error shape, plus merge: 2-file and 4-file merges, order
preservation, special/unicode/URL-encoded filenames, invalid/missing/oversize
inputs, count + total-size limits, per-request temp cleanup, output-name
sanitization, manifest routing, merge config defaults.

## Docker

Base `public.ecr.aws/lambda/python:3.12` (x86_64) + `ghostscript` via `dnf`,
`boto3` + `pypdf` via pip, `CMD ["app.lambda_handler"]`. pypdf (pure Python,
maintained) does merge concatenation; Ghostscript settings are untouched.

## Future Architecture

`src/operations/` now hosts `compress.py` and `merge.py`. Future `split.py`,
`rotate.py`, `extract.py`, `convert.py`, `watermark.py`, `protect.py`, plus
editor flattening follow the same pattern, reusing `common/` (s3, filenames,
validation, cleanup) — multi-file ones reuse the manifest-request pattern.

## Cost

Stays on S3 + Lambda + ECR only. No RDS/ECS/DynamoDB/servers. ECR lifecycle
tip: keep `:latest` + one fallback, expire untagged images.
