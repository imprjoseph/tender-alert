#!/usr/bin/env python3
"""
政府電子採購網 每日標案關鍵字通知
資料來源：政府電子採購網（經 g0v 標案 API：https://pcc-api.openfun.app）

流程：
  1. 抓最近 N 天的公告清單（/api/listbydate）
  2. 依「公告類型 → 標的分類 → 關鍵字 / 排除詞」篩選
  3. 已通知過的案件跳過（data/seen.json）
  4. 對命中案件抓明細（預算、截止投標、官方連結）
  5. 寄 Email / 推 LINE，並寫入 data/matches.csv 與 reports/

用法：
  python tender_alert.py                 # 正式執行
  python tender_alert.py --dry-run       # 只印結果，不寄送、不更新 seen
  python tender_alert.py --date 20261013 # 指定日期（測試用）
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import smtplib
import sys
import time
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

TZ = ZoneInfo("Asia/Taipei")
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
REPORT_DIR = ROOT / "reports"
SEEN_FILE = DATA_DIR / "seen.json"
MATCH_CSV = DATA_DIR / "matches.csv"

API_BASE = os.environ.get("PCC_API_BASE", "https://pcc-api.openfun.app")
FIXTURE_DIR = os.environ.get("PCC_FIXTURE_DIR")  # 離線測試用
UA = "imPR-tender-alert/1.0 (+github actions)"


# ---------------------------------------------------------------- 資料抓取
def _get_json(path: str, params: dict) -> dict:
    if FIXTURE_DIR:
        key = "_".join(str(v) for v in params.values())
        f = Path(FIXTURE_DIR) / f"{path.strip('/').replace('/', '_')}_{key}.json"
        return json.loads(f.read_text("utf-8")) if f.exists() else {"records": []}

    url = f"{API_BASE}{path}"
    last_err = None
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, timeout=30, headers={"User-Agent": UA})
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"API 失敗：{url} {params} → {last_err}")


def list_by_date(d: date) -> list[dict]:
    return _get_json("/api/listbydate", {"date": d.strftime("%Y%m%d")}).get("records", [])


def tender_detail(unit_id: str, job_number: str, filename: str) -> dict:
    try:
        data = _get_json("/api/tender", {"unit_id": unit_id, "job_number": job_number})
    except RuntimeError:
        return {}
    for rec in data.get("records", []):
        if rec.get("filename") == filename:
            return rec.get("detail", {}) or {}
    return {}


# ---------------------------------------------------------------- 篩選
def contains_any(text: str, words: list[str]) -> list[str]:
    return [w for w in words or [] if w and w in text]


def match_record(rec: dict, cfg: dict) -> dict | None:
    brief = rec.get("brief", {}) or {}
    ttype = brief.get("type", "")
    title = brief.get("title", "") or ""
    category = brief.get("category", "") or ""
    unit = rec.get("unit_name", "") or ""

    f = cfg["filters"]
    # 1) 公告類型
    if f.get("type_include") and not contains_any(ttype, f["type_include"]):
        return None
    if contains_any(ttype, f.get("type_exclude", [])):
        return None
    # 2) 標的分類
    if contains_any(category, f.get("category_exclude", [])):
        return None
    # 3) 機關
    if f.get("unit_include") and not contains_any(unit, f["unit_include"]):
        return None
    if contains_any(unit, f.get("unit_exclude", [])):
        return None
    # 4) 全域排除詞（案名）
    if contains_any(title, f.get("title_exclude", [])):
        return None

    groups_hit, kw_hit, score = [], [], 0
    for g in cfg["keyword_groups"]:
        hits = contains_any(title, g.get("keywords", []))
        if g.get("search_unit_name"):
            hits += contains_any(unit, g.get("keywords", []))
        if hits and contains_any(title, g.get("exclude", [])):
            hits = []
        if hits and g.get("must_also") and not contains_any(title, g["must_also"]):
            hits = []
        if hits:
            groups_hit.append(g["name"])
            kw_hit += hits
            score += g.get("priority", 1) * len(set(hits))

    cat_hits = contains_any(category, f.get("category_boost", []))
    if cat_hits:
        score += f.get("category_boost_score", 2)
        if not groups_hit and f.get("category_boost_alone", True):
            groups_hit.append("標的分類")
        kw_hit += cat_hits

    if not groups_hit:
        return None

    return {
        "announce_date": str(rec.get("date", "")),
        "type": ttype,
        "unit_id": rec.get("unit_id", ""),
        "unit_name": unit,
        "job_number": rec.get("job_number", ""),
        "filename": rec.get("filename", ""),
        "title": title,
        "category": category,
        "groups": groups_hit,
        "keywords": sorted(set(kw_hit), key=kw_hit.index),
        "score": score,
        "g0v_url": API_BASE + rec.get("url", ""),
    }


def roc_to_dt(s: str) -> datetime | None:
    """'115/11/11 17:00' → datetime"""
    m = re.match(r"(\d{2,3})/(\d{1,2})/(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", s or "")
    if not m:
        return None
    y, mo, d = int(m[1]) + 1911, int(m[2]), int(m[3])
    hh, mm = int(m[4] or 0), int(m[5] or 0)
    try:
        return datetime(y, mo, d, hh, mm, tzinfo=TZ)
    except ValueError:
        return None


def parse_money(s: str) -> int | None:
    m = re.search(r"[\d,]+", s or "")
    return int(m[0].replace(",", "")) if m else None


def enrich(item: dict, now: datetime) -> dict:
    d = tender_detail(item["unit_id"], item["job_number"], item["filename"])
    item["budget"] = parse_money(d.get("採購資料:預算金額", ""))
    item["deadline_str"] = d.get("領投開標:截止投標", "") or d.get("標案內容:公開徵求期間", "")
    dl = roc_to_dt(item["deadline_str"].split("－")[-1].strip()) if item["deadline_str"] else None
    item["deadline"] = dl
    item["days_left"] = (dl.date() - now.date()).days if dl else None
    item["award_method"] = d.get("招標資料:決標方式", "")
    item["url"] = d.get("url") or item["g0v_url"]
    return item


# ---------------------------------------------------------------- 狀態檔
def load_seen() -> dict:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text("utf-8"))
    return {}


def save_seen(seen: dict, keep_days: int = 120) -> None:
    cutoff = (datetime.now(TZ) - timedelta(days=keep_days)).strftime("%Y%m%d")
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    DATA_DIR.mkdir(exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=0), "utf-8")


def seen_key(it: dict) -> str:
    return f"{it['unit_id']}|{it['job_number']}|{it['type']}"


# ---------------------------------------------------------------- 輸出
def fmt_money(v):
    if v is None:
        return "—"
    return f"{v / 10000:,.0f} 萬" if v >= 10000 else f"{v:,} 元"


def fmt_days(it):
    if it.get("days_left") is None:
        return "—"
    n = it["days_left"]
    return "今日截止" if n == 0 else f"剩 {n} 天"


def build_html(items: list[dict], run_date: str, stats: dict) -> str:
    rows = []
    for it in items:
        urgent = it.get("days_left") is not None and it["days_left"] <= 7
        rows.append(f"""
