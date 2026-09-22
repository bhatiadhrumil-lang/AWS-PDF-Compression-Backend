# AWS PDF Processing Backend

Official backend source of truth for the PDF platform's server-side processing.
Today: **PDF compression**, **PDF merge**, and **PDF split** — all three
implemented, deployed, and verified end-to-end on AWS. More operations will
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
 └── *.split.json      (SplitPDF notification, prefix split-requests/)
       → same Lambda `pdf-compressor`
       → src/handler.py :: process_split_record
       → pypdf split in src/operations/split.py (all | ranges)
       → S3 output bucket (split/<request-id>/<stem>-split.zip)
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
    split.py             pypdf split (one input -> ZIP of PDFs)
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
* ECR: `868942372673.dkr.ecr.us-east-2.amazonaws.com/pdf-compressor:latest`
  (currently the split image
  `sha256:49809a4ec746e3a87df4245fe0a361f8f7ef6a65de5474fff844e2f1e845d343`;
  Lambda `CodeSha256` confirms it).
* Input bucket: `pdf-compressor-input-868942372673` with three notifications
  on the same Lambda: `CompressPDF` (`ObjectCreated:*`, suffix `.pdf`),
  `MergePDF` (`ObjectCreated:*`, suffix `.merge.json`), and `SplitPDF`
  (`ObjectCreated:*`, suffix `.split.json`).
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
  (compression), `ObjectCreated:*` with `.merge.json` suffix filter →
  same Lambda (merge), and `ObjectCreated:*` with `.split.json` suffix
  filter → same Lambda (split).
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

Status: Backend implemented + deployed.

Multi-file operation: N ordered input PDFs → one `merged-<name>.pdf`
output, merged with pypdf in exactly the requested order.

### Split PDF

Status: Backend implemented + deployed + verified end-to-end on AWS
(split-all and ranges modes, ZIP contents validated, compress + merge
regressions green).

Single-file, multi-output operation: one source PDF + manifest →
one `split/<request-id>/<stem>-split.zip` containing the requested parts.
Two modes: `all` (one PDF per page: `<stem>-page-<n>.pdf`) and `ranges`
(one PDF per requested range in requested order: `<stem>-page-5.pdf`,
`<stem>-pages-1-3.pdf`). Split reuses the manifest pattern because it
needs parameters (mode/ranges) that a bare S3 upload cannot express.

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
`process_merge_record`, `*.split.json` keys to `process_split_record`,
and `*.pdf` keys to the untouched compress
pipeline; anything else is skipped. A merge/split manifest never enters the
compress path (it is not a `.pdf`) and PDFs never enter the merge/split path.

## Split request contract

Manifest `split-requests/<request-id>.split.json`, uploaded AFTER the
source PDF (same input bucket):

```json
{ "operation": "split", "input": "uploads/<request-id>/document.pdf",
  "mode": "all", "output_name": "document" }
```

```json
{ "operation": "split", "input": "uploads/<request-id>/document.pdf",
  "mode": "ranges", "ranges": ["1-3", "5", "8-10"],
  "output_name": "document" }
```

* `operation` optional (`.split.json` suffix implies split; if present must
  equal `"split"`).
* `input` (required): one plain (decoded) S3 key, same bucket; URL-encoded
  accepted. `.pdf` extension, size cap (`MAX_FILE_SIZE_MB`), `%PDF-` magic,
  non-empty pages, not encrypted — same checks as merge inputs.
* `mode` (default `"all"`): `"ranges"` requires a non-empty `ranges` list;
  `ranges` with `"all"` is rejected (never silently ignored).
* `ranges`: list of `"5"` / `"1-3"` tokens (comma-joined strings also split;
  whitespace tolerated). Rejected: page 0, negatives, empty/malformed
  tokens, reversed (`5-2`), pages beyond the document, duplicate or
  overlapping ranges (each source page at most once), more than
  `SPLIT_MAX_RANGES` ranges. Order is preserved exactly.
