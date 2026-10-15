"""
標案資料庫：把每天查到的全部公告累積起來

  data/db/YYYY-MM.csv      依「公告月份」分檔，每筆一列（同一公告重複查到只更新，不重複）
  data/標案資料庫.xlsx      每次執行重新產生：符合案件 / 近 30 日全部 / 月份統計 / 說明
"""
from __future__ import annotations

import csv
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

DB_DIR = Path(__file__).resolve().parent / "data" / "db"
XLSX = Path(__file__).resolve().parent / "data" / "標案資料庫.xlsx"

FIELDS = ["主鍵", "公告日", "機關", "案號", "案名", "公告類型", "傳輸次數", "採購性質",
          "預算", "截止投標", "符合", "關鍵字群組", "命中關鍵字", "分數", "連結",
          "首次收錄", "最後更新", "資料來源"]


def row_key(r: dict) -> str:
    return f"{r['unit_name']}|{r['job_number']}|{r.get('seq') or '01'}|{r['type']}"


def _roc_to_iso(s: str) -> str:
    """'115/10/20 17:00' → '2026-10-20 17:00'（方便排序，也避免試算表誤判）"""
    import re
    m = re.match(r"\s*(\d{2,3})/(\d{1,2})/(\d{1,2})(.*)", s or "")
    if not m:
        return s or ""
    return f"{int(m[1]) + 1911}-{int(m[2]):02d}-{int(m[3]):02d}{m[4].rstrip()}"


def _to_row(r: dict, m: dict | None, stamp: str, url: str, budget) -> dict:
    d = r.get("date") or ""
    return {
        "主鍵": row_key(r),
        "公告日": f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d,
        "機關": r["unit_name"],
        "案號": r["job_number"],
        "案名": r["title"],
        "公告類型": r["type"],
        "傳輸次數": r.get("seq", ""),
        "採購性質": r.get("category", ""),
        "預算": "" if budget is None else budget,
        "截止投標": _roc_to_iso(r.get("deadline_list", "")),
        "符合": "Y" if m else "",
        "關鍵字群組": "/".join(m["groups"]) if m else "",
        "命中關鍵字": "、".join(m["keywords"]) if m else "",
        "分數": m["score"] if m else "",
        "連結": url,
        "首次收錄": stamp,
        "最後更新": stamp,
        "資料來源": r.get("source", "today"),
    }


