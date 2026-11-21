"""Read-only diagnostics; excludes credentials, cookies and proxy addresses."""
import json
import os
import re
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

URLS = ['https://web.pcc.gov.tw/pis/',
        'https://web.pcc.gov.tw/prkms/tender/common/basic/indexTenderBasic',
        'https://web.pcc.gov.tw/prkms/today/common/todayTender']


def summary(text):
    text = re.sub(r'<script\b.*?</script>|<style\b.*?</style>', '', text, flags=re.S | re.I)
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', text)).strip()[:1000]


print('Proxy variable names present:', [k for k in os.environ if k.lower() in ('http_proxy','https_proxy','all_proxy','no_proxy')], flush=True)
session = requests.Session()
session.headers['User-Agent'] = 'Mozilla/5.0'
for url in URLS:
    try:
        r = session.get(url, timeout=30)
        r.encoding = 'utf-8'
        print(json.dumps({'client':'requests','url':url,'status':r.status_code,
                          'server':r.headers.get('Server'), 'content_type':r.headers.get('Content-Type'),
                          'body':summary(r.text)}, ensure_ascii=False), flush=True)
    except requests.RequestException as exc:
        print('requests', urlparse(url).path, type(exc).__name__, flush=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(locale='zh-TW')
    tab = context.new_page()
    for url in URLS:
        try:
            response = tab.goto(url, wait_until='domcontentloaded', timeout=40000)
            print(json.dumps({'client':'browser','url':url,'status':response.status if response else None,
                              'title':tab.title(),'body':summary(tab.content())}, ensure_ascii=False), flush=True)
        except Exception as exc:
            print('browser',urlparse(url).path,type(exc).__name__,flush=True)
    browser.close()
