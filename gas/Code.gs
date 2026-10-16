/**
 * 政府標案資料庫 — Google Apps Script 接收端
 * 綁定在 Google Sheet「政府標案資料庫（自動累積）」上（擴充功能 → Apps Script）
 *
 * GitHub Actions 每天把「新增或有變動」的公告 POST 過來，這裡負責 upsert：
 *   工作表「全部公告」：全部收錄的勞務類公告
 *   工作表「符合案件」：命中關鍵字者（不含更正公告），右側「狀態／負責人／是否投標／備註」
 *                       由同仁填寫，系統更新資料時不會覆蓋
 * 新資料插在最上方（最新在上）。主鍵欄（A 欄）隱藏，請勿刪除。
 */

const TOKEN = '__GAS_TOKEN__';   // 必須與 GitHub Secret「GAS_TOKEN」相同

const ALL_COLS = ['主鍵', '公告日', '機關', '案名', '預算', '截止投標', '公告類型', '傳輸次數',
  '採購性質', '案號', '符合', '關鍵字群組', '命中關鍵字', '分數', '連結', '首次收錄', '最後更新', '資料來源'];
const HIT_COLS = ['主鍵', '公告日', '機關', '案名', '預算', '截止投標', '公告類型', '案號',
  '關鍵字群組', '命中關鍵字', '分數', '連結', '首次收錄'];
const HIT_EXTRA = ['狀態', '負責人', '是否投標', '備註'];
const TEXT_COLS = ['主鍵', '案號', '傳輸次數'];

function doPost(e) {
  let body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return json_({ ok: false, error: 'bad json' });
  }
  if (body.token !== TOKEN) return json_({ ok: false, error: 'unauthorized' });

  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    const rows = body.rows || [];
    const all = upsert_('全部公告', ALL_COLS, [], rows);
    const isHit = r => r['符合'] === 'Y' && String(r['公告類型']).indexOf('更正') < 0;
    const hits = upsert_('符合案件', HIT_COLS, HIT_EXTRA, rows.filter(isHit));
    hits.removed = removeRows_('符合案件', HIT_COLS.length, HIT_EXTRA.length,
      rows.filter(r => !isHit(r)).map(r => String(r['主鍵'])));
    writeInfo_();
    return json_({ ok: true, all: all, hits: hits });
  } catch (err) {
    return json_({ ok: false, error: String(err) });
  } finally {
    lock.releaseLock();
  }
}

/** 健康檢查：瀏覽器開 Web App 網址可看到目前筆數 */
function doGet() {
  const ss = SpreadsheetApp.getActive();
  const n = name => { const s = ss.getSheetByName(name); return s ? Math.max(0, s.getLastRow() - 1) : 0; };
  return json_({ ok: true, 全部公告: n('全部公告'), 符合案件: n('符合案件') });
}

function upsert_(name, cols, extra, rows) {
  const ss = SpreadsheetApp.getActive();
  let sh = ss.getSheetByName(name);
  if (!sh) sh = ss.insertSheet(name);
  if (sh.getLastRow() === 0) setupSheet_(sh, cols, extra);
  if (!rows.length) return { added: 0, updated: 0 };

  const width = cols.length;
  const last = sh.getLastRow();
  const keys = last > 1 ? sh.getRange(2, 1, last - 1, 1).getValues().map(r => String(r[0])) : [];
  const index = {};
  keys.forEach((k, i) => { index[k] = i + 2; });

  const toAdd = [];
  let updated = 0;
  const seen = {};
  rows.forEach(r => {
    const k = String(r['主鍵']);
    if (seen[k]) return;
    seen[k] = true;
    const vals = cols.map(c => cell_(c, r[c]));
    if (index[k]) {
      sh.getRange(index[k], 1, 1, width).setValues([vals]);   // 只寫資料欄，右側備註欄不動
      updated++;
    } else {
      toAdd.push(vals);
    }
  });

  if (toAdd.length) {
    toAdd.sort((a, b) => String(b[1]).localeCompare(String(a[1])));  // 公告日新→舊
    sh.insertRowsBefore(2, toAdd.length);   // 沿用資料列格式（不沿用標題列）
    const rng = sh.getRange(2, 1, toAdd.length, width);
    TEXT_COLS.forEach(c => {
      const ci = cols.indexOf(c);
      if (ci >= 0) sh.getRange(2, ci + 1, toAdd.length, 1).setNumberFormat('@');
    });
    rng.setValues(toAdd);
    if (extra.length) sh.getRange(2, width + 1, toAdd.length, extra.length).clearContent();
  }
  return { added: toAdd.length, updated: updated };
}

