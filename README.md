# 政府標案每日快報（GitHub Actions 版）

每天早上 07:30 左右，自動掃描政府電子採購網「今日招標」的全部公告，依 `config.yml` 的關鍵字篩選，
把符合的案件寄到 Email（可選 LINE），並累積到 `data/matches.csv` 作為標案情報庫。

- 成本：0 元（GitHub Actions 免費額度內，每次約 1 分鐘）
- 資料來源：政府電子採購網官方頁面（今日招標 + 標案明細）
- 不需要伺服器

---

## 運作邏輯

```
GitHub Actions（每天 07:23 TW）
   │
   ├─ 1. 抓「今日招標」全部公告（每天約 1,000–1,500 筆，一次請求）
   ├─ 2. 初篩：公告類型 → 機關 → 案名排除詞 → 關鍵字群組
   ├─ 3. 跳過已通知過的案件（data/seen.json）
   ├─ 4. 抓命中案件明細頁：標的分類、預算、截止投標；排除工程／財物類
   ├─ 5. 預算區間、已過期過濾 → 依「優先度分數、剩餘天數」排序
   └─ 6. 寄 Email / 推 LINE → 寫入 matches.csv、reports/日期.md → commit 回 repo
```

---

## 部署步驟（約 15 分鐘）

### Step 1｜建立 repo
1. GitHub 新增 **Private** repo，例如 `imprjoseph/tender-alert`
2. 把本資料夾所有檔案上傳（含隱藏資料夾 `.github/`）

### Step 2｜開啟寫入權限
`Settings → Actions → General → Workflow permissions` → 選 **Read and write permissions** → Save

### Step 3｜設定 Secrets
`Settings → Secrets and variables → Actions → New repository secret`

| Secret | 必填 | 說明 / 範例 |
|---|---|---|
| `SMTP_HOST` | ✔ | Gmail：`smtp.gmail.com`；Outlook/M365：`smtp.office365.com` |
| `SMTP_PORT` | ✔ | `587` |
| `SMTP_USER` | ✔ | 寄件帳號 |
| `SMTP_PASS` | ✔ | **應用程式密碼**（非登入密碼，見下方） |
| `MAIL_TO` | ✔ | 收件人，逗號分隔：`joseph@impr.com.tw,emily@impr.com.tw` |
| `MAIL_FROM` | | 不填則同 `SMTP_USER` |
| `LINE_CHANNEL_TOKEN` | | LINE Messaging API 的 Channel access token |
| `LINE_TO` | | 群組 ID 或使用者 ID；不填則推給官方帳號所有好友 |

**寄件信箱建議用 Gmail 專用帳號**（例如 `impr.tender.bot@gmail.com`）：
Google 帳戶 → 安全性 → 開啟兩步驟驗證 → 「應用程式密碼」產生 16 碼密碼 → 填入 `SMTP_PASS`。

> M365（Outlook）也可以，但需 IT 管理員對該信箱開啟「SMTP AUTH」，很多公司預設關閉。

### Step 4｜先試跑
`Actions → 每日標案快報 → Run workflow`
- `dry_run = true`：不寄信，完成後在該次執行頁下載 `preview` 看 Email 樣式
- `dry_run = false`：正式寄出，並開始記錄已通知案件

確認收到信後就不用再管它，每天會自動執行（週末無新案不寄信）。

---

## 調整關鍵字（日常維護）

直接在 GitHub 網頁編輯 `config.yml` → Commit，下次執行生效。

| 想做的事 | 改哪裡 |
|---|---|
| 新增關鍵字 | `keyword_groups` 裡的 `keywords` |
| 案名有某字就不要 | `filters.title_exclude` |
| 只看特定部會 | `filters.unit_include: [經濟部, 數位發展部]` |
| 只看 100 萬以上 | `options.budget_min: 1000000` |
| 想看更正公告（截止延期） | 刪掉 `type_exclude` 裡的 `更正` |
| 改執行時間 | `.github/workflows/daily.yml` 的 `cron`（UTC 時間，台灣 −8 小時） |

**關鍵字設計心法**
- 只看案名比對；標的分類在明細頁取得後用於排除工程/財物、與 `category_boost` 加分排序。
- 先寬後窄：前兩週先收多一點，看哪些是雜訊，再加進 `title_exclude`。
- 「活動」會撞到「活動中心整修」，已用 `工程類` 排除 + 群組 `exclude` 處理。

---

## LINE 推播設定（選用）

> LINE Notify 已於 2025 年停止服務，請勿使用舊教學。

1. [LINE Developers](https://developers.line.biz/) 建立 Provider → 建立 **Messaging API** channel（會同時產生一個 LINE 官方帳號）
2. Messaging API 頁籤 → 產生 **Channel access token (long-lived)** → 填入 `LINE_CHANNEL_TOKEN`
3. 推給公司群組：把官方帳號加入 LINE 群組，取得群組 ID 填入 `LINE_TO`
   （或不填 `LINE_TO`，讓同事加官方帳號好友，改用 broadcast）
4. 免費方案每月有訊息則數上限；本系統每天最多 1 則，broadcast 會依好友數計算則數

---

## 輸出檔案

| 檔案 | 用途 |
|---|---|
| `data/matches.csv` | 所有通知過的案件（Excel 可直接開），可匯入標案情報庫 |
| `data/seen.json` | 已通知紀錄，避免重複；保留 120 天 |
| `reports/YYYY-MM-DD.md` | 每日清單，GitHub 上可直接閱讀 |

---

## 本機測試

```bash
pip install -r requirements.txt
python tender_alert.py --dry-run                 # 掃今日招標，產生 preview.html
python tender_alert.py --dry-run --ignore-seen   # 忽略已通知紀錄
```

---

## 常見問題

**Q：排程沒在 07:30 準時跑？**
GitHub 排程在尖峰時段會延遲數分鐘到半小時，屬正常現象。需要準時可改用手動觸發或外部排程。

**Q：採購網連不上或改版怎麼辦？**
程式會重試 4 次；仍失敗則該次 Action 顯示紅燈並在 Summary 顯示錯誤原因，GitHub 會寄失敗通知給 repo 擁有者。若某天失敗，那天的公告需手動到採購網補查。

**Q：為什麼不用 g0v API？**
g0v 標案 API 前面有 Cloudflare 機器人檢查，會擋 GitHub 伺服器的連線（實測 403），因此改抓官方頁面。

**Q：公開 repo 可以嗎？**
建議用 Private。Public repo 若 60 天無 commit 會自動停用排程（本系統每天都會 commit，實務上不會發生），但案件清單與關鍵字策略會公開。

**Q：資料使用規範**
資料著作權屬政府電子採購網，本系統供公司內部商機追蹤使用，對外引用請註明「資料來源：政府電子採購網」。
