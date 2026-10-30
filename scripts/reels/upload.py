from __future__ import annotations

import os

from .config import Project
from .util import ReelsError, log, read_json, write_json


def _client(project: Project):
    try:
        import boto3  # type: ignore
    except ImportError as e:
        raise ReelsError("uploads need boto3: .venv/bin/pip install boto3") from e
    up = project.config["upload"]
    endpoint = os.environ.get("REELS_S3_ENDPOINT") or up.get("endpoint")
    bucket = os.environ.get("REELS_S3_BUCKET") or up.get("bucket")
    base = os.environ.get("REELS_PUBLIC_BASE_URL") or up.get("publicBaseUrl")
    key_id = os.environ.get("REELS_S3_ACCESS_KEY_ID")
    secret = os.environ.get("REELS_S3_SECRET_ACCESS_KEY")
    missing = [
        name
        for name, value in (
            ("upload.endpoint", endpoint),
            ("upload.bucket", bucket),
            ("upload.publicBaseUrl", base),
            ("REELS_S3_ACCESS_KEY_ID", key_id),
            ("REELS_S3_SECRET_ACCESS_KEY", secret),
        )
        if not value
    ]
    if missing:
        raise ReelsError(
            "upload isn't set up (missing " + ", ".join(missing) + "). "
            "See .claude/skills/cycling-reels/reference/publishing.md"
        )
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        region_name=up.get("region") or "auto",
    )
    return client, bucket, str(base).rstrip("/"), up.get("prefix") or ""


def upload(project: Project, slugs: list[str]) -> list[dict]:
    """Copies reel + cover to S3-compatible storage (e.g. Cloudflare R2) and records public URLs."""
    if not slugs:
        raise ReelsError("nothing to upload")
    client, bucket, base, prefix = _client(project)
    done = []
    for slug in slugs:
        path = project.out_dir / slug / "post.json"
        if not path.exists():
            raise ReelsError(f"no rendered reel called {slug}")
        post = read_json(path)
        for field, ext, ctype, url_field in (("file", ".mp4", "video/mp4", "mediaUrl"), ("cover", ".jpg", "image/jpeg", "coverUrl")):
            key = f"{prefix}{slug}{ext}"
            client.upload_file(str(project.abs(post[field])), bucket, key, ExtraArgs={"ContentType": ctype})
            post[url_field] = f"{base}/{key}"
        write_json(path, post)
        log(f"↑ {slug}: {post['mediaUrl']}")
        done.append(post)
    return done
