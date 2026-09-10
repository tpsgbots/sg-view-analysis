# Deploy sg-view-analysis-data-bot (r2/worker/worker.js) to Cloudflare.
. "$PSScriptRoot\set_credentials.ps1"
python "$PSScriptRoot\worker\deploy_worker.py"
