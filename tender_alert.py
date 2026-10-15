#!/usr/bin/env python3
"""
政府電子採購網 每日標案關鍵字通知
資料來源：政府電子採購網 https://web.pcc.gov.tw （官方「今日招標」頁 + 標案明細頁）

流程：
  1. 抓「今日招標」全部公告（一次請求，約 1,000–1,500 筆）
  2. 依「公告類型 → 機關 → 案名排除詞 → 關鍵字群組」初篩
  3. 已通知過的案件跳過（data/seen.json）
  4. 對命中案件抓明細頁：標的分類、預算、截止投標、決標方式
  5. 依標的分類二次過濾（排除工程／財物）、預算區間、已過期
  6. 寄 Email / 推 LINE，並寫入 data/matches.csv 與 reports/

用法：
  python tender_alert.py                 # 正式執行
  python tender_alert.py --dry-run       # 只印結果，不寄送、不更新 seen
  python tender_alert.py --ignore-seen   # 忽略已通知紀錄（測試用）
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
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from html import escape, unescape
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

import tender_db

TZ = ZoneInfo("Asia/Taipei")
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
REPORT_DIR = ROOT / "reports"
SEEN_FILE = DATA_DIR / "seen.json"
MATCH_CSV = DATA_DIR / "matches.csv"

PCC = "https://web.pcc.gov.tw"
TODAY_URL = f"{PCC}/prkms/today/common/todayTender"
DETAIL_URL = f"{PCC}/tps/QueryTender/query/searchTenderDetail?pkPmsMain={{pk}}"
FIXTURE_DIR = os.environ.get("PCC_FIXTURE_DIR")  # 離線測試用：today.html、detail_<pk>.html
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9"})


# ---------------------------------------------------------------- 資料抓取
def _get_html(url: str, fixture: str) -> str:
    if FIXTURE_DIR:
        f = Path(FIXTURE_DIR) / fixture
        return f.read_text("utf-8", errors="replace") if f.exists() else ""
    last_err = None
    for attempt in range(4):
        try:
            r = _session.get(url, timeout=40)
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.text
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(4 * (attempt + 1))
    raise RuntimeError(f"採購網連線失敗：{url} → {last_err}")


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()


SEARCH_URL = (f"{PCC}/prkms/tender/common/basic/readTenderBasic?pageSize=100&firstSearch=true"
              "&searchType=basic&isBinding=N&isLogIn=N&level_1=on&tenderType=TENDER_DECLARATION"
              "&tenderWay=TENDER_WAY_ALL_DECLARATION&dateType=isDate&radProctrgCate={cate}"
              "&tenderStartDate={start}&tenderEndDate={end}")
CATE_CODE = {"工程類": "RAD_PROCTRG_CATE_1", "財物類": "RAD_PROCTRG_CATE_2", "勞務類": "RAD_PROCTRG_CATE_3", "不限": ""}


def fetch_search(start: datetime, end: datetime, cate: str = "勞務類", max_pages: int = 30) -> list[dict]:
    """官方「標案查詢」：依採購性質＋公告日期區間，逐頁取回（每頁 100 筆，間隔 4 秒）。"""
    from urllib.parse import quote
    base = SEARCH_URL.format(cate=CATE_CODE.get(cate, ""), start=quote(start.strftime("%Y/%m/%d"), safe=""),
                             end=quote(end.strftime("%Y/%m/%d"), safe=""))
    recs, total = [], None
    for page in range(1, max_pages + 1):
        url = base if page == 1 else f"{base}&firstSearch=false&d-49738-p={page}"
        html = _get_html(url, f"search_p{page}.html")
        if "驗證碼檢核" in html:
            raise CaptchaBlocked()
        flat = re.sub(r"\s+", " ", html)
        if total is None:
            m = re.search(r'共有<span class="red">\s*([\d,]+)\s*</span>筆', flat)
            total = int(m.group(1).replace(",", "")) if m else 0
        tb = flat[flat.find("<tbody>", flat.find('id="tpam"')):]
        rows = re.findall(r'<tr class="tb_b[23]">(.*?)</tr>', tb)
        for r in rows:
            tds = re.findall(r"<td[^>]*>(.*?)</td>", r)
            if len(tds) < 9:
                continue
            title = re.search(r'pageCode2Img\("(.*?)"\)', tds[2])
            case_cell = _text(re.sub(r"<script.*?</script>", " ", tds[2]))
            is_corr = "更正公告" in case_cell
            job = case_cell.split("(")[0].strip().split(" ")[0]
            pk = re.search(r"tpam\?pk=([^\"&]+)", r)
            way = _text(tds[4])
            ann = roc_to_dt(_text(tds[6]))
            recs.append({
                "date": ann.strftime("%Y%m%d") if ann else "",
                "type": f"{way}公告" + ("（更正公告）" if is_corr else ""),
                "unit_name": _text(tds[1]),
                "title": unescape(title.group(1)) if title else case_cell,
                "job_number": job,
                "seq": _text(tds[3]),
                "pk": pk.group(1) if pk else "",
                "category": _text(tds[5]),
                "deadline_list": _text(tds[7]),
                "budget_text": _text(tds[8]),
                "source": "search",
            })
        if len(rows) < 100 or (total and len(recs) >= total):
            break
        if not FIXTURE_DIR:
            time.sleep(4)
    return recs


def fetch_today() -> list[dict]:
    """解析「今日招標」頁：每個公告類型一個區塊，底下是案件表格。"""
    html = _get_html(TODAY_URL, "today.html")
    if not html:
        raise RuntimeError("今日招標頁為空")
    flat = re.sub(r"\s+", " ", html)
    # 以各區塊標題切段：□公開招標公告□ 總筆數:597
    parts = re.split(r"<strong>\s*□(.+?)□\s*總筆數:\s*\d+\s*</strong>", flat)
    recs, today = [], datetime.now(TZ).strftime("%Y%m%d")
    for i in range(1, len(parts), 2):
        ttype, body = parts[i].strip(), parts[i + 1]
        tb = re.search(r"<tbody>(.*?)</tbody>", body)
        if not tb:
            continue
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tb.group(1)):
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr)
            if len(tds) < 5:
                continue
            pk = re.search(r"pkPmsMain=([^\"&]+)", tds[3])
            recs.append({
                "date": today,
                "type": ttype,
                "unit_name": _text(tds[1]),
                "title": _text(tds[2]),
                "job_number": _text(tds[3]),
                "pk": pk.group(1) if pk else "",
                "deadline_list": _text(tds[4]),
            })
    if not recs:
        raise RuntimeError("今日招標頁解析不到任何案件，網頁結構可能已改版")
    return recs


def _field(flat: str, label: str) -> str:
    m = re.search(r'<td[^>]*class="tbg_\w+"[^>]*>\s*' + re.escape(label) +
                  r'\s*</td>\s*<td[^>]*>(.*?)</td>\s*</tr>', flat)
    return _text(m.group(1)) if m else ""


def _parse_detail(html: str) -> dict:
    flat = re.sub(r"\s+", " ", html)
    d = {
        "category": _field(flat, "標的分類"),
        "budget_text": _field(flat, "預算金額"),
        "deadline": _field(flat, "截止投標"),
        "award_method": _field(flat, "決標方式"),
        "tender_way": _field(flat, "招標方式"),
        "unit_id": _field(flat, "機關代碼"),
    }
    if not re.search(r"\d", d["budget_text"] or ""):
        m = re.search(r'isVal = "(\d+)"', flat)
        d["budget_text"] = m.group(1) if m else d["budget_text"]
    return d


class CaptchaBlocked(Exception):
    pass


def fetch_detail(pk: str) -> dict:
    """抓明細頁。採購網對短時間大量查詢會要求驗證碼；遇到即停止，不重試、不繞過。"""
    url = DETAIL_URL.format(pk=pk)
    if FIXTURE_DIR:
        html = _get_html(url, f"detail_{pk}.html")
        if "驗證碼檢核" in html:
            raise CaptchaBlocked()
        return _parse_detail(html) if html else {}
    try:
        r = _session.get(url, timeout=40, headers={"Referer": TODAY_URL})
        r.encoding = "utf-8"
    except Exception:  # noqa: BLE001
        return {}
    if "驗證碼檢核" in r.text:
        raise CaptchaBlocked()
    return _parse_detail(r.text)


# ---------------------------------------------------------------- 篩選
def contains_any(text: str, words: list[str]) -> list[str]:
    return [w for w in words or [] if w and w in (text or "")]


def match_record(rec: dict, cfg: dict) -> dict | None:
    """初篩：只用清單頁就有的欄位（類型、機關、案名）。"""
    ttype, title, unit = rec["type"], rec["title"], rec["unit_name"]
    f = cfg["filters"]
    if f.get("type_include") and not contains_any(ttype, f["type_include"]):
        return None
    if contains_any(ttype, f.get("type_exclude", [])):
        return None
    if f.get("unit_include") and not contains_any(unit, f["unit_include"]):
        return None
    if contains_any(unit, f.get("unit_exclude", [])):
        return None
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
    if not groups_hit:
        return None

    return {
        **rec,
        "groups": groups_hit,
        "keywords": sorted(set(kw_hit), key=kw_hit.index),
        "score": score,
        "url": (f"{PCC}/prkms/urlSelector/common/tpam?pk={rec['pk']}" if rec.get("source") == "search"
                else DETAIL_URL.format(pk=rec["pk"])) if rec["pk"] else TODAY_URL,
    }


def roc_to_dt(s: str) -> datetime | None:
    """'115/11/11 17:00' → datetime"""
    m = re.search(r"(\d{2,3})/(\d{1,2})/(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", s or "")
    if not m:
        return None
    try:
        return datetime(int(m[1]) + 1911, int(m[2]), int(m[3]),
                        int(m[4] or 23), int(m[5] or 59), tzinfo=TZ)
    except ValueError:
        return None


def parse_money(s: str) -> int | None:
    m = re.search(r"\d[\d,]*", s or "")
    return int(m[0].replace(",", "")) if m else None


def enrich(item: dict, now: datetime, cfg: dict, d: dict) -> dict:
    """套用明細（可能為空）並做二次過濾；被濾掉者標記 _drop。"""
    item["detail_ok"] = bool(d.get("category") or d.get("deadline"))
    f = cfg["filters"]
    item["category"] = d.get("category") or item.get("category", "")
    if contains_any(item["category"], f.get("category_exclude", [])):
        item["_drop"] = "分類"
        return item
    boost = contains_any(item["category"], f.get("category_boost", []))
    if boost:
        item["score"] += f.get("category_boost_score", 2)
        item["keywords"] += [b for b in boost if b not in item["keywords"]]
    item["unit_id"] = d.get("unit_id", "")
    item["budget"] = parse_money(d.get("budget_text") or item.get("budget_text", ""))
    item["deadline_str"] = d.get("deadline") or item.get("deadline_list", "")
    dl = roc_to_dt(item["deadline_str"])
    item["days_left"] = (dl.date() - now.date()).days if dl else None
    item["award_method"] = d.get("award_method", "")
    item["announce_date"] = item["date"]
    return item


# ---------------------------------------------------------------- 狀態檔
def load_seen() -> dict:
    if not SEEN_FILE.exists():
        return {}
    raw = json.loads(SEEN_FILE.read_text("utf-8"))
    out = {}
    for k, v in raw.items():  # 舊格式「機關|案號|公告類型」→「機關|案號|01」
        parts = k.split("|")
        if len(parts) >= 3 and not parts[-1].isdigit():
            k = f"{parts[0]}|{parts[1]}|01"
        out[k] = v
    return out


def save_seen(seen: dict, keep_days: int = 120) -> None:
    cutoff = (datetime.now(TZ) - timedelta(days=keep_days)).strftime("%Y%m%d")
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    DATA_DIR.mkdir(exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=0), "utf-8")


def seen_key(it: dict) -> str:
    # 同一案號第二次以上公告（傳輸次數 02、03…，多為流標重新招標）視為新案再通知
    return f"{it['unit_name']}|{it['job_number']}|{it.get('seq') or '01'}"


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
      {stats['dates']} 共 {stats['scanned']} 筆→ 新符合 <b style="color:#1d4ed8">{len(items)}</b> 件
    </div>
  </div>
  <table style="width:100%;border-collapse:collapse">
    <tr style="background:#f9fafb;font-size:12px;color:#6b7280">
      <th style="text-align:left;padding:8px">標案</th><th style="text-align:right;padding:8px">預算</th><th style="text-align:right;padding:8px">截止投標</th>
    </tr>{body}
  </table>
  <div style="padding:16px 20px;font-size:11px;color:#9ca3af">
    資料來源：政府電子採購網。請以採購網公告內容為準。<br>
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


def send_email(subject: str, html: str, text: str, attachment: Path | None = None) -> None:
    host = os.environ.get("SMTP_HOST")
    to = [a.strip() for a in os.environ.get("MAIL_TO", "").split(",") if a.strip()]
    if not host or not to:
        print("ℹ️  未設定 SMTP_HOST / MAIL_TO，略過 Email")
        return
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    pwd = os.environ.get("SMTP_PASS", "")
    sender = os.environ.get("MAIL_FROM", user)

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = formataddr(("標案快報", sender))
    msg["To"] = ", ".join(to)
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text, "plain", "utf-8"))
    alt.attach(MIMEText(html, "html", "utf-8"))
    msg.attach(alt)
    if attachment and attachment.exists():
        from email.mime.application import MIMEApplication
        part = MIMEApplication(attachment.read_bytes(), Name=attachment.name)
        part.add_header("Content-Disposition", "attachment", filename=("utf-8", "", attachment.name))
        msg.attach(part)

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


# ---------------------------------------------------------------- 資料庫
PENDING_FILE = DATA_DIR / "sheet_pending.json"


def push_sheet(rows: list[dict]) -> bool:
    """把新增／變動的列送到 Google Sheet（Apps Script Web App）。失敗的列存起來下次重送。"""
    url, token = os.environ.get("GAS_URL"), os.environ.get("GAS_TOKEN")
    pending = json.loads(PENDING_FILE.read_text("utf-8")) if PENDING_FILE.exists() else []
    if not url or not token:
        if rows:
            print("ℹ️  未設定 GAS_URL / GAS_TOKEN，略過雲端試算表同步")
        return False
    merged = {r["主鍵"]: r for r in pending}
    merged.update({r["主鍵"]: r for r in rows})
    allrows = list(merged.values())
    if not allrows:
        return True
    failed, added, updated = [], 0, 0
    for i in range(0, len(allrows), 800):
        chunk = allrows[i:i + 800]
        try:
            r = requests.post(url, json={"token": token, "rows": chunk}, timeout=300)
            res = r.json()
            if not res.get("ok"):
                raise RuntimeError(res.get("error"))
            added += res["all"]["added"]
            updated += res["all"]["updated"]
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  試算表同步失敗（{type(e).__name__}: {str(e)[:200]}），{len(chunk)} 筆下次重送")
            failed.extend(chunk)
    DATA_DIR.mkdir(exist_ok=True)
    if failed:
        PENDING_FILE.write_text(json.dumps(failed, ensure_ascii=False), "utf-8")
    elif PENDING_FILE.exists():
        PENDING_FILE.unlink()
    msg = f"雲端試算表：新增 {added}、更新 {updated}" + (f"，{len(failed)} 筆待重送" if failed else "")
    print(f"☁️  {msg}")
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::{'warning' if failed else 'notice'} title=雲端試算表::{msg}")
    return not failed


def save_db(recs: list[dict], cfg: dict, now: datetime) -> tuple[int, int]:
    def url_fn(r, m):
        if not r.get("pk"):
            return TODAY_URL
        return (f"{PCC}/prkms/urlSelector/common/tpam?pk={r['pk']}" if r.get("source") == "search"
                else DETAIL_URL.format(pk=r["pk"]))
    added, updated, changed = tender_db.upsert(recs, lambda r: match_record(r, cfg), url_fn,
                                               lambda r: parse_money(r.get("budget_text", "")), now)
    push_sheet(changed)
    return added, updated


def backfill(cfg: dict, now: datetime, days: int, cate: str) -> int:
    """逐日回補資料庫；遇驗證碼即停止並保存已取得的部分。"""
    total = 0
    for i in range(days):
        d = now - timedelta(days=i)
        try:
            recs = fetch_search(d, d, cate)
        except CaptchaBlocked:
            print(f"⚠️  {d:%m/%d} 遇驗證碼，停止回補（已保存 {total} 筆）")
            break
        added, updated = save_db(recs, cfg, now)
        total += len(recs)
        print(f"🗄  {d:%Y-%m-%d} {len(recs)} 筆（新增 {added}、更新 {updated}）")
        if not FIXTURE_DIR:
            time.sleep(8)
    tender_db.build_xlsx(now)
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::notice title=資料庫回補::{days} 天，共 {total} 筆")
    return 0


# ---------------------------------------------------------------- 主程式
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.yml"))
    ap.add_argument("--dry-run", action="store_true", help="不寄送、不寫入狀態")
    ap.add_argument("--ignore-seen", action="store_true", help="忽略已通知紀錄（測試用）")
    ap.add_argument("--db-only", action="store_true", help="只更新資料庫與 Excel，不寄送、不標記已通知")
    ap.add_argument("--sync-all", action="store_true", help="把資料庫全部資料同步到雲端試算表（初次或重建用）")
    ap.add_argument("--days", type=int, help="查詢天數（覆蓋 config；回補資料庫用，逐日查詢）")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text("utf-8"))
    opts = cfg.get("options", {})
    now = datetime.now(TZ)
    run_date = now.strftime("%Y-%m-%d")
    is_weekend = now.weekday() >= 5

    if args.sync_all:
        rows = tender_db.load_all()
        print(f"☁️  全部同步 {len(rows)} 筆")
        return 0 if push_sheet(rows) else 1

    seen = {} if args.ignore_seen else load_seen()
    lookback = max(1, int(args.days or opts.get("lookback_days", 3)))
    start = now - timedelta(days=lookback - 1)
    cate = opts.get("category", "勞務類")
    source_note = ""
    if args.db_only and args.days:
        return backfill(cfg, now, lookback, cate)
    try:
        recs = fetch_search(start, now, cate)
        source = "search"
        source_note = f"標案查詢（{cate}，{start:%m/%d}–{now:%m/%d}）"
    except Exception as e:  # noqa: BLE001  — 查詢頁失敗時改用今日招標頁
        print(f"⚠️  標案查詢失敗（{type(e).__name__}: {e}），改用今日招標頁")
        recs = fetch_today()
        source = "today"
        source_note = f"今日招標（{now:%m/%d}，查詢頁失敗備援）"
    print(f"📥 {source_note} {len(recs)} 筆")

    xlsx = None
    if not args.dry_run:
        added, updated = save_db(recs, cfg, now)
        xlsx = tender_db.build_xlsx(now)
        print(f"🗄  資料庫：新增 {added}、更新 {updated} 筆")
        if args.db_only:
            return 0

    candidates = {}
    for rec in recs:
        it = match_record(rec, cfg)
        if not it:
            continue
        k = seen_key(it)
        if k in seen or k in candidates:  # 同案同次只通知一次（更正公告由 type_exclude 排除）
            continue
        candidates[k] = it

    print(f"🔎 關鍵字初篩命中 {len(candidates)} 件，抓取明細中…")
    items = []
    bmin, bmax = opts.get("budget_min"), opts.get("budget_max")
    drop = {"分類": 0, "預算": 0, "過期": 0}
    max_detail = int(opts.get("max_detail", 4))
    ranked = sorted(candidates.values(), key=lambda x: -x["score"])
    detail_ok, blocked = 0, False
    for n, it in enumerate(ranked):
        d = {}
        if source == "today" and n < max_detail and it["pk"] and not blocked:
            try:
                d = fetch_detail(it["pk"])
                detail_ok += bool(d)
            except CaptchaBlocked:
                blocked = True
                print("⚠️  採購網要求驗證碼，停止抓取明細（其餘案件不含預算/分類）")
            if not FIXTURE_DIR:
                time.sleep(3)
        it = enrich(it, now, cfg, d)
        if it.get("_drop"):
            drop[it["_drop"]] += 1
            continue
        b = it.get("budget")
        if b is not None and ((bmin and b < bmin) or (bmax and b > bmax)):
            drop["預算"] += 1
            continue
        if opts.get("skip_expired", True) and it.get("days_left") is not None and it["days_left"] < 0:
            drop["過期"] += 1
            continue
        items.append(it)

    items.sort(key=lambda x: (-x["score"], x["days_left"] if x.get("days_left") is not None else 999))
    stats = {"scanned": len(recs), "dates": source_note}

    print(f"✅ 最終符合 {len(items)} 件（排除 {drop}，明細 {detail_ok} 件{'，遇驗證碼停止' if blocked else ''}）")
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::notice title=標案快報::{source_note} {len(recs)} 筆，初篩 {len(candidates)} 件，"
              f"排除 分類{drop['分類']}/預算{drop['預算']}/過期{drop['過期']}，明細 {detail_ok} 件"
              f"{'（遇驗證碼停止）' if blocked else ''}，最終 {len(items)} 件"
              + ("｜" + "；".join(i['title'][:25] for i in items[:5]) if items else ""))
    for it in items:
        print(f"  [{it['score']}] {it['title']}｜{it['unit_name']}｜{fmt_money(it.get('budget'))}"
              f"｜{it.get('deadline_str')}｜{it.get('category')}｜{'、'.join(it['keywords'])}")

    html = build_html(items, run_date, stats)
    text = build_text(items, run_date)
    if args.dry_run:
        (ROOT / "preview.html").write_text(html, "utf-8")
        print(f"\n(dry-run) Email 預覽已存到 preview.html\n---- LINE 預覽 ----\n{text}")
        return 0

    if not os.environ.get("SMTP_HOST") and not os.environ.get("LINE_CHANNEL_TOKEN"):
        raise RuntimeError("尚未設定寄送管道：請在 GitHub Settings → Secrets → Actions 設定 SMTP_HOST 等 Email 參數")
    send_empty = opts.get("send_when_empty", True) and not is_weekend
    if items or send_empty:
        subject = f"【標案快報】{run_date} 新案 {len(items)} 件" + (
            f"｜{items[0]['title'][:20]}…" if items else "")
        send_email(subject, html, text, xlsx if opts.get("attach_excel", True) else None)
        if items or opts.get("line_when_empty", False):
            send_line(text)
    else:
        print("ℹ️  無新案（週末不寄空報）")

    stamp = now.strftime("%Y%m%d")
    for k in candidates:          # 含被分類／預算／過期濾掉者，避免每天重抓明細
        seen.setdefault(k, stamp)
    save_seen(seen)
    if items:
        append_csv(items, run_date)
        write_report(items, run_date)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        import traceback
        print(traceback.format_exc())
        if os.environ.get("GITHUB_ACTIONS"):
            msg = f"{type(e).__name__}: {e}"[:900].replace("\n", " ")
            print(f"::error title=標案快報執行失敗::{msg}")
        sys.exit(1)
