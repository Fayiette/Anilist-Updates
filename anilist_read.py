"""Merge latest Anilist manga read activity into CSV logs and sync with R2.

Public-CI safe: no API response bodies, titles, or URLs in logs.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from anilist_r2 import (
    configure_logging,
    data_dir,
    discord_user_prefix,
    download_object_or_exit,
    env_required,
    fold_upload_results,
    parquet_basename_from_csv_key,
    r2_object_key,
    r2_prefix,
    s3_client,
    send_discord_alert,
    upload_file_if_changed,
)

READ_LOG_BASENAME = env_required("R2_READ_LOG_CSV_KEY")
RECENT_LOG_BASENAME = env_required("R2_RECENT_READ_CSV_KEY")
USERNAME = env_required("ANILIST_USERNAME")

HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

logger = logging.getLogger("anilist.read")


def get_user_id(username: str) -> int:
    query = """
    query ($name: String) {
      User(name: $name) { id }
    }"""
    res = requests.post(
        "https://graphql.anilist.co",
        json={"query": query, "variables": {"name": username}},
        headers=HEADERS,
        timeout=(5, 30),
    )
    res.raise_for_status()
    return int(res.json()["data"]["User"]["id"])


def get_manga_list_activities(user_id: int, page: int = 1, per_page: int = 50):
    query = """
    query ($userId: Int, $page: Int, $perPage: Int) {
      Page(page: $page, perPage: $perPage) {
        activities(userId: $userId, type: MANGA_LIST, sort: ID_DESC) {
          ... on ListActivity {
            createdAt
            progress
            media {
              title { english romaji }
              siteUrl
              type
            }
          }
        }
      }
    }"""
    variables = {"userId": user_id, "page": page, "perPage": per_page}
    res = requests.post(
        "https://graphql.anilist.co",
        json={"query": query, "variables": variables},
        headers=HEADERS,
        timeout=(5, 30),
    )
    res.raise_for_status()
    return res.json()["data"]["Page"]["activities"]


def process_and_group_activities(activities):
    rows = []
    for act in activities:
        media = act.get("media")
        if not media or media.get("type") != "MANGA":
            continue
        title = media["title"].get("english") or media["title"].get("romaji") or "N/A"
        site_url = media.get("siteUrl", "N/A")
        created_at = datetime.fromtimestamp(
            act["createdAt"], tz=timezone.utc
        ).isoformat()
        progress = (act.get("progress") or "").strip()

        match = re.match(r"^(\d+)\s*-\s*(\d+)$", progress)
        if match:
            start, end = int(match[1]), int(match[2])
            for c in range(start, end + 1):
                rows.append(
                    {
                        "Title": title,
                        "AniList Link": site_url,
                        "Time Read (UTC)": created_at,
                        "Chapter": str(c),
                    }
                )
        elif progress:
            rows.append(
                {
                    "Title": title,
                    "AniList Link": site_url,
                    "Time Read (UTC)": created_at,
                    "Chapter": progress,
                }
            )

    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame()

    df["Time Read (UTC)"] = pd.to_datetime(df["Time Read (UTC)"], utc=True)
    df["Chapter"] = df["Chapter"].astype(int)
    df.sort_values(
        by=["AniList Link", "Chapter", "Time Read (UTC)"], inplace=True
    )
    df.drop_duplicates(
        subset=["AniList Link", "Chapter"], keep="first", inplace=True
    )

    grouped = []
    for (title, link), group in df.groupby(["Title", "AniList Link"]):
        group = group.sort_values("Time Read (UTC)").reset_index(drop=True)
        session = []
        session_start = group.loc[0, "Time Read (UTC)"]

        for _, row in group.iterrows():
            if session and (
                row["Time Read (UTC)"] - session_start > timedelta(minutes=30)
            ):
                chaps = [r["Chapter"] for r in session]
                latest = max(r["Time Read (UTC)"] for r in session)
                grouped.append(
                    {
                        "Title": title,
                        "AniList Link": link,
                        "Time Read (UTC)": latest.isoformat(),
                        "Chapter Start": min(chaps),
                        "Chapter End": max(chaps),
                    }
                )
                session = []
            if not session:
                session_start = row["Time Read (UTC)"]
            session.append(row)

        if session:
            chaps = [r["Chapter"] for r in session]
            latest = max(r["Time Read (UTC)"] for r in session)
            grouped.append(
                {
                    "Title": title,
                    "AniList Link": link,
                    "Time Read (UTC)": latest.isoformat(),
                    "Chapter Start": min(chaps),
                    "Chapter End": max(chaps),
                }
            )

    final_df = pd.DataFrame(grouped)
    if not final_df.empty:
        final_df["Status"] = "Reading"
    return final_df


def main() -> str:
    configure_logging()
    client, bucket = s3_client()
    prefix = r2_prefix()
    key_read = r2_object_key(prefix, READ_LOG_BASENAME)
    key_recent = r2_object_key(prefix, RECENT_LOG_BASENAME)
    recent_pq_name = parquet_basename_from_csv_key(RECENT_LOG_BASENAME)
    read_pq_name = parquet_basename_from_csv_key(READ_LOG_BASENAME)
    key_recent_pq = r2_object_key(prefix, recent_pq_name)
    key_read_pq = r2_object_key(prefix, read_pq_name)

    base = data_dir()
    read_csv = base / READ_LOG_BASENAME
    recent_csv = base / RECENT_LOG_BASENAME
    read_pq = base / read_pq_name
    recent_pq = base / recent_pq_name

    download_object_or_exit(client, bucket, key_read, read_csv)
    download_object_or_exit(client, bucket, key_recent, recent_csv)

    user_id = get_user_id(USERNAME)

    activities: list = []
    for page in range(1, 3):
        page_data = get_manga_list_activities(user_id, page)
        if not page_data:
            break
        activities.extend(page_data)
        time.sleep(1.5)

    df = process_and_group_activities(activities)
    if df.empty:
        logger.info("No valid read activity found.")
        return "no-change"

    df.to_csv(recent_csv, index=False)
    old = pd.read_csv(read_csv)
    combined = pd.concat([old, df]).drop_duplicates(
        subset=["AniList Link", "Chapter Start", "Chapter End"]
    )
    combined.to_csv(read_csv, index=False)

    df.to_parquet(recent_pq, index=False)
    combined.to_parquet(read_pq, index=False)

    # Parquet before CSV per pair.
    out = (
        upload_file_if_changed(
            client,
            bucket,
            key_recent_pq,
            recent_pq,
            content_type="application/vnd.apache.parquet",
            public=True,
        ),
        upload_file_if_changed(
            client,
            bucket,
            key_recent,
            recent_csv,
            content_type="text/csv",
            public=True,
        ),
        upload_file_if_changed(
            client,
            bucket,
            key_read_pq,
            read_pq,
            content_type="application/vnd.apache.parquet",
            public=True,
        ),
        upload_file_if_changed(
            client,
            bucket,
            key_read,
            read_csv,
            content_type="text/csv",
            public=True,
        ),
    )
    return fold_upload_results(*out)


if __name__ == "__main__":
    ts = int(time.time())
    label = "Anilist Read Script"
    pre = discord_user_prefix()
    try:
        out = main()
        if out == "uploaded":
            send_discord_alert(f"✅ {label} — Uploaded to R2 at <t:{ts}:f>")
        elif out == "no-change":
            send_discord_alert(
                f"✅ {label} — No changes to upload. Last checked at <t:{ts}:f>"
            )
        else:
            send_discord_alert(
                f"{pre}⚠️ {label} — Finished with status {out} at <t:{ts}:f>"
            )
    except Exception as e:
        logging.getLogger("anilist.read").exception("Script failed.")
        send_discord_alert(f"{pre}❌ {label} failed at <t:{ts}:f>: {e}")