<tr>
  <td style="padding:10px 8px;border-bottom:1px solid #e5e7eb;vertical-align:top">
    <div style="font-size:12px;color:#6b7280">{escape(' / '.join(it['groups']))}・{escape(it['type'])}</div>
    <a href="{escape(it['url'])}" style="font-size:15px;font-weight:600;color:#1d4ed8;text-decoration:none">{escape(it['title'])}</a>
    <div style="font-size:13px;color:#374151;margin-top:2px">{escape(it['unit_name'])}｜案號 {escape(it['job_number'])}</div>
    <div style="font-size:12px;color:#6b7280;margin-top:2px">{escape(it['category'])}｜命中：{escape('、'.join(it['keywords']))}</div>
  </td>
  <td style="padding:10px 8px;border-bottom:1px solid #e5e7eb;text-align:right;white-space:nowrap;vertical-align:top;font-size:14px">{fmt_money(it.get('budget'))}</td>
  <td style="padding:10px 8px;border-bottom:1px solid #e5e7eb;text-align:right;white-space:nowrap;vertical-align:top;font-size:13px;{'color:#b91c1c;font-weight:600' if urgent else 'color:#374151'}">
    {escape(it.get('deadline_str') or '—')}<br>{fmt_days(it)}
  </td>
</tr>""")
    body = "".join(rows) or """