* `output_name` (optional stem): sanitized (traversal/controls stripped,
  trailing `.pdf`/`.zip` removed, spaces/unicode kept); falls back to the
  request id.

## Split output

One ZIP at `split/<request-id>/<stem>-split.zip` (request id always
embedded, so the frontend polls this EXACT key — no prefix listing, no
cross-user collisions). ZIP is flat (no directories): mode `all` packs
`<stem>-page-1.pdf` … `<stem>-page-N.pdf`; mode `ranges` packs one file
per requested range in requested order. ZIP creation uses stdlib
`zipfile` (deflated).

## Split limits

| Variable | Default | Purpose |
|---|---|---|
| `SPLIT_MAX_RANGES` | `50` | max page ranges per split request |
| `SPLIT_MAX_OUTPUTS` | `200` | max PDFs generated per split request |
| `MAX_FILE_SIZE_MB` | `100` | max source PDF size (shared) |

Rationale: each range becomes one output PDF in the ZIP; the caps keep the
manifest small, /tmp usage (source + parts + ZIP) inside 512 MB, and the
job inside the 300 s timeout. All env-overridable.

Result shape: `{"status": "ok"|"failed", "key": manifest, "operation":
"split", "output_key": ..., "reason": ...}`. Covered failures: missing /
malformed manifest, wrong operation, missing input key or S3 object,
invalid/encrypted PDF, bad mode, invalid/out-of-bounds/overlapping ranges,
too many ranges or outputs, ZIP failure, upload failure. User-safe reasons;
tracebacks to CloudWatch only.

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
| `SPLIT_MAX_RANGES` | no | `50` | max page ranges per split request |
| `SPLIT_MAX_OUTPUTS` | no | `200` | max PDFs generated per split request |
| `TMP_DIR` | no | system temp | temp workdir base |
| `GHOSTSCRIPT_BIN` | no | `gs` | gs binary override (tests/CI) |
| `S3_ENDPOINT_URL` | no | — | S3-compatible endpoint override (local dev) |

## Testing

```bash
python3 -m unittest discover -s tests -v   # 165 tests, no AWS credentials needed
python3 -m py_compile src/app.py src/handler.py src/config.py \
  src/operations/compress.py src/operations/merge.py \
  src/operations/split.py src/common/*.py
```

Covers: key decoding (spaces/parens/brackets/`+`/`&`/unicode/`%`), `.pdf` case
variants, output naming, multi-record + dedupe + batch isolation, invalid and
oversized input, temp-dir isolation/cleanup, config defaults/validation,
entry-point success/error shape, plus merge: 2-file and 4-file merges, order
preservation, special/unicode/URL-encoded filenames, invalid/missing/oversize
inputs, count + total-size limits, per-request temp cleanup, output-name
sanitization, manifest routing, merge config defaults, plus split:
split-all (1-page and multi-page), single/span/multi ranges in requested
order, range-parser rejects (0, beyond-count, reversed, malformed, empty,
duplicate/overlap), special/unicode/traversal filenames, invalid/missing
inputs, ZIP contents and part validity, cleanup, split routing and config.

## Docker

Base `public.ecr.aws/lambda/python:3.12` (x86_64) + `ghostscript` via `dnf`,
`boto3` + `pypdf` via pip, `CMD ["app.lambda_handler"]`. pypdf (pure Python,
maintained) does merge concatenation; Ghostscript settings are untouched.

## Future Architecture

`src/operations/` now hosts `compress.py`, `merge.py`, and `split.py`.
Future `rotate.py`, `extract.py`, `convert.py`, `watermark.py`, `protect.py`,
plus editor flattening follow the same pattern, reusing `common/` (s3,
filenames, validation, cleanup) — parameterized ones reuse the
manifest-request pattern.

## Cost

Stays on S3 + Lambda + ECR only. No RDS/ECS/DynamoDB/servers. ECR lifecycle
tip: keep `:latest` + one fallback, expire untagged images.
