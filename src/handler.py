"""Record pipeline: decode -> validate -> download -> verify -> compress -> upload.

One S3 event may carry several Records; each is processed independently and
a single bad record never fails the whole batch. Duplicate keys inside one
event are processed once.
"""
import os
import traceback

from common import s3 as s3_client
from common.cleanup import temp_workdir
from common.filenames import (
    ROTATE_MANIFEST_SUFFIX,
    SPLIT_MANIFEST_SUFFIX,
    decode_s3_key,
    has_pdf_extension,
    is_merge_manifest_key,
    is_rotate_manifest_key,
    is_split_manifest_key,
    merged_output_key_for,
    output_key_for,
    request_id_from_manifest,
    rotate_output_key_for,
    sanitize_split_stem,
    split_output_key_for,
)
from common.validation import size_ok, validate_local_pdf
from config import from_env
from operations.compress import compress_pdf
from operations.merge import (
    MergeError,
    check_merge_counts,
    check_merge_total_size,
    load_merge_request,
    merge_pdfs,
    validate_merge_input_file,
    validate_merge_input_key,
)
from operations.split import (
    SplitError,
    check_split_output_count,
    load_split_request,
    make_split_zip,
    page_count_of,
    resolve_ranges,
    split_pdf,
    validate_split_input_key,
)
from operations.rotate import (
    RotateError,
    load_rotate_request,
    page_count_of as rotate_page_count_of,
    resolve_pages,
    rotate_pdf,
    validate_rotate_input_key,
)


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
    if is_merge_manifest_key(key):
        return process_merge_record(bucket, key, cfg)
    if is_split_manifest_key(key):
        return process_split_record(bucket, key, cfg)
    if is_rotate_manifest_key(key):
        return process_rotate_record(bucket, key, cfg)
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


def process_merge_record(bucket, manifest_key, cfg=None):
    """Process one merge-request manifest. Returns a result dict (never raises).

    Downloads the manifest, then each input IN ORDER into a unique temp
    workdir (removed on success and failure), validates every input,
    merges, and uploads a single "merged-<name>.pdf" object.
    """
    cfg = cfg or from_env()

    def failed(reason):
        print("MERGE FAIL manifest=%r reason=%s" % (manifest_key, reason))
        return {"status": "failed", "key": manifest_key,
                "operation": "merge", "reason": reason}

    try:
        with temp_workdir(prefix="pdf-merge-") as workdir:
            manifest_file = os.path.join(workdir, "request.merge.json")
            print("Downloading merge request s3://%s/%s" % (bucket, manifest_key))
            try:
                s3_client.download_file(bucket, manifest_key, manifest_file)
            except Exception as exc:
                return failed("cannot download merge request: %s" % exc)
            try:
                request = load_merge_request(manifest_file)
            except MergeError as exc:
                return failed(str(exc))

            inputs = request["inputs"]
            try:
                check_merge_counts(inputs, cfg.merge_max_files)
            except MergeError as exc:
                return failed(str(exc))

            # Pre-download: reject known-oversize inputs and known-over-budget
            # totals early (unknown sizes are enforced post-download).
            known_sizes = []
            for key in inputs:
                try:
                    validate_merge_input_key(key, cfg.max_file_size_mb)
                except MergeError as exc:
                    return failed(str(exc))
                size = s3_client.head_object_size(bucket, key)
                if size is not None:
                    ok, reason = size_ok(size, cfg.max_file_size_mb)
                    if not ok:
                        return failed("input %r %s" % (key, reason))
                known_sizes.append(size)
            try:
                check_merge_total_size(known_sizes, cfg.merge_max_total_mb)
            except MergeError as exc:
                return failed(str(exc))

            # Download every input in manifest order, validating as we go.
            local_files = []
            running_total = 0
            total_limit = int(cfg.merge_max_total_mb) * 1024 * 1024
            for index, key in enumerate(inputs):
                local_path = os.path.join(workdir, "input-%04d.pdf" % index)
                print("Downloading merge input %d/%d s3://%s/%s"
                      % (index + 1, len(inputs), bucket, key))
                try:
                    s3_client.download_file(bucket, key, local_path)
                except Exception as exc:
                    return failed("cannot download input %r: %s" % (key, exc))
                try:
                    size = validate_merge_input_file(
                        local_path, key, cfg.max_file_size_mb)
                except MergeError as exc:
                    return failed(str(exc))
                running_total += size
                if running_total > total_limit:
                    return failed(
                        "merge inputs total %d bytes, exceeds %d MB limit"
                        % (running_total, cfg.merge_max_total_mb))
                local_files.append(local_path)

            merged_file = os.path.join(workdir, "merged.pdf")
            print("Merging %d file(s) for manifest=%r"
                  % (len(local_files), manifest_key))
            try:
                merge_pdfs(local_files, merged_file)
            except MergeError as exc:
                return failed(str(exc))

            out_key = merged_output_key_for(request["output_name"], manifest_key)
            print("Uploading s3://%s/%s" % (cfg.output_bucket, out_key))
            try:
                s3_client.upload_file(merged_file, cfg.output_bucket, out_key)
            except Exception as exc:
                return failed("cannot upload merged PDF: %s" % exc)
    except Exception as exc:  # per-record isolation: never raise
        traceback.print_exc()
        return failed(str(exc))

    print("Merge done manifest=%r -> %r" % (manifest_key, out_key))
    return {"status": "ok", "key": manifest_key, "operation": "merge",
            "output_key": out_key}