<tr><td colspan="3" style="padding:24px;text-align:center;color:#6b7280">今日沒有符合條件的新案件。</td></tr>"""
    return f"""<!doctype html><html><body style="margin:0;background:#f3f4f6;font-family:'Noto Sans TC','Microsoft JhengHei',Arial,sans-serif">
<div style="max-width:760px;margin:0 auto;background:#fff">
  <div style="padding:20px 20px 12px;border-bottom:3px solid #1d4ed8">
    <div style="font-size:20px;font-weight:700;color:#111827">政府標案每日快報｜{run_date}</div>
    <div style="font-size:13px;color:#6b7280;margin-top:4px">
      掃描 {stats['scanned']} 筆公告（{stats['dates']}）→ 新符合 <b style="color:#1d4ed8">{len(items)}</b> 件
    </div>
  </div>
  <table style="width:100%;border-collapse:collapse">
    <tr style="background:#f9fafb;font-size:12px;color:#6b7280">
      <th style="text-align:left;padding:8px">標案</th><th style="text-align:right;padding:8px">預算</th><th style="text-align:right;padding:8px">截止投標</th>
    </tr>{body}
  </table>
  <div style="padding:16px 20px;font-size:11px;color:#9ca3af">
    資料來源：政府電子採購網（經 g0v 標案 API）。請以政府電子採購網公告內容為準。<br>
    調整關鍵字：編輯 repo 中的 config.yml。
  </div>