def _load(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = {row["主鍵"]: row for row in csv.DictReader(f)}
    for row in rows.values():  # 舊資料民國日期 → 西元
        row["截止投標"] = _roc_to_iso(row.get("截止投標", ""))
    return rows


def _save(path: Path, rows: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows.values(), key=lambda x: (x["公告日"], x["機關"], x["案號"]), reverse=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(ordered)


def upsert(recs: list[dict], match_fn, url_fn, budget_fn, now: datetime) -> tuple[int, int, list[dict]]:
    """寫入資料庫；回傳 (新增筆數, 更新筆數, 新增或變動的列)。"""
    changed: list[dict] = []
    stamp = now.strftime("%Y-%m-%d %H:%M")
    by_month: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        d = r.get("date") or now.strftime("%Y%m%d")
        by_month[f"{d[:4]}-{d[4:6]}"].append(r)

    added = updated = 0
    for month, items in by_month.items():
        path = DB_DIR / f"{month}.csv"
        db = _load(path)
        for r in items:
            m = match_fn(r)
            new = _to_row(r, m, stamp, url_fn(r, m), budget_fn(r))
            k = new["主鍵"]
            if k in db:
                old = db[k]
                new["首次收錄"] = old.get("首次收錄") or stamp
                # 備援來源（今日招標頁）沒有預算，不覆蓋查詢頁已有的資料
                for col in ("預算", "採購性質", "傳輸次數"):
                    if not new[col] and old.get(col):
                        new[col] = old[col]
                if any(str(new[c]) != str(old.get(c, "")) for c in FIELDS if c not in ("最後更新", "首次收錄")):
                    db[k] = new
                    updated += 1
                    changed.append(new)
            else:
                db[k] = new
                added += 1
                changed.append(new)
        _save(path, db)
    return added, updated, changed


def load_all() -> list[dict]:
    rows = []
    for p in sorted(DB_DIR.glob("*.csv")):
        rows.extend(_load(p).values())
    return rows


def build_xlsx(now: datetime, recent_days: int = 30) -> Path | None:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        print("ℹ️  未安裝 openpyxl，略過 Excel")
        return None

    rows = load_all()
    def _score(x):
        try:
            return int(float(x.get("分數") or 0))
        except ValueError:
            return 0
    rows.sort(key=lambda x: (x["公告日"], _score(x)), reverse=True)
    cutoff = (now - timedelta(days=recent_days)).strftime("%Y-%m-%d")

    wb = Workbook()
    head_fill = PatternFill("solid", fgColor="1D4ED8")
    head_font = Font(color="FFFFFF", bold=True)
    hit_fill = PatternFill("solid", fgColor="EEF2FF")
    cols = [("公告日", 11), ("機關", 26), ("案名", 60), ("預算", 14), ("截止投標", 16),
            ("公告類型", 24), ("傳輸次數", 8), ("案號", 18), ("關鍵字群組", 18),
            ("命中關鍵字", 24), ("分數", 6), ("連結", 10), ("首次收錄", 16)]

    def sheet(ws, data, title):
        ws.title = title
        ws.append([c for c, _ in cols])
        for i, (c, w) in enumerate(cols, 1):
            cell = ws.cell(row=1, column=i)
            cell.fill, cell.font = head_fill, head_font
            cell.alignment = Alignment(vertical="center")
            ws.column_dimensions[get_column_letter(i)].width = w
        for r in data:
            vals = []
            for c, _ in cols:
                v = r.get(c, "")
                if c in ("預算", "分數") and str(v).strip():
                    try:
                        v = int(float(v))
                    except ValueError:
                        pass
                vals.append("開啟" if c == "連結" and v else v)
            ws.append(vals)
            n = ws.max_row
            link = r.get("連結")
            if link:
                lc = ws.cell(row=n, column=[c for c, _ in cols].index("連結") + 1)
                lc.hyperlink, lc.font = link, Font(color="1D4ED8", underline="single")
            ws.cell(row=n, column=4).number_format = "#,##0"
            if r.get("符合") == "Y" and title != "符合案件":
                for ci in range(1, len(cols) + 1):
                    ws.cell(row=n, column=ci).fill = hit_fill
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    hits = [r for r in rows if r.get("符合") == "Y" and "更正" not in r.get("公告類型", "")]
    sheet(wb.active, hits, "符合案件")
    sheet(wb.create_sheet(), [r for r in rows if r["公告日"] >= cutoff], f"近{recent_days}日全部")

    # 月份統計
    ws = wb.create_sheet("月份統計")
    ws.append(["月份", "收錄公告數", "符合件數", "符合案預算合計(元)"])
    for i in range(1, 5):
        ws.cell(row=1, column=i).fill, ws.cell(row=1, column=i).font = head_fill, head_font
        ws.column_dimensions[get_column_letter(i)].width = 20
    tot, hit_n, hit_b = Counter(), Counter(), Counter()
    for r in rows:
        mth = r["公告日"][:7]
        tot[mth] += 1
        if r.get("符合") == "Y":
            hit_n[mth] += 1
            try:
                hit_b[mth] += int(float(r.get("預算") or 0))
            except ValueError:
                pass
    for mth in sorted(tot, reverse=True):
        ws.append([mth, tot[mth], hit_n[mth], hit_b[mth]])
        ws.cell(row=ws.max_row, column=4).number_format = "#,##0"

    ws = wb.create_sheet("說明")
    for line in [
        "政府標案資料庫（自動產生，請勿手動編輯此檔；每次執行會覆蓋）",
        f"產生時間：{now:%Y-%m-%d %H:%M}　累計收錄 {len(rows)} 筆、符合 {len(hits)} 筆",
        "資料來源：政府電子採購網「標案查詢」（依 config.yml 的採購性質與查詢天數）",
        "原始資料：repo 的 data/db/YYYY-MM.csv（依公告月份分檔，永久保存）",
        "符合案件：案名命中 config.yml 關鍵字、且未被排除詞排除者（不含更正公告）",
        f"近{recent_days}日全部：所有收錄公告，淺藍底為符合案件，可用篩選功能自行查詢",
        "預算空白：表示該筆來自「今日招標」備援頁，未取得預算",
    ]:
        ws.append([line])
    ws.column_dimensions["A"].width = 100

    XLSX.parent.mkdir(parents=True, exist_ok=True)
    wb.save(XLSX)
    return XLSX
