"""使用实际前端源码, 仅模拟 LuCI 与路由器 RPC 边界."""
import os
from pathlib import Path
import re
import unittest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
RESOURCES = ROOT / 'luci-app-traffic-stats/htdocs/luci-static/resources'
VERSION = re.search(r'^PKG_VERSION:=(.+)$', (ROOT / 'luci-app-traffic-stats/Makefile').read_text(), re.M)[1]


class BrowserTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        options = {}
        # 本地可复用已有浏览器; CI 默认使用锁定 Playwright 版本提供的浏览器.
        executable = os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')
        if executable:
            options['executable_path'] = executable
        try:
            cls.browser = cls.playwright.chromium.launch(**options)
        except Exception:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = None
        self.errors = []
        self.open_page()

    def tearDown(self):
        if self.context:
            self.context.close()
        self.assertEqual(self.errors, [], 'Unexpected browser errors')

    def open_page(self, touch=False, width=1200, height=1000):
        if self.context:
            self.context.close()
        self.context = self.browser.new_context(
            viewport={'width': width, 'height': height}, has_touch=touch,
            locale='en-US', timezone_id='UTC', reduced_motion='reduce')
        self.page = self.context.new_page()
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.set_default_timeout(5000)

        def serve(route):
            if route.request.url.endswith('/traffic-stats.css'):
                route.fulfill(body=(RESOURCES / 'traffic-stats.css').read_text(), content_type='text/css')
            elif route.request.url == 'http://traffic-stats.test/':
                route.fulfill(body='<html lang="en"><head></head><body><main></main></body></html>', content_type='text/html')
            else:
                route.abort()

        # 固定本地来源使 localStorage 可用, 所有请求由内存响应, 不访问网络.
        self.context.route('**/*', serve)
        self.reload_page()

    def reload_page(self):
        self.page.goto('http://traffic-stats.test/')
        self.page.evaluate((Path(__file__).with_name('fixture.js')).read_text())
        shared = (RESOURCES / 'traffic-stats.js').read_text().replace('@TRAFFIC_STATS_VERSION@', VERSION)
        self.page.evaluate('(source) => { window.stats = new Function(source)(); }', shared)
        self.page.evaluate('''source => {
            window.app = new Function(source)();
            document.querySelector('main').append(app.render(statusData));
        }''', (RESOURCES / 'view/traffic-stats/overview.js').read_text())
        self.page.wait_for_function('document.querySelector("#traffic-stats-style").sheet !== null')

    def update(self, script=''):
        self.page.evaluate('async () => {' + script + '; await refresh(); }')
