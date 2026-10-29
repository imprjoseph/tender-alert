#!/usr/bin/env python3
"""
訓練「本司會不會挑這個案子」的評分模型，輸出 model.json（純權重表，執行時不需 scikit-learn）

  正例：資料庫中同仁人工整理的案件（資料來源＝人工整理）
        ＋ Google Sheet／資料庫中狀態填為「追蹤中／準備投標／已投標／得標」者（日後擴充）
  負例：data/raw 系統掃描的全部勞務類公告中，規則與正例都未選中者

用法：python train_model.py            # 需 pip install scikit-learn numpy（只有訓練時需要）
"""
from __future__ import annotations

import csv
import glob
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import tender_alert as ta  # noqa: E402

MODEL = ROOT / "model.json"
from tender_model import NGRAM, char_wb, text_of  # noqa: E402


def main() -> int:
    import numpy as np
    from sklearn.feature_extraction.text import CountVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold

    cfg = yaml.safe_load((ROOT / "config.yml").read_text("utf-8"))
    db, raw = [], []
    for p in glob.glob(str(ROOT / "data/db/*.csv")):
        db += list(csv.DictReader(open(p, encoding="utf-8-sig")))
    for p in glob.glob(str(ROOT / "data/raw/*.csv")):
        raw += list(csv.DictReader(open(p, encoding="utf-8-sig")))
    pos = [r for r in db if r.get("資料來源") == "人工整理"]
    raw = [r for r in raw if "更正" not in r["公告類型"]]
    pos_keys = {(r["機關"], r["案名"]) for r in pos}

    def rule(r):
        return bool(ta.match_record({"type": r["公告類型"], "title": r["案名"], "unit_name": r["機關"], "pk": ""},
                                    cfg, use_model=False))

    neg = [r for r in raw if not rule(r) and (r["機關"], r["案名"]) not in pos_keys]
    X_txt = [text_of(r["案名"], r["機關"]) for r in pos + neg]
    y = np.array([1] * len(pos) + [0] * len(neg))
    print(f"正例 {len(pos)}、負例 {len(neg)}")

    vec = CountVectorizer(analyzer="char_wb", ngram_range=NGRAM, binary=True, min_df=2)
    X = vec.fit_transform(X_txt)
    oof = np.zeros(len(y))
    for tr, te in StratifiedKFold(5, shuffle=True, random_state=0).split(X, y):
        m = LogisticRegression(C=0.5, max_iter=3000, class_weight="balanced").fit(X[tr], y[tr])
        oof[te] = m.predict_proba(X[te])[:, 1]
    th = float(cfg.get("model", {}).get("threshold", 0.9))
    print(f"交叉驗證：門檻 {th} → 正例召回 {np.mean(oof[:len(pos)] >= th):.0%}、負例誤選 {np.mean(oof[len(pos):] >= th):.1%}")

    m = LogisticRegression(C=0.5, max_iter=3000, class_weight="balanced").fit(X, y)
    vocab = vec.get_feature_names_out()
    w = {vocab[i]: round(float(c), 4) for i, c in enumerate(m.coef_[0]) if abs(c) >= 0.005}
    model = {"ngram": list(NGRAM), "intercept": round(float(m.intercept_[0]), 4), "weights": w,
             "trained_on": {"positive": len(pos), "negative": len(neg)}}

    # 驗證純 Python 計分與 scikit-learn 一致
    import math
    probe = X_txt[:200] + X_txt[-200:]
    sk = m.predict_proba(vec.transform(probe))[:, 1]
    mine = [1 / (1 + math.exp(-(model["intercept"] + sum(w.get(g, 0) for g in char_wb(s))))) for s in probe]
    diff = max(abs(a - b) for a, b in zip(sk, mine))
    print(f"純 Python 計分最大誤差 {diff:.4f}（特徵 {len(w)} 個）")
    if diff > 0.02:
        raise SystemExit("計分誤差過大，未輸出 model.json")
    MODEL.write_text(json.dumps(model, ensure_ascii=False, separators=(",", ":")), "utf-8")
    print(f"已輸出 {MODEL.name}（{MODEL.stat().st_size // 1024} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
