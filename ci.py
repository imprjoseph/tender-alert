#!/usr/bin/env python3
"""
GitHub Actions 用的小工具（Windows / Linux 執行器通用，不依賴 bash 語法）

  python ci.py run      依觸發方式組出參數並執行 tender_alert.py
  python ci.py commit   把 data/、reports/ 的變動存回 repo
"""
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

BACKUP_CRON = "37 1 * * *"


def env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def run() -> int:
    args = []
    if env("EVENT_SCHEDULE") == BACKUP_CRON:
        args.append("--if-not-done")
    if env("IN_DRY_RUN") == "true":
        args.append("--dry-run")
    if env("IN_DB_ONLY") == "true":
        args.append("--db-only")
    if env("IN_IGNORE_SEEN") == "true":
        args.append("--ignore-seen")
    if env("IN_SYNC_ALL") == "true":
        args = ["--sync-all"]
    if env("IN_RESCORE") == "true":
        args = ["--rescore"]
    if env("IN_DAYS"):
        args += ["--days", env("IN_DAYS")]
    cmd = [sys.executable, "tender_alert.py", *args]
    print("▶", " ".join(cmd), flush=True)
    return subprocess.call(cmd)


def sh(*cmd: str, check: bool = True) -> int:
    print("$", " ".join(cmd), flush=True)
    r = subprocess.call(list(cmd))
    if check and r != 0:
        raise SystemExit(r)
    return r


def commit() -> int:
    sh("git", "config", "user.name", "tender-bot")
    sh("git", "config", "user.email", "tender-bot@users.noreply.github.com")
    paths = [p for p in ("data", "reports") if os.path.exists(p)]
    if not paths:
        return 0
    sh("git", "add", *paths)
    if subprocess.call(["git", "diff", "--cached", "--quiet"]) == 0:
        print("（沒有變動）")
        return 0
    today = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")
    sh("git", "commit", "-m", f"標案快報 {today}")
    for _ in range(3):
        if sh("git", "push", check=False) == 0:
            return 0
        sh("git", "pull", "--rebase", check=False)
    return 1


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "run"
    sys.exit(run() if action == "run" else commit())
