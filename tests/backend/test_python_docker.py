"""验证 Docker 网络发现, 确保不读取敏感字段或不可用网络模式."""
import errno
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from traffic_stats import proxy

ID1, ID2 = 'a' * 64, 'b' * 64


def record(name='/WARP', mode='bridge', running=True):
    return {'name': name, 'image': 'example/warp:latest', 'state': 'running' if running else 'exited',
            'running': running, 'mode': mode,
            'networks': {'bridge': {'IPAddress': '172.18.0.2', 'GlobalIPv6Address': '2001:db8:0::2'},
                         'extra': {'IPAddress': '172.18.0.2'}},
            'ports': {'1080/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '1080'}, {'HostIp': '::', 'HostPort': '1080'}],
                      '1080/udp': [{'HostIp': '192.0.2.1', 'HostPort': '1080'}], '8080/tcp': None}}


def output(*records):
    return ('\n'.join(json.dumps(value) for value in records) + '\n').encode()


class DockerTests(unittest.TestCase):
    def test_names_validate_and_preserve_order(self):
        self.assertEqual(proxy.names(['WARP', 'warp-2', 'WARP']), ['WARP', 'warp-2'])
        self.assertEqual(proxy.names('WARP'), ['WARP'])
        for value in ([''], ['/WARP'], ['--all'], ['space name'], ['bad;command'], ['x' * 256], None, [1]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                proxy.names(value)

    def test_projected_batch_includes_dual_stack_published_listeners_and_stopped(self):
        cancel = lambda: False
        with patch.object(proxy, 'run', side_effect=[(ID1 + '\n' + ID2).encode(),
                                                    output(record(), record('/stopped', running=False))]) as run:
            found = proxy.containers(cancel)
        self.assertEqual(len(run.call_args_list), 2)
        for call in run.call_args_list:
            self.assertEqual(call.args[0][:3], list(proxy.DOCKER))
            self.assertIs(call.kwargs['cancel'], cancel)
        inspect = run.call_args_list[1].args[0]
        self.assertEqual(inspect[-2:], [ID1, ID2])
        self.assertIn(proxy.CONTAINER_FORMAT, inspect)
        self.assertNotIn('Env', proxy.CONTAINER_FORMAT)
        self.assertEqual(found['excluded_names'], [])
        self.assertEqual(found['containers'][0]['addresses'], ['172.18.0.2', '2001:db8::2'])
        self.assertEqual(found['containers'][0]['listeners'], [
            {'address': '0.0.0.0', 'protocol': 'tcp', 'port': 1080},
            {'address': '192.0.2.1', 'protocol': 'udp', 'port': 1080},
            {'address': '::', 'protocol': 'tcp', 'port': 1080}])
        self.assertEqual(found['containers'][1]['addresses'], [])
        self.assertEqual(found['containers'][1]['listeners'], [])

    def test_excluded_network_modes_never_enter_selection(self):
        with patch.object(proxy, 'run', side_effect=[('\n'.join([str(i) * 64 for i in range(3)])).encode(),
             output(record('/host', 'host'), record('/isolated', 'none'), record('/shared', 'container:abc'))]):
            self.assertEqual(proxy.containers(), {'containers': [], 'excluded_names': ['host', 'isolated', 'shared']})

    def test_empty_success_differs_from_missing_or_unreachable_docker(self):
        with patch.object(proxy, 'run', return_value=b'') as run:
            self.assertEqual(proxy.containers(), {'containers': [], 'excluded_names': []})
            self.assertEqual(run.call_count, 1)
        for error in (FileNotFoundError(errno.ENOENT, 'missing'), OSError(errno.EIO, 'unreachable'),
                      TimeoutError(errno.ETIMEDOUT, 'timed out')):
            with self.subTest(error=error), patch.object(proxy, 'run', side_effect=error), self.assertRaises(OSError):
                proxy.containers()

    def test_disappearing_container_retries_changed_list_only(self):
        with patch.object(proxy, 'run', side_effect=[(ID1 + '\n' + ID2).encode(),
             OSError(errno.EIO, 'container disappeared'), ID1.encode(), output(record())]):
            self.assertEqual(len(proxy.containers()['containers']), 1)
        with patch.object(proxy, 'run', side_effect=[ID1.encode(), OSError(errno.EIO, 'bad inspect'), ID1.encode()]), self.assertRaises(OSError):
            proxy.containers()

    def test_invalid_ids_and_malformed_metadata_fail_explicitly(self):
        with patch.object(proxy, 'run', return_value=b'--injection'), self.assertRaises(ValueError):
            proxy.containers()
        for value in ([], {}, {'name': '/invalid'}, dict(record(), running='true'),
                      dict(record(), networks=['invalid']), dict(record(), ports={'abc': []})):
            with self.subTest(value=value), patch.object(proxy, 'run', side_effect=[ID1.encode(), output(value)]), self.assertRaises(ValueError):
                proxy.containers()


if __name__ == '__main__':
    unittest.main()
