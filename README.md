# 政府標案每日快報（GitHub Actions 版）

每天早上 07:30 左右，以政府電子採購網官方「標案查詢」（勞務類＋近 3 日）取回全部公告，依 `config.yml` 的關鍵字篩選，
把符合的案件寄到 Email（可選 LINE），並累積到 `data/matches.csv` 作為標案情報庫。

- 成本：0 元（GitHub Actions 免費額度內，每次約 1 分鐘）
- 資料來源：政府電子採購網官方「標案查詢」頁；失敗時自動改用「今日招標」頁備援
- 不需要伺服器

---

## 運作邏輯

```
GitHub Actions（每天 07:23 TW）
   │
   ├─ 1. 官方標案查詢：採購性質＝勞務類、公告日＝近 3 日（每頁 100 筆，間隔 4 秒翻頁）
   │     每筆自帶：機關、案號、案名、招標方式、公告日、截止投標、預算
   ├─ 2. 篩選：公告類型 → 機關 → 案名排除詞 → 關鍵字群組
   ├─ 3. 跳過已通知過的案件（data/seen.json；同案號第二次公告會再通知）
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

確認收到信後就不用再管它，每天會自動執行（週末無新案不寄信）。查近 3 日，所以某天失敗隔天會自動補上。

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
| 也看財物類（例如印刷品） | `options.category: 不限`（翻頁數會變 3 倍） |
| 改執行時間 | `.github/workflows/daily.yml` 的 `cron`（UTC 時間，台灣 −8 小時） |

**關鍵字設計心法**
- 只比對案名；採購性質在查詢時就限定勞務類，工程/財物不會進來。
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

## AI 推薦（以同仁過去挑選紀錄訓練）

除了關鍵字規則，另有一個以同仁過去人工挑選的 1,986 筆標案訓練的評分模型（`model.json`）。
**關鍵字規則或 AI 推薦度 ≥ 門檻（config.yml `model.threshold`，預設 0.9）任一成立即通知**，
快報與資料庫的「關鍵字群組」會標示「AI推薦0.94」這類推薦度。

- 交叉驗證：規則單獨抓到同仁過去挑選的 51%；規則＋模型約 80%，每日件數相近
- 重新訓練（例如累積更多挑選紀錄後）：`pip install scikit-learn numpy` → `python train_model.py` → commit `model.json`
- 匯入人工整理的歷史 Excel：`python tender_import.py 檔案1.xlsx 檔案2.xlsx ...`

## 雲端資料庫（Google Sheet）

每次執行後，新增或有變動的公告會自動寫入 Google Sheet「政府標案資料庫（自動累積）」，一直累積：

| 工作表 | 內容 |
|---|---|
| 符合案件 | 只收錄與本司業務相關的案件；右側橘色欄「狀態／負責人／是否投標／備註」由同仁填寫，**系統更新不會覆蓋**；規則調整後不再符合者自動移除（已填備註者保留） |
| 說明 | 最後同步時間、筆數 |

設定方式（一次性）：
1. 打開該 Sheet → 擴充功能 → Apps Script → 貼上 `gas/Code.gs`（把 `__GAS_TOKEN__` 換成自訂密碼）→ 儲存
2. 部署 → 新增部署作業 → 類型「網頁應用程式」→ 執行身分「我」、存取權「所有人」→ 部署並授權 → 複製網址
3. GitHub Secrets 新增 `GAS_URL`（上述網址）、`GAS_TOKEN`（同步驟 1 的密碼）
4. Actions → Run workflow → `sync_all = true`，把既有資料一次寫入

同步失敗的資料會存在 `data/sheet_pending.json`，下次執行自動重送。容量：Google Sheet 上限 1,000 萬格，以每年約 10 萬筆估算可用 5 年以上。

## 標案資料庫（repo 內檔案）

資料庫**只保存與本司業務相關的案件**（符合 `config.yml` 關鍵字規則者）：

| 檔案 | 內容 | 用法 |
|---|---|---|
| `data/標案資料庫.xlsx` | 每次重新產生，**每天隨 Email 附上** | 直接用 Excel 篩選、排序 |
| └ 工作表「標案資料庫」 | 歷來所有相關案件（不含更正公告） | 商機追蹤 |
| └ 工作表「月份統計」 | 每月掃描公告數、相關件數、預算合計 | 市場量體觀察 |
| `data/db/YYYY-MM.csv` | 標案資料庫（只含相關案件），依公告月份分檔 | 匯入其他系統／標案情報庫 |
| `data/raw/YYYY-MM.csv` | 原始收錄（全部勞務類公告），不對外顯示 | 調整關鍵字後用 `rescore` 重新篩選，避免漏案 |
| `data/matches.csv` | 每日實際通知過的案件 | 通知紀錄 |
| `data/seen.json` | 已通知紀錄，避免重複；保留 120 天 | 系統用 |

- 同一筆公告重複查到只會更新，不會重複；同案號第二次公告（流標再招）視為新的一筆。
- 調整 `config.yml` 關鍵字後，之後新收錄的資料會用新規則標記「符合」。
- **回補歷史資料**：Actions → Run workflow → `db_only = true`、`days = 14`（逐日查詢，遇驗證碼會自動停止並保存已取得部分）。

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
程式會重試 4 次；仍失敗則該次 Action 顯示紅燈並在 Summary 顯示錯誤原因，GitHub 會寄失敗通知給 repo 擁有者。因為每次查近 3 日，單日失敗隔天會自動補上。

**Q：會不會被採購網擋？**
「標案查詢」列表頁以 4 秒間隔翻頁，實測 10 頁未觸發驗證碼。明細頁短時間連查約 5 筆會出現「撲克牌驗證碼」，因此平常不抓明細；本系統遇驗證碼即停止，不繞過。
若查詢頁失敗，會改用「今日招標」頁（只有當天、只有前 `max_detail` 件有預算）。

**Q：為什麼不用 g0v API？**
g0v 標案 API 前面有 Cloudflare 機器人檢查，會擋 GitHub 伺服器的連線（實測 403），因此改抓官方頁面。

**Q：公開 repo 可以嗎？**
建議用 Private。Public repo 若 60 天無 commit 會自動停用排程（本系統每天都會 commit，實務上不會發生），但案件清單與關鍵字策略會公開。

**Q：資料使用規範**
資料著作權屬政府電子採購網，本系統供公司內部商機追蹤使用，對外引用請註明「資料來源：政府電子採購網」。
