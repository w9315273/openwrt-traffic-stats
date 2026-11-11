"""验证翻译覆盖及后端英文错误协议, 不修改运行语言."""
from pathlib import Path
import ast
import json
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from traffic_stats import rpc


def entries(path):
    text = path.read_text()
    return {json.loads(a): json.loads(b) for a,b in re.findall(r'(?m)^msgid (".+")\nmsgstr (".*")$', text)}


class I18nTests(unittest.TestCase):
    def test_all_frontend_strings_have_chinese_translation(self):
        catalog = entries(ROOT / 'luci-app-traffic-stats/po/zh_Hans/traffic-stats.po')
        self.assertTrue(catalog)
        referenced = set()
        pattern = re.compile(r'''_\(\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')\s*\)''')
        for source in (ROOT / 'luci-app-traffic-stats/htdocs').rglob('*.js'):
            for match in pattern.finditer(source.read_text()):
                message = ast.literal_eval(match[1])
                referenced.add(message)
                self.assertIn(message, catalog, (source, message))
                self.assertTrue(catalog[message])
                self.assertEqual(re.findall(r'%[sd]', message), re.findall(r'%[sd]', catalog[message]))
        menu = json.loads((ROOT / 'luci-app-traffic-stats/root/usr/share/luci/menu.d/luci-app-traffic-stats.json').read_text())
        for item in menu.values():
            self.assertIn(item['title'], catalog)
            referenced.add(item['title'])
        self.assertEqual(set(catalog), referenced)

    def test_backend_error_messages_are_english_and_codes_remain_stable(self):
        for code,message in [('invalid_request','Invalid request.'), ('history_unavailable','History unavailable.')]:
            self.assertEqual(rpc.failure(code,message), {'ok':False,'error':code,'message':message})
        for source in (ROOT / 'src/traffic_stats').glob('*.py'):
            for node in ast.walk(ast.parse(source.read_text())):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'failure':
                    self.assertTrue(all(isinstance(arg,ast.Constant) and isinstance(arg.value,str) for arg in node.args))
                    self.assertTrue(all(not re.search(r'[\u4e00-\u9fff]',arg.value) for arg in node.args))

if __name__ == '__main__':
    unittest.main()