</div></body></html>"""


def build_text(items: list[dict], run_date: str, limit: int = 15) -> str:
    if not items:
        return f"📋 標案快報 {run_date}\n今日沒有符合條件的新案件。"
    lines = [f"📋 標案快報 {run_date}｜新案 {len(items)} 件"]
    for i, it in enumerate(items[:limit], 1):
        lines.append(
            f"\n{i}. {it['title']}\n"
            f"🏛 {it['unit_name']}\n"
            f"💰 {fmt_money(it.get('budget'))}｜⏰ {it.get('deadline_str') or '—'}（{fmt_days(it)}）\n"
            f"🔗 {it['url']}"
        )
    if len(items) > limit:
        lines.append(f"\n…另有 {len(items) - limit} 件，請看 Email。")
    text = "\n".join(lines)
    return text[:4900]


def send_email(subject: str, html: str, text: str) -> None:
    host = os.environ.get("SMTP_HOST")
    to = [a.strip() for a in os.environ.get("MAIL_TO", "").split(",") if a.strip()]
    if not host or not to:
        print("ℹ️  未設定 SMTP_HOST / MAIL_TO，略過 Email")
        return
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    pwd = os.environ.get("SMTP_PASS", "")
    sender = os.environ.get("MAIL_FROM", user)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("標案快報", sender))
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    if port == 465:
        s = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        s = smtplib.SMTP(host, port, timeout=30)
        s.starttls()
    with s:
        if user:
            s.login(user, pwd)
        s.sendmail(sender, to, msg.as_string())
    print(f"✅ Email 已寄出 → {', '.join(to)}")


def send_line(text: str) -> None:
    token = os.environ.get("LINE_CHANNEL_TOKEN")
    to = os.environ.get("LINE_TO")  # userId / groupId；留空則 broadcast 給所有好友
    if not token:
        print("ℹ️  未設定 LINE_CHANNEL_TOKEN，略過 LINE")
        return
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"messages": [{"type": "text", "text": text}]}
    if to:
        url = "https://api.line.me/v2/bot/message/push"
        payload["to"] = to
    else:
        url = "https://api.line.me/v2/bot/message/broadcast"
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code >= 300:
        print(f"⚠️  LINE 發送失敗 {r.status_code}: {r.text}")
    else:
        print("✅ LINE 已推播")


def append_csv(items: list[dict], run_date: str) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    new = not MATCH_CSV.exists()
    with MATCH_CSV.open("a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["通知日", "公告日", "公告類型", "機關代碼", "機關", "案號", "案名",
                        "標的分類", "預算", "截止投標", "群組", "命中關鍵字", "分數", "連結"])
        for it in items:
            w.writerow([run_date, it["announce_date"], it["type"], it["unit_id"], it["unit_name"],
                        it["job_number"], it["title"], it["category"], it.get("budget") or "",
                        it.get("deadline_str", ""), "/".join(it["groups"]), "、".join(it["keywords"]),
                        it["score"], it["url"]])


def write_report(items: list[dict], run_date: str) -> None:
    REPORT_DIR.mkdir(exist_ok=True)
    lines = [f"# 標案快報 {run_date}", "", f"新符合 {len(items)} 件", "",
             "| 案名 | 機關 | 預算 | 截止投標 | 命中 |", "|---|---|---|---|---|"]
    for it in items:
        lines.append(f"| [{it['title']}]({it['url']}) | {it['unit_name']} | {fmt_money(it.get('budget'))} "
                     f"| {it.get('deadline_str') or '—'} | {'、'.join(it['keywords'])} |")
    (REPORT_DIR / f"{run_date}.md").write_text("\n".join(lines) + "\n", "utf-8")


# ---------------------------------------------------------------- 主程式
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.yml"))
    ap.add_argument("--date", help="只掃指定日期 YYYYMMDD（測試用）")
    ap.add_argument("--dry-run", action="store_true", help="不寄送、不寫入狀態")
    ap.add_argument("--ignore-seen", action="store_true", help="忽略已通知紀錄（測試用）")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text("utf-8"))
    opts = cfg.get("options", {})
    now = datetime.now(TZ)
    run_date = now.strftime("%Y-%m-%d")

    if args.date:
        dates = [datetime.strptime(args.date, "%Y%m%d").date()]
    else:
        n = int(opts.get("lookback_days", 3))
        dates = [(now - timedelta(days=i)).date() for i in range(n)]

    seen = {} if args.ignore_seen else load_seen()
    scanned, candidates = 0, {}
    for d in dates:
        recs = list_by_date(d)
        scanned += len(recs)
        print(f"📥 {d:%Y-%m-%d} 公告 {len(recs)} 筆")
        for rec in recs:
            it = match_record(rec, cfg)
            if not it:
                continue
            k = seen_key(it)
            if k in seen or k in candidates:
                continue
            candidates[k] = it

    print(f"🔎 關鍵字初篩命中 {len(candidates)} 件，抓取明細中…")
    items = []
    bmin, bmax = opts.get("budget_min"), opts.get("budget_max")
    for k, it in candidates.items():
        it = enrich(it, now)
        time.sleep(0.3 if not FIXTURE_DIR else 0)
        b = it.get("budget")
        if b is not None and bmin and b < bmin:
            continue
        if b is not None and bmax and b > bmax:
            continue
        if opts.get("skip_expired", True) and it.get("days_left") is not None and it["days_left"] < 0:
            continue
        items.append(it)

    items.sort(key=lambda x: (-x["score"], x.get("days_left") if x.get("days_left") is not None else 999))
    stats = {"scanned": scanned, "dates": f"{dates[-1]:%m/%d}–{dates[0]:%m/%d}" if len(dates) > 1 else f"{dates[0]:%m/%d}"}

    print(f"✅ 最終符合 {len(items)} 件")
    for it in items:
        print(f"  [{it['score']}] {it['title']}｜{it['unit_name']}｜{fmt_money(it.get('budget'))}｜{it.get('deadline_str')}｜{'、'.join(it['keywords'])}")

    html = build_html(items, run_date, stats)
    text = build_text(items, run_date)
    if args.dry_run:
        out = ROOT / "preview.html"
        out.write_text(html, "utf-8")
        print(f"\n(dry-run) Email 預覽已存到 {out}\n---- LINE 預覽 ----\n{text}")
        return 0

    if items or opts.get("send_when_empty", True):
        subject = f"【標案快報】{run_date} 新案 {len(items)} 件" + (
            f"｜{items[0]['title'][:20]}…" if items else "")
        send_email(subject, html, text)
        if items or opts.get("line_when_empty", False):
            send_line(text)

    for it in items:
        seen[seen_key(it)] = now.strftime("%Y%m%d")
    # 初篩命中但被預算／過期濾掉的也記錄，避免每天重抓明細
    for k in candidates:
        seen.setdefault(k, now.strftime("%Y%m%d"))
    save_seen(seen)
    append_csv(items, run_date)
    write_report(items, run_date)
    return 0


if __name__ == "__main__":
    sys.exit(main())
