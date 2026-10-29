"""AI 模型共用的文字切詞（訓練 train_model.py 與執行 tender_alert.py 共用）"""
import re

NGRAM = (2, 3)


def text_of(title: str, unit: str) -> str:
    return re.sub(r"\d+", "0", title or "") + " ＠" + (unit or "")


def char_wb(text: str, lo: int = NGRAM[0], hi: int = NGRAM[1]) -> set:
    """與 scikit-learn CountVectorizer(analyzer='char_wb', lowercase=True) 相同的切法"""
    out = set()
    for w in re.sub(r"\s\s+", " ", text.lower()).split():
        w = f" {w} "
        for n in range(lo, hi + 1):
            off = 0
            out.add(w[off:off + n])
            while off + n < len(w):
                off += 1
                out.add(w[off:off + n])
            if off == 0:
                break
    return out
