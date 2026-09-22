"""Thin S3 access layer. boto3 is imported lazily so unit tests can import
this package without AWS dependencies installed."""
import os


def _client():
    import boto3  # deferred: keeps `import common.s3` credential-free

    endpoint = os.environ.get("S3_ENDPOINT_URL") or None
    if endpoint:
        return boto3.client("s3", endpoint_url=endpoint)
    return boto3.client("s3")


def head_object_size(bucket, key):
    """Return object size in bytes, or None if unknown/unavailable."""
    try:
        resp = _client().head_object(Bucket=bucket, Key=key)
        size = resp.get("ContentLength")
        return int(size) if size is not None else None
    except Exception:
        return None


def download_file(bucket, key, destination):
    _client().download_file(bucket, key, destination)


def upload_file(source, bucket, key):
    _client().upload_file(source, bucket, key)