def process_split_record(bucket, manifest_key, cfg=None):
    """Process one split-request manifest. Returns a result dict (never raises).

    Downloads the manifest, then the single source PDF, into a unique temp
    workdir (removed on success and failure), validates, splits per the
    requested mode, packs the parts flat into one ZIP, and uploads it to
    "split/<request-id>/<stem>-split.zip".
    """
    cfg = cfg or from_env()

    def failed(reason):
        print("SPLIT FAIL manifest=%r reason=%s" % (manifest_key, reason))
        return {"status": "failed", "key": manifest_key,
                "operation": "split", "reason": reason}

    try:
        with temp_workdir(prefix="pdf-split-") as workdir:
            manifest_file = os.path.join(workdir, "request.split.json")
            print("Downloading split request s3://%s/%s" % (bucket, manifest_key))
            try:
                s3_client.download_file(bucket, manifest_key, manifest_file)
            except Exception as exc:
                return failed("cannot download split request: %s" % exc)
            try:
                request = load_split_request(manifest_file)
            except SplitError as exc:
                return failed(str(exc))

            source_key = request["input"]
            try:
                validate_split_input_key(source_key)
            except SplitError as exc:
                return failed(str(exc))
            head_size = s3_client.head_object_size(bucket, source_key)
            if head_size is not None:
                ok, reason = size_ok(head_size, cfg.max_file_size_mb)
                if not ok:
                    return failed("input %r %s" % (source_key, reason))

            local_input = os.path.join(workdir, "input.pdf")
            print("Downloading split input s3://%s/%s" % (bucket, source_key))
            try:
                s3_client.download_file(bucket, source_key, local_input)
            except Exception as exc:
                return failed("cannot download input %r: %s" % (source_key, exc))
            ok, reason = validate_local_pdf(local_input, cfg.max_file_size_mb)
            if not ok:
                return failed("input %r %s" % (source_key, reason))

            try:
                page_count = page_count_of(local_input, source_key)
            except SplitError as exc:
                return failed(str(exc))

            if request["mode"] == "all":
                try:
                    check_split_output_count(page_count, cfg.split_max_outputs)
                except SplitError as exc:
                    return failed(str(exc))
                targets = None
            else:
                try:
                    targets = resolve_ranges(
                        request["ranges"], page_count, cfg.split_max_ranges)
                    check_split_output_count(len(targets), cfg.split_max_outputs)
                except SplitError as exc:
                    return failed(str(exc))

            stem = (sanitize_split_stem(request["output_name"])
                    or sanitize_split_stem(
                        request_id_from_manifest(
                            manifest_key, SPLIT_MANIFEST_SUFFIX))
                    or "document")
            parts_dir = os.path.join(workdir, "parts")
            os.mkdir(parts_dir)
            print("Splitting %d page(s) mode=%s for manifest=%r"
                  % (page_count, request["mode"], manifest_key))
            try:
                parts = split_pdf(
                    local_input, source_key, targets, parts_dir, stem)
                zip_path = os.path.join(workdir, "split.zip")
                make_split_zip(parts, zip_path)
            except SplitError as exc:
                return failed(str(exc))

            out_key = split_output_key_for(request["output_name"], manifest_key)
            print("Uploading s3://%s/%s" % (cfg.output_bucket, out_key))
            try:
                s3_client.upload_file(zip_path, cfg.output_bucket, out_key)
            except Exception as exc:
                return failed("cannot upload split ZIP: %s" % exc)
    except Exception as exc:  # per-record isolation: never raise
        traceback.print_exc()
        return failed(str(exc))

    print("Split done manifest=%r -> %r" % (manifest_key, out_key))
    return {"status": "ok", "key": manifest_key, "operation": "split",
            "output_key": out_key}


