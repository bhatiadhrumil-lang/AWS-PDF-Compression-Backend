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
    DELETE_MANIFEST_SUFFIX,
    EXTRACT_MANIFEST_SUFFIX,
    JPG2PDF_MANIFEST_SUFFIX,
    PDF2JPG_MANIFEST_SUFFIX,
    ROTATE_MANIFEST_SUFFIX,
    SPLIT_MANIFEST_SUFFIX,
    decode_s3_key,
    delete_output_key_for,
    edit_output_key_for,
    extract_output_key_for,
    has_pdf_extension,
    is_delete_manifest_key,
    is_edit_manifest_key,
    is_extract_manifest_key,
    is_jpg_to_pdf_manifest_key,
    is_merge_manifest_key,
    is_pdf_to_jpg_manifest_key,
    is_rotate_manifest_key,
    is_split_manifest_key,
    jpg_to_pdf_output_key_for,
    merged_output_key_for,
    output_key_for,
    pdf_to_jpg_output_keys_for,
    request_id_from_manifest,
    rotate_output_key_for,
    sanitize_split_stem,
    split_output_key_for,
)
from common.validation import size_ok, validate_local_jpg, validate_local_pdf
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
from operations.delete_pages import (
    DeleteError,
    load_delete_request,
    page_count_of as delete_page_count_of,
    resolve_deletion,
    delete_pdf_pages,
    validate_delete_input_key,
)
from operations.extract import (
    ExtractError,
    check_extract_page_count,
    extract_pdf,
    load_extract_request,
    page_count_of as extract_page_count_of,
    resolve_extract_pages,
    validate_extract_input_key,
)
from operations.jpg_to_pdf import (
    JpgToPdfError,
    check_jpg_counts,
    check_jpg_total_size,
    image_size_of,
    jpg_images_to_pdf,
    load_jpg_to_pdf_request,
    validate_jpg_input_key,
)
from operations.pdf_to_jpg import (
    PdfToJpgError,
    check_pdf_to_jpg_page_count,
    load_pdf_to_jpg_request,
    page_count_of as pdf_to_jpg_page_count_of,
    render_pdf_pages,
    resolve_pdf_to_jpg_pages,
    validate_pdf_to_jpg_input_key,
)
from operations.edit import (
    EditError,
    apply_page_ops,
    check_edit_count,
    collect_image_sources,
    load_edit_request,
    page_info_of,
    render_edited_pdf,
    resolve_edit_pages,
    validate_edit_input_key,
    validate_image_file,
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
    if is_delete_manifest_key(key):
        return process_delete_record(bucket, key, cfg)
    if is_extract_manifest_key(key):
        return process_extract_record(bucket, key, cfg)
    if is_jpg_to_pdf_manifest_key(key):
        return process_jpg_to_pdf_record(bucket, key, cfg)
    if is_pdf_to_jpg_manifest_key(key):
        return process_pdf_to_jpg_record(bucket, key, cfg)
    if is_edit_manifest_key(key):
        return process_edit_record(bucket, key, cfg)
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


def process_delete_record(bucket, manifest_key, cfg=None):
    """Process one delete-request manifest. Returns a result dict (never raises).

    Downloads the manifest, then the single source PDF, into a unique temp
    workdir (removed on success and failure), validates, deletes the
    selected pages, and uploads a single
    "delete/<request-id>/<stem>-deleted.pdf" object. Deleting every page is
    rejected — a zero-page PDF is never produced.
    """
    cfg = cfg or from_env()

    def failed(reason):
        print("DELETE FAIL manifest=%r reason=%s" % (manifest_key, reason))
        return {"status": "failed", "key": manifest_key,
                "operation": "delete", "reason": reason}

    try:
        with temp_workdir(prefix="pdf-delete-") as workdir:
            manifest_file = os.path.join(workdir, "request.delete.json")
            print("Downloading delete request s3://%s/%s" % (bucket, manifest_key))
            try:
                s3_client.download_file(bucket, manifest_key, manifest_file)
            except Exception as exc:
                return failed("cannot download delete request: %s" % exc)
            try:
                request = load_delete_request(manifest_file)
            except DeleteError as exc:
                return failed(str(exc))

            source_key = request["input"]
            try:
                validate_delete_input_key(source_key)
            except DeleteError as exc:
                return failed(str(exc))
            head_size = s3_client.head_object_size(bucket, source_key)
            if head_size is not None:
                ok, reason = size_ok(head_size, cfg.max_file_size_mb)
                if not ok:
                    return failed("input %r %s" % (source_key, reason))

            local_input = os.path.join(workdir, "input.pdf")
            print("Downloading delete input s3://%s/%s" % (bucket, source_key))
            try:
                s3_client.download_file(bucket, source_key, local_input)
            except Exception as exc:
                return failed("cannot download input %r: %s" % (source_key, exc))
            ok, reason = validate_local_pdf(local_input, cfg.max_file_size_mb)
            if not ok:
                return failed("input %r %s" % (source_key, reason))

            try:
                page_count = delete_page_count_of(local_input, source_key)
            except DeleteError as exc:
                return failed(str(exc))

            try:
                # Reuses the split range cap: deletion is lightweight, so
                # no new knob — the cap is about manifest sanity.
                doomed = resolve_deletion(
                    request["pages"], page_count, cfg.split_max_ranges)
            except DeleteError as exc:
                return failed(str(exc))

            stem = (sanitize_split_stem(request["output_name"])
                    or sanitize_split_stem(
                        request_id_from_manifest(
                            manifest_key, DELETE_MANIFEST_SUFFIX))
                    or "document")
            deleted_file = os.path.join(workdir, "deleted.pdf")
            print("Deleting %d page(s) of %d for manifest=%r"
                  % (len(doomed), page_count, manifest_key))
            try:
                delete_pdf_pages(local_input, source_key, doomed, deleted_file)
            except DeleteError as exc:
                return failed(str(exc))

            out_key = delete_output_key_for(request["output_name"], manifest_key)
            print("Uploading s3://%s/%s" % (cfg.output_bucket, out_key))
            try:
                s3_client.upload_file(deleted_file, cfg.output_bucket, out_key)
            except Exception as exc:
                return failed("cannot upload edited PDF: %s" % exc)
    except Exception as exc:  # per-record isolation: never raise
        traceback.print_exc()
        return failed(str(exc))

    print("Delete done manifest=%r -> %r" % (manifest_key, out_key))
    return {"status": "ok", "key": manifest_key, "operation": "delete",
            "output_key": out_key}


def process_extract_record(bucket, manifest_key, cfg=None):
    """Process one extract-request manifest. Returns a result dict (never raises).

    Downloads the manifest, then the single source PDF, into a unique temp
    workdir (removed on success and failure), validates, copies the selected
    pages IN REQUESTED ORDER into a single new PDF, and uploads it to
    "extract/<request-id>/<stem>-extracted.pdf". The source PDF is never
    modified. An empty selection is rejected — a zero-page PDF is never
    produced.
    """
    cfg = cfg or from_env()

    def failed(reason):
        print("EXTRACT FAIL manifest=%r reason=%s" % (manifest_key, reason))
        return {"status": "failed", "key": manifest_key,
                "operation": "extract", "reason": reason}

    try:
        with temp_workdir(prefix="pdf-extract-") as workdir:
            manifest_file = os.path.join(workdir, "request.extract.json")
            print("Downloading extract request s3://%s/%s" % (bucket, manifest_key))
            try:
                s3_client.download_file(bucket, manifest_key, manifest_file)
            except Exception as exc:
                return failed("cannot download extract request: %s" % exc)
            try:
                request = load_extract_request(manifest_file)
            except ExtractError as exc:
                return failed(str(exc))

            source_key = request["input"]
            try:
                validate_extract_input_key(source_key)
            except ExtractError as exc:
                return failed(str(exc))
            head_size = s3_client.head_object_size(bucket, source_key)
            if head_size is not None:
                ok, reason = size_ok(head_size, cfg.max_file_size_mb)
                if not ok:
                    return failed("input %r %s" % (source_key, reason))

            local_input = os.path.join(workdir, "input.pdf")
            print("Downloading extract input s3://%s/%s" % (bucket, source_key))
            try:
                s3_client.download_file(bucket, source_key, local_input)
            except Exception as exc:
                return failed("cannot download input %r: %s" % (source_key, exc))
            ok, reason = validate_local_pdf(local_input, cfg.max_file_size_mb)
            if not ok:
                return failed("input %r %s" % (source_key, reason))

            try:
                page_count = extract_page_count_of(local_input, source_key)
            except ExtractError as exc:
                return failed(str(exc))

            try:
                pages = resolve_extract_pages(
                    request["pages"], page_count, cfg.extract_max_pages)
                check_extract_page_count(len(pages), cfg.extract_max_pages)
            except ExtractError as exc:
                return failed(str(exc))

            extracted_file = os.path.join(workdir, "extracted.pdf")
            print("Extracting %d page(s) of %d for manifest=%r"
                  % (len(pages), page_count, manifest_key))
            try:
                extract_pdf(local_input, source_key, pages, extracted_file)
            except ExtractError as exc:
                return failed(str(exc))

            out_key = extract_output_key_for(request["output_name"], manifest_key)
            print("Uploading s3://%s/%s" % (cfg.output_bucket, out_key))
            try:
                s3_client.upload_file(extracted_file, cfg.output_bucket, out_key)
            except Exception as exc:
                return failed("cannot upload extracted PDF: %s" % exc)
    except Exception as exc:  # per-record isolation: never raise
        traceback.print_exc()
        return failed(str(exc))

    print("Extract done manifest=%r -> %r" % (manifest_key, out_key))
    return {"status": "ok", "key": manifest_key, "operation": "extract",
            "output_key": out_key}


def process_jpg_to_pdf_record(bucket, manifest_key, cfg=None):
    """Process one jpg-to-pdf request manifest (never raises).

    Downloads the manifest, then each image IN MANIFEST ORDER, into a unique
    temp workdir (removed on success and failure), validates, converts the
    images into a single PDF (one image per page), and uploads it to
    "jpg-to-pdf/<request-id>/<safe-name>.pdf". Source images are never
    modified.
    """
    cfg = cfg or from_env()

    def failed(reason):
        print("JPG2PDF FAIL manifest=%r reason=%s" % (manifest_key, reason))
        return {"status": "failed", "key": manifest_key,
                "operation": "jpg_to_pdf", "reason": reason}

    try:
        with temp_workdir(prefix="pdf-jpg2pdf-") as workdir:
            manifest_file = os.path.join(workdir, "request.jpg2pdf.json")
            print("Downloading jpg to pdf request s3://%s/%s" % (bucket, manifest_key))
            try:
                s3_client.download_file(bucket, manifest_key, manifest_file)
            except Exception as exc:
                return failed("cannot download jpg to pdf request: %s" % exc)
            try:
                request = load_jpg_to_pdf_request(manifest_file)
            except JpgToPdfError as exc:
                return failed(str(exc))

            image_keys = request["images"]
            try:
                check_jpg_counts(len(image_keys), cfg.jpg2pdf_max_images)
            except JpgToPdfError as exc:
                return failed(str(exc))
            for image_key in image_keys:
                try:
                    validate_jpg_input_key(image_key)
                except JpgToPdfError as exc:
                    return failed(str(exc))
            total_bytes = 0
            for image_key in image_keys:
                head_size = s3_client.head_object_size(bucket, image_key)
                if head_size is not None:
                    ok, within = size_ok(head_size, cfg.max_file_size_mb)
                    if not ok:
                        return failed("image %r %s" % (image_key, within))
                    total_bytes += head_size
            try:
                check_jpg_total_size(total_bytes, cfg.jpg2pdf_max_total_mb)
            except JpgToPdfError as exc:
                return failed(str(exc))

            local_images = []
            for pos, image_key in enumerate(image_keys):
                local_path = os.path.join(workdir, "image-%d.jpg" % pos)
                print("Downloading jpg input s3://%s/%s" % (bucket, image_key))
                try:
                    s3_client.download_file(bucket, image_key, local_path)
                except Exception as exc:
                    return failed("cannot download image %r: %s" % (image_key, exc))
                ok, reason = validate_local_jpg(local_path, cfg.max_file_size_mb)
                if not ok:
                    return failed("image %r %s" % (image_key, reason))
                local_images.append((local_path, image_key))

            converted_file = os.path.join(workdir, "converted.pdf")
            print("Converting %d image(s) for manifest=%r"
                  % (len(local_images), manifest_key))
            try:
                jpg_images_to_pdf(local_images, converted_file)
            except JpgToPdfError as exc:
                return failed(str(exc))

            out_key = jpg_to_pdf_output_key_for(
                request["output_name"], manifest_key)
            print("Uploading s3://%s/%s" % (cfg.output_bucket, out_key))
            try:
                s3_client.upload_file(converted_file, cfg.output_bucket, out_key)
            except Exception as exc:
                return failed("cannot upload converted PDF: %s" % exc)
    except Exception as exc:  # per-record isolation: never raise
        traceback.print_exc()
        return failed(str(exc))

    print("JpgToPdf done manifest=%r -> %r" % (manifest_key, out_key))
    return {"status": "ok", "key": manifest_key, "operation": "jpg_to_pdf",
            "output_key": out_key}


def process_pdf_to_jpg_record(bucket, manifest_key, cfg=None):
    """Process one pdf-to-jpg request manifest (never raises).

    Downloads the manifest, then the single source PDF, into a unique temp
    workdir (removed on success and failure), validates, renders each
    requested page to JPG with Ghostscript, and uploads the images to
    "pdf-to-jpg/<request-id>/<stem>-page-001.jpg" (one per requested page,
    in requested order). The source PDF is never modified.
    """
    cfg = cfg or from_env()

    def failed(reason):
        print("PDF2JPG FAIL manifest=%r reason=%s" % (manifest_key, reason))
        return {"status": "failed", "key": manifest_key,
                "operation": "pdf_to_jpg", "reason": reason}

    try:
        with temp_workdir(prefix="pdf-pdf2jpg-") as workdir:
            manifest_file = os.path.join(workdir, "request.pdf2jpg.json")
            print("Downloading pdf to jpg request s3://%s/%s" % (bucket, manifest_key))
            try:
                s3_client.download_file(bucket, manifest_key, manifest_file)
            except Exception as exc:
                return failed("cannot download pdf to jpg request: %s" % exc)
            try:
                request = load_pdf_to_jpg_request(manifest_file)
            except PdfToJpgError as exc:
                return failed(str(exc))

            source_key = request["input"]
            try:
                validate_pdf_to_jpg_input_key(source_key)
            except PdfToJpgError as exc:
                return failed(str(exc))
            head_size = s3_client.head_object_size(bucket, source_key)
            if head_size is not None:
                ok, reason = size_ok(head_size, cfg.max_file_size_mb)
                if not ok:
                    return failed("input %r %s" % (source_key, reason))

            local_input = os.path.join(workdir, "input.pdf")
            print("Downloading pdf input s3://%s/%s" % (bucket, source_key))
            try:
                s3_client.download_file(bucket, source_key, local_input)
            except Exception as exc:
                return failed("cannot download input %r: %s" % (source_key, exc))
            ok, reason = validate_local_pdf(local_input, cfg.max_file_size_mb)
            if not ok:
                return failed("input %r %s" % (source_key, reason))

            try:
                page_count = pdf_to_jpg_page_count_of(local_input, source_key)
            except PdfToJpgError as exc:
                return failed(str(exc))
            try:
                pages = resolve_pdf_to_jpg_pages(
                    request["pages"], page_count, cfg.pdf2jpg_max_pages)
            except PdfToJpgError as exc:
                return failed(str(exc))

            stem = (sanitize_split_stem(request["output_name"])
                    or sanitize_split_stem(
                        request_id_from_manifest(
                            manifest_key, PDF2JPG_MANIFEST_SUFFIX))
                    or "document")
            pages_dir = os.path.join(workdir, "pages")
            os.mkdir(pages_dir)
            print("Rendering %d page(s) for manifest=%r"
                  % (len(pages), manifest_key))
            try:
                rendered = render_pdf_pages(
                    local_input, source_key, pages, request["quality"],
                    pages_dir, stem)
            except PdfToJpgError as exc:
                return failed(str(exc))

            expected_keys = pdf_to_jpg_output_keys_for(
                request["output_name"], manifest_key, pages)
            out_keys = []
            for out_key, (page_num, local_path) in zip(expected_keys, rendered):
                print("Uploading s3://%s/%s" % (cfg.output_bucket, out_key))
                try:
                    s3_client.upload_file(
                        local_path, cfg.output_bucket, out_key)
                except Exception as exc:
                    return failed(
                        "cannot upload page %d: %s" % (page_num, exc))
                out_keys.append(out_key)
    except Exception as exc:  # per-record isolation: never raise
        traceback.print_exc()
        return failed(str(exc))

    print("PdfToJpg done manifest=%r -> %d file(s)" % (manifest_key, len(out_keys)))
    return {"status": "ok", "key": manifest_key, "operation": "pdf_to_jpg",
            "output_keys": out_keys}


def apply_edit_page_ops(local_input, page_ops, workdir, source_key):
    """Apply manifest page ops, returning (working_pdf, sizes, page_count).

    Without ops this is a pass-through: (local_input, original sizes/count).
    With ops, the transformed document is written to the workdir and overlay
    page numbers resolve against the FINAL pages.
    """
    from operations.edit import apply_page_ops, page_info_of

    if not page_ops:
        sizes, count = page_info_of(local_input, source_key)
        return local_input, sizes, count
    working_input = os.path.join(workdir, "pages.pdf")
    sizes, count = apply_page_ops(
        local_input, page_ops, working_input, source_key)
    return working_input, sizes, count


def process_edit_record(bucket, manifest_key, cfg=None):
    """Process one edit-request manifest. Returns a result dict (never raises).

    Downloads the manifest, then the single source PDF plus any referenced
    overlay images, into a unique temp workdir (removed on success and
    failure), validates everything, renders the overlays, and uploads a
    single "edit/<request-id>/<stem>-edited.pdf" object.
    """
    cfg = cfg or from_env()

    def failed(reason):
        print("EDIT FAIL manifest=%r reason=%s" % (manifest_key, reason))
        return {"status": "failed", "key": manifest_key,
                "operation": "edit", "reason": reason}

    try:
        with temp_workdir(prefix="pdf-edit-") as workdir:
            manifest_file = os.path.join(workdir, "request.edit.json")
            print("Downloading edit request s3://%s/%s" % (bucket, manifest_key))
            try:
                s3_client.download_file(bucket, manifest_key, manifest_file)
            except Exception as exc:
                return failed("cannot download edit request: %s" % exc)
            try:
                request = load_edit_request(manifest_file)
            except EditError as exc:
                return failed(str(exc))
            try:
                check_edit_count(request["edits"], cfg.edit_max_edits)
            except EditError as exc:
                return failed(str(exc))

            source_key = request["input"]
            try:
                validate_edit_input_key(source_key)
            except EditError as exc:
                return failed(str(exc))
            head_size = s3_client.head_object_size(bucket, source_key)
            if head_size is not None:
                ok, reason = size_ok(head_size, cfg.max_file_size_mb)
                if not ok:
                    return failed("input %r %s" % (source_key, reason))

            local_input = os.path.join(workdir, "input.pdf")
            print("Downloading edit input s3://%s/%s" % (bucket, source_key))
            try:
                s3_client.download_file(bucket, source_key, local_input)
            except Exception as exc:
                return failed("cannot download input %r: %s" % (source_key, exc))
            ok, reason = validate_local_pdf(local_input, cfg.max_file_size_mb)
            if not ok:
                return failed("input %r %s" % (source_key, reason))

            try:
                page_sizes, page_count = page_info_of(local_input, source_key)
            except EditError as exc:
                return failed(str(exc))
            # Page operations (rotate/delete/move/insert_blank) run BEFORE
            # overlays; overlay page numbers always refer to the FINAL pages.
            # Absent "pages", this is a pass-through (working file == input).
            try:
                working_input, page_sizes, page_count = apply_edit_page_ops(
                    local_input, request["page_ops"], workdir, source_key)
            except EditError as exc:
                return failed(str(exc))
            try:
                grouped = resolve_edit_pages(request["edits"], page_sizes)
            except EditError as exc:
                return failed(str(exc))

            image_paths = {}
            for position, src_key in enumerate(
                    collect_image_sources(request["edits"])):
                local_image = os.path.join(workdir, "image-%04d" % position)
                print("Downloading edit image s3://%s/%s" % (bucket, src_key))
                try:
                    s3_client.download_file(bucket, src_key, local_image)
                except Exception as exc:
                    return failed("cannot download image %r: %s" % (src_key, exc))
                try:
                    validate_image_file(local_image, src_key, cfg.edit_max_image_mb)
                except EditError as exc:
                    return failed(str(exc))
                image_paths[src_key] = local_image

            edited_file = os.path.join(workdir, "edited.pdf")
            print("Applying %d edit(s) on %d page(s) for manifest=%r"
                  % (len(request["edits"]), page_count, manifest_key))
            try:
                render_edited_pdf(working_input, source_key, grouped,
                                  image_paths, edited_file)
            except EditError as exc:
                return failed(str(exc))

            out_key = edit_output_key_for(request["output_name"], manifest_key)
            print("Uploading s3://%s/%s" % (cfg.output_bucket, out_key))
            try:
                s3_client.upload_file(edited_file, cfg.output_bucket, out_key)
            except Exception as exc:
                return failed("cannot upload edited PDF: %s" % exc)
    except Exception as exc:  # per-record isolation: never raise
        traceback.print_exc()
        return failed(str(exc))

    print("Edit done manifest=%r -> %r" % (manifest_key, out_key))
    return {"status": "ok", "key": manifest_key, "operation": "edit",
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