/** 關鍵字規則調整後不再符合的列：若同仁未填任何備註欄，就從「符合案件」移除 */
function removeRows_(name, width, extraLen, keys) {
  const sh = SpreadsheetApp.getActive().getSheetByName(name);
  if (!sh || !keys.length || sh.getLastRow() < 2) return 0;
  const drop = {};
  keys.forEach(k => { drop[k] = true; });
  const n = sh.getLastRow() - 1;
  const data = sh.getRange(2, 1, n, width + extraLen).getValues();
  const targets = [];
  data.forEach((row, i) => {
    const annotated = row.slice(width).some(v => String(v).trim() !== '');
    if (drop[String(row[0])] && !annotated) targets.push(i + 2);
  });
  for (let i = targets.length - 1; i >= 0; i--) sh.deleteRow(targets[i]);  // 由下往上刪
  return targets.length;
}

function cell_(col, v) {
  if (v === undefined || v === null) return '';
  if (col === '連結') return v ? '=HYPERLINK("' + String(v).replace(/"/g, '') + '","開啟")' : '';
  if (col === '預算' || col === '分數') {
    const n = Number(String(v).replace(/,/g, ''));
    return v === '' || isNaN(n) ? '' : n;
  }
  return String(v);
}

function setupSheet_(sh, cols, extra) {
  const head = cols.concat(extra);
  sh.getRange(1, 1, 1, head.length).setValues([head])
    .setFontWeight('bold').setFontColor('#ffffff').setBackground('#1d4ed8');
  if (extra.length) sh.getRange(1, cols.length + 1, 1, extra.length).setBackground('#b45309');
  sh.setFrozenRows(1);
  sh.hideColumns(1);
  const widths = { 公告日: 95, 機關: 200, 案名: 420, 預算: 110, 截止投標: 130, 公告類型: 200,
    案號: 130, 關鍵字群組: 140, 命中關鍵字: 170, 連結: 60, 首次收錄: 130, 狀態: 90, 負責人: 80, 是否投標: 80, 備註: 260 };
  head.forEach((c, i) => { if (widths[c]) sh.setColumnWidth(i + 1, widths[c]); });
  const bi = cols.indexOf('預算');
  if (bi >= 0) sh.getRange(2, bi + 1, sh.getMaxRows() - 1, 1).setNumberFormat('#,##0');
  if (extra.indexOf('狀態') >= 0) {
    const rule = SpreadsheetApp.newDataValidation()
      .requireValueInList(['待評估', '追蹤中', '準備投標', '已投標', '得標', '未得標', '不投'], true).build();
    sh.getRange(2, cols.length + 1, sh.getMaxRows() - 1, 1).setDataValidation(rule);
    const bid = SpreadsheetApp.newDataValidation().requireValueInList(['是', '否', '評估中'], true).build();
    sh.getRange(2, cols.length + 3, sh.getMaxRows() - 1, 1).setDataValidation(bid);
  }
  sh.getRange(1, 1, sh.getMaxRows(), head.length).createFilter();
}

function writeInfo_() {
  const ss = SpreadsheetApp.getActive();
  let sh = ss.getSheetByName('說明');
  if (!sh) {
    sh = ss.insertSheet('說明');
    const def = ss.getSheetByName('工作表1') || ss.getSheetByName('Sheet1');
    if (def && def.getLastRow() === 0) ss.deleteSheet(def);
  }
  const n = name => { const s = ss.getSheetByName(name); return s ? Math.max(0, s.getLastRow() - 1) : 0; };
  const tz = Session.getScriptTimeZone() || 'Asia/Taipei';
  sh.clear();
  sh.getRange(1, 1, 8, 1).setValues([
    ['政府標案資料庫（由 GitHub Actions 每天自動更新）'],
    ['最後同步：' + Utilities.formatDate(new Date(), 'Asia/Taipei', 'yyyy-MM-dd HH:mm')],
    ['全部公告：' + n('全部公告') + ' 筆　符合案件：' + n('符合案件') + ' 筆'],
    ['資料來源：政府電子採購網「標案查詢」（採購性質與關鍵字設定見 GitHub repo 的 config.yml）'],
    ['「符合案件」右側橘色欄位（狀態／負責人／是否投標／備註）由同仁填寫，系統不會覆蓋'],
    ['請勿刪除或移動欄位、勿刪除隱藏的 A 欄（主鍵），可自由使用篩選、排序、建立篩選檢視'],
    ['預算空白：機關未公開預算，或該筆來自備援資料源'],
    ['新資料插在最上方'],
  ]);
  sh.getRange(1, 1).setFontWeight('bold').setFontSize(14);
  sh.setColumnWidth(1, 720);
}

function json_(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}
