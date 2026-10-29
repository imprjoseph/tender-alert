#!/usr/bin/env python3
"""
匯入同仁過去人工整理的每日標案 Excel（每月一檔、每日一頁，每案 7 列一組：
機關名稱／單位名稱／標案名稱／預算金額／截止投標／開標時間）到標案資料庫 data/db。

用法：python tender_import.py 檔案1.xlsx 檔案2.xlsx ...   （可重複執行，不會重複匯入）
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path
from urllib.parse import quote

import openpyxl
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tender_alert as ta  # noqa: E402
import tender_db as db  # noqa: E402

SOURCE = "人工整理"


def _clean(v) -> str:
    return re.sub(r"\s+", " ", ("" if v is None else str(v)).replace("\t", " ")).strip()


def parse_file(path: Path) -> list[dict]:
    wb = openpyxl.load_workbook(path, data_only=True)
    out = []
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        day = next((v.date() for r in rows[:4] for v in r if isinstance(v, dt.datetime)), None)
        cur = None
        for r in rows:
            lab = _clean(r[1]) if len(r) > 1 else ""
            val = _clean(r[2]) if len(r) > 2 else ""
            if lab == "機關名稱":
                if cur:
                    out.append(cur)
                cur = {"day": day, "機關名稱": val}
            elif cur is not None and lab:
                cur[lab] = val
        if cur:
            out.append(cur)
    return [x for x in out if x.get("標案名稱") and x.get("day")]


def search_url(title: str, day: dt.date) -> str:
    s, e = day - dt.timedelta(days=5), day + dt.timedelta(days=2)
    return (f"{ta.PCC}/prkms/tender/common/basic/readTenderBasic?pageSize=10&firstSearch=true&searchType=basic"
            f"&isBinding=N&isLogIn=N&level_1=on&tenderType=TENDER_DECLARATION&tenderWay=TENDER_WAY_ALL_DECLARATION"
            f"&dateType=isDate&tenderStartDate={quote(s.strftime('%Y/%m/%d'), safe='')}"
            f"&tenderEndDate={quote(e.strftime('%Y/%m/%d'), safe='')}&tenderName={quote(title[:30], safe='')}")


def main(files: list[str]) -> int:
    cfg = yaml.safe_load((ta.ROOT / "config.yml").read_text("utf-8"))
    stamp = dt.datetime.now(ta.TZ).strftime("%Y-%m-%d %H:%M")
    norm = lambda s: re.sub(r"\s+", "", s or "")  # noqa: E731

    items: dict[tuple, dict] = {}
    for f in files:
        for x in parse_file(Path(f)):
            k = (norm(x["機關名稱"]), norm(x["標案名稱"]))
            if k not in items or x["day"] < items[k]["day"]:   # 同一案多日出現：保留最早的一天
                items[k] = x
    print(f"讀取 {len(files)} 個檔案，不重複案件 {len(items)} 筆")

    rows = []
    for x in items.values():
        title, unit = x["標案名稱"], x["機關名稱"]
        m = ta.match_record({"type": "公開招標公告", "title": title, "unit_name": unit, "pk": ""}, cfg)
        rows.append({
            "主鍵": f"{unit}|{title}|{SOURCE}",
            "公告日": x["day"].isoformat(),
            "機關": unit,
            "案號": "",
            "案名": title,
            "公告類型": "人工整理" + (f"｜{x['單位名稱']}" if x.get("單位名稱") and x["單位名稱"] != unit else ""),
            "傳輸次數": "",
            "採購性質": "",
            "預算": ta.parse_money(x.get("預算金額", "")) or "",
            "截止投標": db._roc_to_iso(x.get("截止投標", "")),
            "符合": "Y",
            "關鍵字群組": "/".join(m["groups"]) if m else "人工篩選",
            "命中關鍵字": "、".join(m["keywords"]) if m else "",
            "分數": m["score"] if m else "",
            "連結": search_url(title, x["day"]),
            "首次收錄": stamp,
            "最後更新": stamp,
            "資料來源": SOURCE,
        })

    added = 0
    changed = []
    by_month: dict[str, list] = {}
    for r in rows:
        by_month.setdefault(r["公告日"][:7], []).append(r)
    for month, rs in by_month.items():
        path = db.DB_DIR / f"{month}.csv"
        cur = db._load(path)
        for r in rs:
            if r["主鍵"] not in cur:
                cur[r["主鍵"]] = r
                added += 1
                changed.append(r)
        db._save(path, cur)
    hit = sum(1 for r in rows if r["關鍵字群組"] != "人工篩選")
    print(f"新增 {added} 筆（其中 {hit} 筆也符合現行關鍵字規則）；資料庫現有 {len(db.load_all())} 筆")
    db.build_xlsx(dt.datetime.now(ta.TZ))
    ta.push_sheet(changed)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