def process_rotate_record(bucket, manifest_key, cfg=None):
    """Process one rotate-request manifest. Returns a result dict (never raises).

    Downloads the manifest, then the single source PDF, into a unique temp
    workdir (removed on success and failure), validates, rotates the
    selected pages (or all), and uploads a single
    "rotate/<request-id>/<stem>-rotated.pdf" object.
    """
    cfg = cfg or from_env()

    def failed(reason):
        print("ROTATE FAIL manifest=%r reason=%s" % (manifest_key, reason))
        return {"status": "failed", "key": manifest_key,
                "operation": "rotate", "reason": reason}

    try:
        with temp_workdir(prefix="pdf-rotate-") as workdir:
            manifest_file = os.path.join(workdir, "request.rotate.json")
            print("Downloading rotate request s3://%s/%s" % (bucket, manifest_key))
            try:
                s3_client.download_file(bucket, manifest_key, manifest_file)
            except Exception as exc:
                return failed("cannot download rotate request: %s" % exc)
            try:
                request = load_rotate_request(manifest_file)
            except RotateError as exc:
                return failed(str(exc))

            source_key = request["input"]
            try:
                validate_rotate_input_key(source_key)
            except RotateError as exc:
                return failed(str(exc))
            head_size = s3_client.head_object_size(bucket, source_key)
            if head_size is not None:
                ok, reason = size_ok(head_size, cfg.max_file_size_mb)
                if not ok:
                    return failed("input %r %s" % (source_key, reason))

            local_input = os.path.join(workdir, "input.pdf")
            print("Downloading rotate input s3://%s/%s" % (bucket, source_key))
            try:
                s3_client.download_file(bucket, source_key, local_input)
            except Exception as exc:
                return failed("cannot download input %r: %s" % (source_key, exc))
            ok, reason = validate_local_pdf(local_input, cfg.max_file_size_mb)
            if not ok:
                return failed("input %r %s" % (source_key, reason))

            try:
                page_count = rotate_page_count_of(local_input, source_key)
            except RotateError as exc:
                return failed(str(exc))

            if request["pages"] == "all":
                selected = None
            else:
                try:
                    # Reuses the split range cap: rotation is lightweight, so
                    # no new knob — the cap is about manifest sanity.
                    selected = resolve_pages(
                        request["pages"], page_count, cfg.split_max_ranges)
                except RotateError as exc:
                    return failed(str(exc))

            rotated_file = os.path.join(workdir, "rotated.pdf")
            print("Rotating %d page(s) by %d for manifest=%r"
                  % (page_count, request["rotation"], manifest_key))
            try:
                rotate_pdf(local_input, source_key, request["rotation"],
                           selected, rotated_file)
            except RotateError as exc:
                return failed(str(exc))

            out_key = rotate_output_key_for(request["output_name"], manifest_key)
            print("Uploading s3://%s/%s" % (cfg.output_bucket, out_key))
            try:
                s3_client.upload_file(rotated_file, cfg.output_bucket, out_key)
            except Exception as exc:
                return failed("cannot upload rotated PDF: %s" % exc)
    except Exception as exc:  # per-record isolation: never raise
        traceback.print_exc()
        return failed(str(exc))

    print("Rotate done manifest=%r -> %r" % (manifest_key, out_key))
    return {"status": "ok", "key": manifest_key, "operation": "rotate",
            "output_key": out_key}


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
