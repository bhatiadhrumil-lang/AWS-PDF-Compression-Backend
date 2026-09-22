"""Record pipeline: decode -> validate -> download -> verify -> compress -> upload.

One S3 event may carry several Records; each is processed independently and
a single bad record never fails the whole batch. Duplicate keys inside one
event are processed once.
"""
import os
import traceback

from common import s3 as s3_client
from common.cleanup import temp_workdir
from common.filenames import decode_s3_key, has_pdf_extension, output_key_for
from common.validation import size_ok, validate_local_pdf
from config import from_env
from operations.compress import compress_pdf


def _record_bucket_key(record):
    s3info = (record or {}).get("s3", {})
    bucket = s3info.get("bucket", {}).get("name")
    raw_key = s3info.get("object", {}).get("key")
    event_size = s3info.get("object", {}).get("size")
    return bucket, raw_key, event_size


def process_record(record, cfg=None):
    """Process one S3 event record. Returns a result dict (never raises)."""
    cfg = cfg or from_env()
    bucket, raw_key, event_size = _record_bucket_key(record)
    key = decode_s3_key(raw_key)

    def skipped(reason):
        print("SKIP key=%r reason=%s" % (key, reason))
        return {"status": "skipped", "key": key, "reason": reason}

    def failed(reason):
        print("FAIL key=%r reason=%s" % (key, reason))
        return {"status": "failed", "key": key, "reason": reason}

    if not bucket or not key:
        return skipped("missing bucket or key in record")
    if not has_pdf_extension(key):
        return skipped("not a .pdf object")

    if event_size is not None:
        ok, reason = size_ok(event_size, cfg.max_file_size_mb)
        if not ok:
            return skipped(reason)
    else:
        head_size = s3_client.head_object_size(bucket, key)
        ok, reason = size_ok(head_size, cfg.max_file_size_mb)
        if not ok:
            return skipped(reason)

    try:
        with temp_workdir(prefix="pdf-compress-") as workdir:
            input_file = os.path.join(workdir, "input.pdf")
            output_file = os.path.join(workdir, "compressed.pdf")

            print("Downloading s3://%s/%s" % (bucket, key))
            s3_client.download_file(bucket, key, input_file)

            ok, reason = validate_local_pdf(input_file, cfg.max_file_size_mb)
            if not ok:
                return failed(reason)

            print("Compressing %r" % key)
            compress_pdf(input_file, output_file)

            out_key = output_key_for(key)
            print("Uploading s3://%s/%s" % (cfg.output_bucket, out_key))
            s3_client.upload_file(output_file, cfg.output_bucket, out_key)
    except Exception as exc:  # per-record isolation: never raise
        traceback.print_exc()
        return failed(str(exc))

    print("Done key=%r -> %r" % (key, out_key))
    return {"status": "ok", "key": key, "output_key": out_key}


def handle_event(event, cfg=None):
    """Process every record in the event. Returns {"results": [...]}."""
    cfg = cfg or from_env()
    records = (event or {}).get("Records", []) or []
    print("Received %d record(s)" % len(records))
    results = []
    seen = set()
    for record in records:
        _, raw_key, _ = _record_bucket_key(record)
        dedupe = decode_s3_key(raw_key)
        if dedupe and dedupe in seen:
            print("SKIP duplicate key=%r" % dedupe)
            results.append({"status": "skipped", "key": dedupe, "reason": "duplicate in event"})
            continue
        if dedupe:
            seen.add(dedupe)
        results.append(process_record(record, cfg))
    return {"results": results}
