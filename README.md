# Anilist Update automation

Scripts sync **CSV + Parquet** pairs with **Cloudflare R2** using the same `R2_PREFIX` **+ basename keys** pattern. Full object keys are `r2_object_key(prefix, basename)` — no folder paths hardcoded in Python.

For each `R2_*_CSV_KEY` basename (e.g. `log.csv`), the Parquet object is `**{stem}.parquet`** (e.g. `log.parquet`) under the same `R2_PREFIX`. Upload order matches BDO: **parquet first, then CSV** per dataset.

## Setup

```bash
cd "Anilist Update"
pip install -r requirements.txt
cp .env.example .env
```

Fill **R2 credentials**, `**ANILIST_USERNAME`**, and optionally `**DISCORD_*`**. Object names are basename env vars plus `**R2_PREFIX**` (set to empty for bucket root: `R2_PREFIX=`).

`[anilist_r2.py](anilist_r2.py)` loads **only** this folder’s `.env` on import.


| Item                                                          | Required                            |
| ------------------------------------------------------------- | ----------------------------------- |
| R2 + `ANILIST_USERNAME`                                       | Yes                                 |
| `R2_READ_LOG_`*, `R2_RECENT_*`, `R2_DONE_PLANNED_*` basenames | Yes (defaults in `.env.example`)    |
| `R2_PREFIX`                                                   | Key must be set; value may be empty |
| `DISCORD_*`, `ANILIST_DATA_DIR`                               | No                                  |


**CI logs** stay generic — no API bodies or titles. **Discord** is private but still avoid secrets.

## R2 bootstrap (`anilist_read.py`)

The read script **downloads both** the read log and recent CSV from R2 **before** merging new activity. Seed R2 once with two CSVs whose columns match what the scripts expect (e.g. minimal headers / empty data rows) at keys:

`{R2_PREFIX}/{R2_READ_LOG_CSV_KEY}` and `{R2_PREFIX}/{R2_RECENT_READ_CSV_KEY}`. On each successful run, matching `***.parquet`** files are written and uploaded (no separate env vars).

`anilist_done-planned_log.py` rebuilds from the API and uploads **CSV + Parquet** on each run (no prior objects required for a cold start).

## Local

```bash
python anilist_read.py
python anilist_done-planned_log.py
```

## GitHub Actions

`[.github/workflows/](.github/workflows/)` — two scheduled jobs. `pip install -r requirements.txt` matches local use. The pip cache key uses `**hashFiles('requirements.txt')**` (paths are from the repo root).

Workflow `env` repeats the same `**R2_*` basename keys** and `**R2_PREFIX`** as `.env.example`; secrets live on the `**prod`** environment.


| Workflow                   | Cron (UTC)   | Script                        |
| -------------------------- | ------------ | ----------------------------- |
| `anilist-read.yml`         | `0 2 * * *`  | `anilist_read.py`             |
| `anilist-done-planned.yml` | `0 14 * * *` | `anilist_done-planned_log.py` |


Both support `workflow_dispatch`.

