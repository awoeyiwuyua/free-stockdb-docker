"""CI diagnostic: post test_ops log tail to the PR comment (temporary, env support).

Invoked from .github/workflows/test.yml; removed after investigation.
"""
import json
import os
import urllib.request

log_path = os.environ.get("OPS_LOG", "/tmp/ops.log")
try:
    with open(log_path, encoding="utf-8", errors="replace") as f:
        log = f.read()
except OSError as exc:
    log = f"(log read failed: {exc})"

tail = "\n".join(log.splitlines()[-150:])
body = "**test_ops CI 诊断（无条件上报）**\n```\n" + tail[:6000] + "\n```"
data = json.dumps({"body": body}).encode("utf-8")

pr = os.environ.get("GITHUB_REF", "").replace("refs/pull/", "").split("/")[0]
repo = os.environ.get("GITHUB_REPOSITORY", "")
token = os.environ.get("GITHUB_TOKEN", "")
if pr and repo and token:
    url = f"https://api.github.com/repos/{repo}/issues/{pr}/comments"
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "ci-diagnose",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print("comment posted:", r.status)
    except Exception as exc:  # noqa: BLE001
        print("comment failed:", exc)
else:
    print("skip comment (missing pr/repo/token)", pr, repo, bool(token))
