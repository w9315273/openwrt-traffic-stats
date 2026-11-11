"""验证容器发现接口仅提供必要信息, 且不依赖采集配置."""
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from traffic_stats import rpc


class ContainerRpcTests(unittest.TestCase):
    def test_filtered_fields_and_no_configuration_dependency(self):
        data = {'containers':[{'name':'proxy', 'image':'image:tag', 'state':'running',
                              'addresses':['172.18.0.2'], 'listeners':[], 'private':'not exposed'}],
                'excluded_names':['host-app']}
        with patch.object(rpc.proxy,'containers',return_value=data), patch.object(rpc.config,'load_configuration',side_effect=AssertionError('must not read config')):
            reply=rpc.dispatch('containers',{},SimpleNamespace())
        self.assertTrue(reply['ok'])
        self.assertEqual(set(reply['data']['containers'][0]),{'name','image','state','addresses'})
        self.assertEqual(reply['data']['excluded_names'],['host-app'])

    def test_no_arbitrary_docker_requests_and_explicit_unavailability(self):
        with patch.object(rpc.proxy,'containers') as fetch:
            self.assertEqual(rpc.dispatch('containers',{'command':'inspect'},SimpleNamespace())['error'],'invalid_request')
            fetch.assert_not_called()
        for failure in (OSError('daemon down'),ValueError('invalid output')):
            with patch.object(rpc.proxy,'containers',side_effect=failure):
                self.assertEqual(rpc.dispatch('containers',{},SimpleNamespace())['error'],'docker_unavailable')
