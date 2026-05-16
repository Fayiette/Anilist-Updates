"""Shared R2 helpers for Anilist automation scripts.

Public-CI safety: this module never logs secret values, webhook URLs, R2
endpoints, or Anilist response bodies. Object keys are built from ``R2_PREFIX``
(see Black Desert Online ``pearl_items.py``) plus basename env vars — no path
literals in callers.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
from pathlib import Path
from typing import Optional, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger("anilist")


def load_repo_env() -> None:
    """Load ``.env`` from the Anilist Update folder."""
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        return
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False)


def env_required(name: str) -> str:
    """Read required string from env (empty → log and exit)."""
    v = (os.getenv(name) or "").strip()
    if not v:
        logger.error("Missing or empty env var: %s", name)
        sys.exit(1)
    return v


def r2_object_key(prefix: str, filename: str) -> str:
    """Build S3 object key;"""
    if not prefix:
        return filename
    return f"{prefix}/{filename}"


def parquet_basename_from_csv_key(csv_key: str) -> str:
    """Twin Parquet name for a CSV basename."""
    return f"{Path(csv_key).stem}.parquet"


def fold_upload_results(*results: str) -> str:
    """Aggregate multiple ``upload_file_if_changed`` outcomes (any error → error, etc.)."""
    if any(r == "error" for r in results):
        return "error"
    if any(r == "uploaded" for r in results):
        return "uploaded"
    return "no-change"


def r2_prefix() -> str:
    """Normalized prefix from ``R2_PREFIX``. Key must be set; value may be empty for bucket root."""
    if "R2_PREFIX" not in os.environ:
        logger.error(
            "R2_PREFIX must be set (use empty for bucket root, e.g. R2_PREFIX= in .env)."
        )
        sys.exit(1)
    return os.environ["R2_PREFIX"].strip().strip("/")


def data_dir() -> Path:
    override = os.getenv("ANILIST_DATA_DIR")
    base = Path(override).expanduser() if override else Path(__file__).resolve().parent
    base.mkdir(parents=True, exist_ok=True)
    return base


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        logger.error("Missing required environment variable.")
        sys.exit(1)
    return val


def s3_client() -> Tuple["boto3.client", str]:
    bucket = _require("R2_BUCKET")
    access = _require("R2_ACCESS_KEY_ID")
    secret = _require("R2_SECRET_ACCESS_KEY")
    endpoint = _require("R2_ENDPOINT")
    session = boto3.session.Session()
    client = session.client(
        "s3",
        region_name="auto",
        endpoint_url=endpoint,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
    )
    return client, bucket


def compute_file_hash(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def download_object_or_exit(client, bucket: str, key: str, dest: Path) -> None:
    """Download a required object; exit(1) on any failure (strict bootstrap)."""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        client.download_file(bucket, key, str(dest))
        logger.info("Downloaded required object from R2.")
    except (ClientError, BotoCoreError, OSError) as e:
        logger.error(
            "Required R2 object missing or download failed (%s). Aborting.",
            type(e).__name__,
        )
        sys.exit(1)


def download_object_if_exists(client, bucket: str, key: str, dest: Path) -> bool:
    """Download an optional object. Returns True if the file is now on disk."""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        client.download_file(bucket, key, str(dest))
        logger.info("Downloaded optional object from R2.")
        return True
    except ClientError as e:
        code = ""
        try:
            code = e.response.get("Error", {}).get("Code", "") or ""
        except AttributeError:
            code = ""
        if code in {"404", "NoSuchKey", "NotFound"}:
            logger.info("Optional object not present on R2; starting fresh.")
            return False
        logger.warning("Optional object fetch failed (%s).", type(e).__name__)
        return False
    except (BotoCoreError, OSError):
        logger.warning("Optional object fetch transport error.")
        return False


def upload_file_if_changed(
    client,
    bucket: str,
    key: str,
    local: Path,
    content_type: str = "application/octet-stream",
    cache_control: Optional[str] = None,
    public: bool = True,
) -> str:
    """Upload only when the local SHA256 differs from remote. Returns one of:

    "uploaded", "no-change", "no-data", "error".
    """
    local_hash = compute_file_hash(local)
    if not local_hash:
        return "no-data"

    remote_hash: Optional[str] = None
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
        remote_hash = hashlib.sha256(obj["Body"].read()).hexdigest()
    except Exception:
        remote_hash = None

    if local_hash == remote_hash:
        logger.info("No change for object; skipping upload.")
        return "no-change"

    extra = {"ContentType": content_type}
    if public:
        extra["ACL"] = "public-read"
    if cache_control:
        extra["CacheControl"] = cache_control

    try:
        client.upload_file(str(local), bucket, key, ExtraArgs=extra)
        logger.info("Uploaded object to R2.")
        return "uploaded"
    except (ClientError, BotoCoreError, OSError) as e:
        logger.error("R2 upload failed (%s).", type(e).__name__)
        return "error"


def send_discord_alert(message: str) -> None:
    """Post to a Discord webhook (private channel). Errors are swallowed."""
    url = os.getenv("DISCORD_WEBHOOK_URL") or os.getenv("DISCORD_WEBHOOK")
    if not url:
        return
    try:
        import requests

        resp = requests.post(url, json={"content": message[:1900]}, timeout=8)
        resp.raise_for_status()
    except Exception:
        logger.warning("Discord notification failed.")


def discord_user_prefix() -> str:
    uid = (os.getenv("DISCORD_USER_ID") or "").strip()
    return f"<@{uid}> " if uid.isdigit() else ""


load_repo_env()
