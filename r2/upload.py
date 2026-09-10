"""Upload sg-view-analysis's data cache to its own R2 bucket.

Uploads every cache/<name>.geojson and cache/<name>.json file the VIEWER
actually needs at runtime (the whole-island layers + height overrides) to
R2 under the same "cache/<name>" key, so the deployed viewer can fetch
cache/<name> from the Worker exactly like the local dev server serves
cache/<name> today -- only the origin changes.

Deliberately its own bucket (sg-view-analysis-data), not
ThunderPrintSG's webstorebucket1 -- see r2/worker/deploy_worker.py.

Only uploads the files the running app actually fetches (see the
fetch(...) call sites in viewer/app.js) -- NOT every intermediate/debug
file that happens to be sitting in cache/ (raw tile dumps, old duplicate
test pulls, per-address .geojson snapshots), which would upload ~541MB of
mostly-irrelevant data instead of the ~150-200MB actually needed.

Credentials: set R2_ACCESS_KEY and R2_SECRET_KEY as environment
variables before running (see set_credentials.ps1).

Usage:
    python r2/upload.py           # upload anything changed or missing
    python r2/upload.py --dry-run # show what would upload, touch nothing
"""
import argparse
import hashlib
import mimetypes
import os
import sys

import boto3

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE_DIR = os.path.join(ROOT, "cache")
BUCKET = "sg-view-analysis-data"
ENDPOINT = "https://f409e2327e8026a4e1729b9706b2beea.r2.cloudflarestorage.com"

# Exactly the files viewer/app.js fetches at runtime -- see its six
# `fetch('../cache/...')` call sites. Keep this list in sync with app.js;
# it's deliberately NOT "everything in cache/" (see module docstring).
NEEDED_FILES = [
    "sg_buildings_full.geojson",
    "sg_roads_full.geojson",
    "sg_water_full.geojson",
    "sg_transit_full.geojson",
    "sg_parks_full.geojson",
    "sg_schools_full.geojson",
    "manual_heights.json",
    "hdb_heights.json",
]


def get_client():
    access = os.environ.get("R2_ACCESS_KEY")
    secret = os.environ.get("R2_SECRET_KEY")
    if not access or not secret:
        sys.exit("Set R2_ACCESS_KEY and R2_SECRET_KEY environment variables first "
                  "(see set_credentials.ps1 / run_upload.ps1 in this folder).")
    return boto3.client(
        "s3", endpoint_url=ENDPOINT,
        aws_access_key_id=access, aws_secret_access_key=secret,
        region_name="auto",
    )


def md5_hex(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    s3 = get_client()

    # R2's ETag for a non-multipart upload is the object's MD5 -- compare
    # against the local file's MD5 so a rerun only re-uploads what actually
    # changed (matters here: the buildings file alone is ~89MB).
    existing = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix="cache/"):
        for obj in page.get("Contents", []):
            existing[obj["Key"]] = obj["ETag"].strip('"')

    uploaded, skipped, missing = [], [], []
    for name in NEEDED_FILES:
        local_path = os.path.join(CACHE_DIR, name)
        key = f"cache/{name}"
        if not os.path.exists(local_path):
            missing.append(name)
            continue

        local_md5 = md5_hex(local_path)
        if existing.get(key) == local_md5:
            skipped.append(name)
            continue

        size_mb = os.path.getsize(local_path) / 1024 / 1024
        if args.dry_run:
            print(f"[dry-run] would upload {name} ({size_mb:.1f}MB)")
            uploaded.append(name)
            continue

        content_type = mimetypes.guess_type(local_path)[0] or "application/json"
        print(f"Uploading {name} ({size_mb:.1f}MB)...")
        s3.upload_file(local_path, BUCKET, key, ExtraArgs={"ContentType": content_type})
        uploaded.append(name)

    print(f"\n{len(uploaded)} uploaded, {len(skipped)} unchanged, {len(missing)} missing locally")
    if missing:
        print("Missing (run the relevant build_island_*.py first):", ", ".join(missing))


if __name__ == "__main__":
    main()
