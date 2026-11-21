import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import tender_alert as app


def row(pk="one", title="論壇服務", script=False):
    name = f'<script>pageCode2Img("{title}")</script>' if script else f'<a href="/prkms/urlSelector/common/tpam?pk={pk}"><span>{title}</span></a>'
    cells = ["1", "測試機關", f"CASE-01<br>{name}", "01", "公開招標", "勞務類", "115/11/20", "115/12/01", "1,000,000"]
    return '<tr class="tb_b2">' + ''.join(f'<td>{c}</td>' for c in cells) + f'<td><a href="/prkms/urlSelector/common/tpam?pk={pk}">檢視</a></td></tr>'


def page(total=1, rows=None, next_url=None):
    return (f'共有<span class="red"> {total} </span>筆資料<table id="tpam"><tbody>'
            + (row() if rows is None else rows) + '</tbody></table>'
            + (f'<a href="{next_url}">下一頁</a>' if next_url else ''))


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.d = datetime(2026, 11, 21, tzinfo=app.TZ)
        self.clock = patch.object(app, "datetime", wraps=datetime).start()
        self.clock.now.return_value = self.d
        self.sleep = patch.object(app.time, "sleep").start()
        patch.object(app, '_last_request_at', None).start()
        self.addCleanup(patch.stopall)

    def search(self, html, **kwargs):
        with patch.object(app, "_get_html", return_value=html):
            return app.fetch_search(self.d, self.d, **kwargs)

    def test_current_title_excludes_case_number(self):
        item = self.search(page())[0]
        self.assertEqual(item['title'], '論壇服務')
        self.assertEqual(item['job_number'], 'CASE-01')
        self.assertEqual(item['budget_text'], '1,000,000')
        self.assertEqual(item['pk'], 'one')

    def test_legacy_title_is_supported(self):
        self.assertEqual(self.search(page(rows=row(script=True)))[0]['title'], '論壇服務')

    def test_verified_zero_is_valid(self):
        self.assertEqual(self.search(page(0, rows='')), [])

    def test_error_page_is_not_zero(self):
        with self.assertRaisesRegex(RuntimeError, '總筆數'):
            self.search('<html>Server Error</html>')

    def test_missing_table_is_failure(self):
        with self.assertRaisesRegex(RuntimeError, '表格'):
            self.search('共有<span class="red">1</span>筆資料')

    def test_captcha_stops_search(self):
        with self.assertRaises(app.CaptchaBlocked):
            self.search('驗證碼檢核')

    def test_follows_actual_next_link_even_on_short_page(self):
        next_url = '?pageSize=50&amp;d-49738-p=2'
        with patch.object(app, '_get_html', side_effect=[page(2, next_url=next_url), page(2, rows=row('two'))]) as get:
            items = app.fetch_search(self.d, self.d)
        self.assertEqual(len(items), 2)
        self.assertIn('pageSize=50&d-49738-p=2', get.call_args.args[0])
        self.assertNotIn('firstSearch=false', get.call_args.args[0])

    def test_missing_next_link_is_failure(self):
        with self.assertRaisesRegex(RuntimeError, '不完整'):
            self.search(page(2))

    def test_repeated_rows_do_not_count_as_complete(self):
        with patch.object(app, '_get_html', side_effect=[page(2, next_url='?d-49738-p=2'), page(2)]):
            with self.assertRaisesRegex(RuntimeError, '重複分頁'):
                app.fetch_search(self.d, self.d)

    def test_page_limit_is_failure(self):
        with self.assertRaisesRegex(RuntimeError, '未完整'):
            self.search(page(2, next_url='?d-49738-p=2'), max_pages=1)

    def test_external_paging_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, '官方網域'):
            self.search(page(2, next_url='https://example.com/'))

    def test_generic_http_500_is_retried_three_times(self):
        with patch.object(app, 'FIXTURE_DIR', None), \
             patch.object(app, '_session_ready', True), patch.object(app._session, 'get', side_effect=requests.HTTPError('500')), \
             patch.object(app, '_request', side_effect=requests.HTTPError('500')) as request:
            with self.assertRaisesRegex(RuntimeError, '連線失敗'):
                app._get_html(app.TODAY_URL, 'today.html')
        self.assertEqual(request.call_count, 3)

    def test_captcha_does_not_retry_or_switch(self):
        response = Mock(text='驗證碼檢核')
        with patch.object(app, 'FIXTURE_DIR', None), \
             patch.object(app, '_session_ready', True), patch.object(app._session, 'get', return_value=response) as get:
            with self.assertRaises(app.CaptchaBlocked):
                app._get_html(app.TODAY_URL, 'today.html')
        self.assertEqual(get.call_count, 1)
        self.sleep.assert_not_called()

    def test_explicit_block_is_not_retried(self):
        response = Mock(text='<title>The URL you requested has been blocked</title>')
        with patch.object(app, 'FIXTURE_DIR', None), patch.object(app, '_session_ready', True), \
             patch.object(app._session, 'get', return_value=response) as get:
            with self.assertRaises(app.SourceBlocked):
                app._get_html(app.TODAY_URL, 'today.html')
        get.assert_called_once()
        self.sleep.assert_not_called()

    def test_requests_initialize_session(self):
        response = Mock(text='ok')
        with patch.object(app, 'FIXTURE_DIR', None), \
             patch.object(app, '_session_ready', False), patch.object(app._session, 'get', return_value=response) as get:
            self.assertEqual(app._get_html(app.TODAY_URL, 'today.html'), 'ok')
        self.assertEqual(get.call_args_list[0].args[0], app.PCC + '/pis/')
        self.assertEqual(get.call_args_list[1].args[0], app.SEARCH_INDEX)
        self.assertEqual(get.call_args_list[2].kwargs['headers']['Referer'], app.SEARCH_INDEX)

    def test_request_rate_is_limited(self):
        with patch.object(app, '_last_request_at', 100), patch.object(app.time, 'monotonic', return_value=102), \
             patch.object(app._session, 'get', return_value=Mock(text='ok')):
            app._request(app.TODAY_URL, app.SEARCH_INDEX)
        self.sleep.assert_called_once_with(5)

    def test_today_zero_is_valid(self):
        html = '<strong>□公開招標公告□ 總筆數:0</strong><tbody><tr><td>無符合條件資料</td></tr></tbody>'
        with patch.object(app, '_get_html', return_value=html):
            self.assertEqual(app.fetch_today(), [])

    def test_today_unknown_page_is_failure(self):
        with patch.object(app, '_get_html', return_value='error'):
            with self.assertRaises(RuntimeError):
                app.fetch_today()

    def test_incomplete_today_fallback_does_not_write_or_notify(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'LAST_OK', Path(tmp)/'last_success.txt'), \
             patch.object(sys, 'argv', ['tender_alert.py', '--dry-run']), \
             patch.object(app, 'fetch_search', side_effect=RuntimeError('primary 500')), \
             patch.object(app, 'fetch_today', return_value=[]), patch.object(app, 'send_email') as email, \
             patch.object(app, 'save_db') as save:
            with self.assertRaisesRegex(RuntimeError, '保留補查'):
                app.main()
            self.assertFalse(app.LAST_OK.exists())
        email.assert_not_called()
        save.assert_not_called()

    def test_main_stops_all_sources_on_captcha(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'LAST_OK', Path(tmp)/'last_success.txt'), \
             patch.object(sys, 'argv', ['tender_alert.py', '--dry-run']), \
             patch.object(app, 'fetch_search', side_effect=app.CaptchaBlocked('驗證碼')), \
             patch.object(app, 'fetch_today') as fallback:
            with self.assertRaises(app.CaptchaBlocked):
                app.main()
        fallback.assert_not_called()

    def test_main_stops_all_sources_on_explicit_block(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'LAST_OK', Path(tmp)/'last_success.txt'), \
             patch.object(sys, 'argv', ['tender_alert.py', '--dry-run']), \
             patch.object(app, 'fetch_search', side_effect=app.SourceBlocked('blocked')), \
             patch.object(app, 'fetch_today') as fallback:
            with self.assertRaises(app.SourceBlocked):
                app.main()
        fallback.assert_not_called()

    def test_failure_covers_last_success_date(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'LAST_OK', Path(tmp)/'last_success.txt'), \
             patch.object(sys, 'argv', ['tender_alert.py', '--dry-run']), \
             patch.object(app, 'fetch_search', side_effect=RuntimeError('primary')) as search, \
             patch.object(app, 'fetch_today', side_effect=RuntimeError('fallback')):
            app.LAST_OK.write_text('2026-11-18', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, '主查詢失敗.*今日備援失敗'):
                app.main()
            self.assertLessEqual(search.call_args.args[0].date(), datetime(2026, 11, 18).date())
            self.assertEqual(app.LAST_OK.read_text(), '2026-11-18')


if __name__ == '__main__':
    unittest.main()
