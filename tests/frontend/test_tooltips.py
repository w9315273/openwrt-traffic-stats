"""提示框回归覆盖鼠标, 触屏及列表销毁时的清理."""
from harness import BrowserTestCase


class TooltipTests(BrowserTestCase):
    def test_unattributed_bubble_tracks_only_horizontal_movement(self):
        row = self.page.locator('.ts-unknown')
        tip = self.page.locator('#ts-unknown-tooltip')
        row.hover(position={'x': 300, 'y': 10})
        self.assertTrue(tip.is_visible())
        first = tip.bounding_box()
        row.hover(position={'x': 400, 'y': 25})
        self.assertNotEqual(tip.bounding_box()['x'], first['x'])
        self.assertEqual(tip.bounding_box()['y'], first['y'])
        self.assertNotEqual(tip.evaluate("el => getComputedStyle(el, '::after').content"), 'none')
        for x in (2, row.bounding_box()['width'] - 2):
            row.hover(position={'x': x, 'y': 10})
            self.assertTrue(tip.evaluate('''el => {
                const box = el.getBoundingClientRect(), card = document.querySelector('.ts-overview .ts-section').getBoundingClientRect();
                return box.left >= card.left && box.right <= card.right && box.top >= card.top && box.bottom <= card.bottom;
            }'''))
        row.click()
        self.page.mouse.move(0, 0)
        self.assertTrue(tip.is_hidden(), 'Desktop click must not pin the tooltip')

    def test_unattributed_touch_toggle_outside_scroll_and_legacy_click(self):
        self.open_page(touch=True, width=390, height=800)
        button = self.page.locator('.ts-unknown-label')
        tip = self.page.locator('#ts-unknown-tooltip')
        button.tap()
        self.assertTrue(tip.is_visible())
        self.assertGreaterEqual(button.bounding_box()['height'], 44)
        self.assertTrue(tip.evaluate('''el => {
            const box = el.getBoundingClientRect(), card = document.querySelector('.ts-overview .ts-section').getBoundingClientRect();
            return box.left >= card.left && box.right <= card.right;
        }'''))
        button.tap()
        self.assertTrue(tip.is_hidden())
        button.tap()
        self.page.get_by_role('heading', name='Devices', exact=True).tap()
        self.assertTrue(tip.is_hidden())
        button.tap()
        self.page.evaluate("window.dispatchEvent(new Event('scroll'))")
        self.assertTrue(tip.is_hidden())
        button.dispatch_event('pointerdown', {'pointerType': 'touch'})
        button.dispatch_event('click', {'detail': 1})
        self.assertTrue(tip.is_visible())
        button.dispatch_event('click', {'detail': 1})
        self.assertTrue(tip.is_hidden())

    def test_unattributed_tooltip_closes_when_row_disappears(self):
        self.open_page(touch=True, width=390, height=800)
        tip = self.page.locator('#ts-unknown-tooltip')
        for trigger in ('hover', 'tap'):
            for field in ('available', 'table_exists', 'table_owned'):
                with self.subTest(trigger=trigger, field=field):
                    self.page.mouse.move(0, 0)
                    self.update('Object.assign(statusData, {available:true, table_exists:true, table_owned:true})')
                    getattr(self.page.locator('.ts-unknown-label'), trigger)()
                    self.assertTrue(tip.is_visible())
                    self.page.evaluate('window.oldUnknownButton = document.querySelector(".ts-unknown-label")')
                    self.update('statusData.' + field + ' = false')
                    self.assertTrue(tip.is_hidden())
                    self.assertEqual(self.page.locator('.ts-unknown').count(), 0)
                    self.assertTrue(self.page.evaluate('!oldUnknownButton.isConnected && oldUnknownButton.getAttribute("aria-expanded") === "false"'))
        self.update('Object.assign(statusData, {available:true, table_exists:true, table_owned:true})')
        self.page.locator('.ts-unknown-label').tap()
        self.page.get_by_role('button', name='br-lan', exact=True).click()
        self.assertTrue(tip.is_hidden())
        self.assertEqual(self.page.locator('.ts-unknown').count(), 0)

    def test_collection_status_mouse_does_not_pin(self):
        icon = self.page.locator('.ts-inline-state')
        tip = self.page.locator('#ts-collection-status-tooltip')
        icon.hover()
        self.assertTrue(tip.is_visible())
        icon.click()
        self.assertEqual(icon.get_attribute('aria-pressed'), 'false')
        self.page.mouse.move(0, 0)
        self.assertTrue(tip.is_hidden())

    def test_collection_status_touch_pin_refresh_unpin_and_outside(self):
        self.open_page(touch=True, width=390, height=800)
        icon = self.page.locator('.ts-inline-state')
        tip = self.page.locator('#ts-collection-status-tooltip')
        icon.tap()
        self.assertTrue(tip.is_visible())
        self.assertEqual(icon.get_attribute('aria-pressed'), 'true')
        self.update()
        self.assertTrue(tip.is_visible())
        icon.tap()
        self.assertTrue(tip.is_hidden())
        icon.tap()
        self.page.get_by_role('heading', name='Devices', exact=True).tap()
        self.assertTrue(tip.is_hidden())
        self.assertEqual(icon.get_attribute('aria-pressed'), 'false')
        icon.tap()
        self.page.keyboard.press('Escape')
        self.assertTrue(tip.is_hidden())
