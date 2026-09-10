"""Deploy sg-view-analysis-data-bot to Cloudflare Workers.

Uploads worker.js (in this folder) as the script's single module,
declaring the R2 bucket binding (DATA_BUCKET -> sg-view-analysis-data) and
enabling the *.workers.dev route so it's reachable with no extra DNS setup.

Same Cloudflare account as ThunderPrintSG's thunderprint-gallery-bot
(reused credentials, per Wesley), but a completely separate bucket and
Worker script -- this project's data never touches webstorebucket1.

Requires CF_API_TOKEN in the environment (see ../set_credentials.ps1).
Run:  ../run_deploy_worker.ps1   (sets the token, then calls this)
"""
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

ACCOUNT_ID = "f409e2327e8026a4e1729b9706b2beea"
SCRIPT_NAME = "sg-view-analysis-data-bot"
BUCKET_NAME = "sg-view-analysis-data"
HERE = os.path.dirname(os.path.abspath(__file__))
WORKER_JS = os.path.join(HERE, "worker.js")

METADATA = {
    "main_module": "worker.js",
    "compatibility_date": "2026-08-09",
    "bindings": [
        {"type": "r2_bucket", "name": "DATA_BUCKET", "bucket_name": BUCKET_NAME}
    ],
    "observability": {"enabled": True, "head_sampling_rate": 1},
}


def api(method, path, token, body=None, content_type="application/json"):
    url = f"https://api.cloudflare.com/client/v4{path}"
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": content_type},
    )
    try:
        return json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path} failed: HTTP {e.code}\n{e.read().decode()[:1000]}")


def main():
    token = os.environ.get("CF_API_TOKEN")
    if not token:
        sys.exit("CF_API_TOKEN not set -- dot-source set_credentials.ps1 first.")

    script = open(WORKER_JS, "rb").read()
    boundary = uuid.uuid4().hex
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"; '
        f'filename="metadata.json"\r\nContent-Type: application/json\r\n\r\n'.encode()
        + json.dumps(METADATA).encode()
        + b"\r\n"
        + f'--{boundary}\r\nContent-Disposition: form-data; name="worker.js"; '
        f'filename="worker.js"\r\nContent-Type: application/javascript+module\r\n\r\n'.encode()
        + script
        + b"\r\n"
        + f"--{boundary}--\r\n".encode()
    )

    res = api(
        "PUT", f"/accounts/{ACCOUNT_ID}/workers/scripts/{SCRIPT_NAME}", token,
        body=body, content_type=f"multipart/form-data; boundary={boundary}",
    )
    print("deploy success:", res["success"])
    print("modified_on:", res["result"].get("modified_on"))

    # Enable the *.workers.dev route for this script -- off by default per script.
    sub = api("GET", f"/accounts/{ACCOUNT_ID}/workers/subdomain", token)
    subdomain = sub["result"]["subdomain"]
    api(
        "POST", f"/accounts/{ACCOUNT_ID}/workers/scripts/{SCRIPT_NAME}/subdomain", token,
        body=json.dumps({"enabled": True, "previews_enabled": False}).encode(),
    )
    print(f"Live at: https://{SCRIPT_NAME}.{subdomain}.workers.dev/")


if __name__ == "__main__":
    main()
