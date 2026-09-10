# Upload/sync the data cache to R2 (sg-view-analysis-data bucket).
# Re-run any time after rerunning a build_island_*.py / build_hdb_heights.py
# script -- only changed files actually re-upload (see upload.py).
. "$PSScriptRoot\set_credentials.ps1"
python "$PSScriptRoot\upload.py" @args
