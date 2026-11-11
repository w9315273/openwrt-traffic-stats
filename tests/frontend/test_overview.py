"""概览回归覆盖筛选, 刷新, 行高, 气泡和触屏行为."""
from harness import BrowserTestCase, VERSION


class OverviewTests(BrowserTestCase):
    def test_filtering_keeps_totals_and_places_empty_message_below_table(self):
        page = self.page
        self.assertEqual(page.locator('.ts-lan-filters button').all_text_contents(), ['All', 'br-lan', 'br-lan2', 'iot'])
        self.assertEqual(page.locator('.ts-refresh-option[aria-pressed="true"]').all_text_contents(), ['All', '3s'])
        totals = page.locator('.ts-metrics').inner_text()
        page.get_by_role('button', name='iot', exact=True).click()
        self.assertEqual(page.locator('.ts-device-name-text').all_text_contents(), ['Sensor'])
        self.assertEqual(page.locator('.ts-unknown').count(), 0)
        self.update()
        self.assertEqual(page.locator('.ts-device-name-text').all_text_contents(), ['Sensor'])
        self.assertEqual(page.locator('.ts-metrics').inner_text(), totals)
        page.get_by_role('button', name='All', exact=True).click()
        self.assertEqual(page.locator('.ts-device-name-text').count(), 5)
        self.assertEqual(page.locator('.ts-unknown').count(), 1)
        self.update("statusData.devices = statusData.devices.filter(d => !d.interfaces.includes('br-lan2'))")
        page.get_by_role('button', name='br-lan2', exact=True).click()
        self.assertTrue(page.locator('.ts-table-scroll').evaluate('''el =>
            el.firstElementChild.tagName === 'TABLE' && el.lastElementChild.textContent.includes('No devices on this network')'''))
        self.update("statusData.configuration.lan_devices = ['br-lan']")
        self.assertTrue(page.locator('.ts-lan-filters').is_hidden())
        self.assertEqual(page.locator('.ts-unknown').count(), 1)

    def test_preferences_survive_reload_and_invalid_interfaces_reset(self):
        page = self.page
        page.get_by_role('button', name='iot', exact=True).click()
        page.get_by_role('button', name='10s', exact=True).click()
        self.reload_page()
        self.assertEqual(page.locator('.ts-lan-filters [aria-pressed="true"]').inner_text(), 'iot')
        self.assertEqual(page.locator('.ts-refresh > div [aria-pressed="true"]').inner_text(), '10s')
        self.assertEqual(page.locator('.ts-device-name-text').all_text_contents(), ['Sensor'])
        page.get_by_role('button', name='All', exact=True).click()
        self.reload_page()
        self.assertEqual(page.locator('.ts-lan-filters [aria-pressed="true"]').inner_text(), 'All')
        page.evaluate("localStorage.setItem('traffic-stats.lan-filter', 'removed-interface')")
        self.reload_page()
        self.assertEqual(page.locator('.ts-lan-filters [aria-pressed="true"]').inner_text(), 'All')
        self.assertIsNone(page.evaluate("localStorage.getItem('traffic-stats.lan-filter')"))
        page.get_by_role('button', name='br-lan', exact=True).click()
        self.update("statusData.configuration.lan_devices = ['br-lan']")
        self.assertTrue(page.locator('.ts-lan-filters').is_hidden())
        self.assertIsNone(page.evaluate("localStorage.getItem('traffic-stats.lan-filter')"))

    def test_row_heights_and_mobile_overflow(self):
        for width in (390, 900, 1200):
            with self.subTest(width=width):
                self.page.set_viewport_size({'width': width, 'height': 1000})
                heights = self.page.locator('.ts-table tbody tr').evaluate_all('rows => rows.map(row => row.getBoundingClientRect().height)')
                self.assertEqual(len(set(heights)), 1, heights)
                if width <= 1000:
                    self.assertGreaterEqual(heights[0], 56)
                self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.page.set_viewport_size({'width': 390, 'height': 800})
        self.update("statusData.configuration.lan_devices = ['br-lan', 'br-lan2', 'iot', 'guest', 'guest2', 'guest3']")
        self.assertTrue(self.page.locator('.ts-lan-filters').evaluate('el => el.scrollWidth > el.clientWidth'))

    def test_refresh_preserves_scroll_selection_focus_and_uses_current_device(self):
        self.page.set_viewport_size({'width': 1440, 'height': 1000})
        self.update("""
            statusData.devices[0].hostname = 'Long device name '.repeat(20);
            statusData.devices[0].addresses = ['192.168.100.100 / '.repeat(12), '2001:db8:abcd:1234:5678:abcd:1234:5678 / '.repeat(6)];
            statusData.rates = { d_000000000001_up: { bytes_per_second: 123 } }
        """)
        self.page.evaluate("""() => {
            window.savedRow = document.querySelector('[data-device="000000000001"]');
            window.savedName = savedRow.querySelector('.ts-device-name-text');
            window.savedIp = savedRow.querySelector('.ts-cell-scroll');
            savedIp.focus(); savedName.scrollLeft = 60; savedIp.scrollLeft = 80;
            const range = document.createRange();
            range.setStart(savedIp.firstChild, 4); range.setEnd(savedIp.firstChild, 12);
            getSelection().removeAllRanges(); getSelection().addRange(range);
            window.selectedText = getSelection().toString();
            window.nameText = savedName.firstChild; window.ipText = savedIp.firstChild;
        }""")
        self.update('statusData.rates.d_000000000001_up.bytes_per_second = 456')
        state = self.page.evaluate("""() => ({
            row: document.querySelector('[data-device="000000000001"]') === savedRow,
            text: savedName.firstChild === nameText && savedIp.firstChild === ipText,
            nameScroll: savedName.scrollLeft, ipScroll: savedIp.scrollLeft,
            selection: getSelection().toString() === selectedText, focus: document.activeElement === savedIp,
            rate: savedRow.cells[4].textContent
        })""")
        self.assertEqual(state, {'row': True, 'text': True, 'nameScroll': 60, 'ipScroll': 80,
                                'selection': True, 'focus': True, 'rate': '456 B/s'})
        self.update("statusData.monthly.counters.d_000000000003_down = '999999'")
        self.assertEqual(self.page.locator('tbody tr[data-device]').first.get_attribute('data-device'), '000000000003')
        self.assertTrue(self.page.evaluate('savedIp.scrollLeft === 80 && savedName.scrollLeft === 60 && savedRow.isConnected'))
        self.assertTrue(self.page.evaluate('document.activeElement === savedIp && getSelection().toString() === selectedText'))
        self.update("""
            statusData.devices[0].hostname = 'Updated name'; statusData.devices[0].addresses = ['192.0.2.1'];
            stats.editDeviceName = device => { window.editedDevice = device; }
        """)
        self.page.locator('[data-device="000000000001"] button').click()
        self.assertTrue(self.page.evaluate("editedDevice.hostname === 'Updated name' && savedName.textContent === 'Updated name' && savedIp.textContent === '192.0.2.1'"))

    def test_rows_added_removed_and_service_recovery(self):
        self.update("statusData.devices.push({id:'000000000006',hostname:'New',interfaces:['br-lan']})")
        self.assertEqual(self.page.locator('[data-device="000000000006"]').count(), 1)
        self.update("statusData.devices = statusData.devices.filter(d => d.id !== '000000000001')")
        self.assertEqual(self.page.locator('[data-device="000000000001"]').count(), 0)
        self.update('statusData.available = false')
        self.assertTrue(self.page.get_by_text('Waiting for the first sample').is_visible())
        self.update('statusData.available = true')
        self.assertEqual(self.page.locator('[data-device="000000000006"]').count(), 1)
        for order in ('ascending', 'descending'):
            self.page.locator('[data-sort="total"]').click()
            self.assertEqual(self.page.locator('[data-sort="total"]').evaluate('el => el.parentNode.getAttribute("aria-sort")'), order)

    def test_footer_uses_package_version_and_project_link(self):
        footer = self.page.locator('.ts-footer a')
        self.assertEqual(footer.inner_text(), 'Traffic Stats v' + VERSION)
        self.assertEqual(footer.get_attribute('href'), 'https://github.com/w9315273/openwrt-traffic-stats')
        self.assertEqual(self.page.locator('.ts-footer').evaluate('el => getComputedStyle(el).position'), 'absolute')
        self.assertEqual(self.page.locator('.ts-footer-github').count(), 1)
