"""Export Anilist manga list (planned/reading/etc.) to CSV and Parquet and sync with R2.

Public-CI safe: no API response bodies or media titles in logs.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import pandas as pd
import requests

from anilist_r2 import (
    configure_logging,
    data_dir,
    discord_user_prefix,
    env_required,
    fold_upload_results,
    parquet_basename_from_csv_key,
    r2_object_key,
    r2_prefix,
    s3_client,
    send_discord_alert,
    upload_file_if_changed,
)

DONE_PLANNED_BASENAME = env_required("R2_DONE_PLANNED_CSV_KEY")
USERNAME = env_required("ANILIST_USERNAME")

API_URL = "https://graphql.anilist.co"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}

GRAPHQL_QUERY = """
query ($username: String) {
  MediaListCollection(userName: $username, type: MANGA) {
    lists {
      name
      entries {
        media {
          title {
            romaji
          }
          siteUrl
          coverImage {
            large
            medium
          }
          description(asHtml: false)
        }
        status
        createdAt
      }
    }
  }
}
"""

DONE_COLUMNS = [
    "Title",
    "AniList Link",
    "Time Added",
    "Status",
    "Cover Image (Large)",
    "Cover Image (Medium)",
    "Description",
]

logger = logging.getLogger("anilist.done")


def fetch_manga_entries(username: str) -> list:
    variables = {"username": username}
    response = requests.post(
        API_URL,
        headers=HEADERS,
        json={"query": GRAPHQL_QUERY, "variables": variables},
        timeout=(5, 60),
    )
    if response.status_code != 200:
        logger.warning(
            "Media list API returned status %s.",
            response.status_code,
        )
        return []

    try:
        data = response.json()
    except ValueError:
        logger.warning("Failed to parse API JSON.")
        return []

    entries: list = []
    lists = data.get("data", {}).get("MediaListCollection", {}).get("lists", [])
    for media_list in lists:
        for item in media_list.get("entries", []):
            status_map = {
                "PLANNING": "Planned",
                "COMPLETED": "Completed",
                "DROPPED": "Dropped",
                "CURRENT": "Reading",
            }

            anilist_status = item.get("status")
            if anilist_status not in status_map:
                continue

            media = item.get("media", {})
            if not media:
                continue

            title = media.get("title", {}).get("romaji", "Unknown Title")
            link = media.get("siteUrl", "")
            created_at = item.get("createdAt")
            time_added = (
                datetime.fromtimestamp(created_at, timezone.utc).isoformat()
                if created_at
                else ""
            )
            status = status_map[anilist_status]

            cover = media.get("coverImage", {})
            cover_large = cover.get("large", "")
            cover_medium = cover.get("medium", "")

            raw_description = media.get("description")
            description = (
                raw_description.replace("\n", " ").replace("\r", " ").strip()
                if raw_description
                else "Coming Soon"
            )

            entries.append(
                [
                    title,
                    link,
                    time_added,
                    status,
                    cover_large,
                    cover_medium,
                    description,
                ]
            )
    return entries


def main() -> str:
    configure_logging()

    entries = fetch_manga_entries(USERNAME)
    if not entries:
        logger.warning("No entries returned from API.")
        return "no-data"

    base = data_dir()
    csv_path = base / DONE_PLANNED_BASENAME
    pq_path = base / parquet_basename_from_csv_key(DONE_PLANNED_BASENAME)

    df = pd.DataFrame(entries, columns=DONE_COLUMNS)
    df.to_csv(csv_path, index=False)
    df.to_parquet(pq_path, index=False)
    logger.info("Wrote CSV and Parquet with %d rows.", len(df))

    prefix = r2_prefix()
    client, bucket = s3_client()
    key_csv = r2_object_key(prefix, DONE_PLANNED_BASENAME)
    key_pq = r2_object_key(prefix, parquet_basename_from_csv_key(DONE_PLANNED_BASENAME))

    r_pq = upload_file_if_changed(
        client,
        bucket,
        key_pq,
        pq_path,
        content_type="application/vnd.apache.parquet",
        public=True,
    )
    r_csv = upload_file_if_changed(
        client,
        bucket,
        key_csv,
        csv_path,
        content_type="text/csv",
        public=True,
    )
    return fold_upload_results(r_pq, r_csv)


if __name__ == "__main__":
    ts = int(time.time())
    label = "Anilist Planned Script"
    pre = discord_user_prefix()
    try:
        result = main()
        if result == "uploaded":
            send_discord_alert(f"✅ {label} — Uploaded to R2 at <t:{ts}:f>")
        elif result == "no-change":
            send_discord_alert(
                f"✅ {label} — No changes to upload. Last checked at <t:{ts}:f>"
            )
        elif result == "no-data":
            send_discord_alert(
                f"{pre}⚠️ {label} — No data to save at <t:{ts}:f>"
            )
        else:
            send_discord_alert(
                f"{pre}⚠️ {label} — Finished with status {result} at <t:{ts}:f>"
            )
    except Exception as e:
        logger.exception("Script failed.")
        send_discord_alert(f"{pre}❌ {label} failed at <t:{ts}:f>: {e}")
