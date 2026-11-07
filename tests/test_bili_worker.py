"""B站搜索边界回归：模拟 SDK，不请求网络。"""
import asyncio
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bili_worker


class BiliWorkerTest(unittest.TestCase):
    def collect(self, response):
        sdk=SimpleNamespace(
            search=SimpleNamespace(search_by_type=AsyncMock(return_value=response),
                                   SearchObjectType=SimpleNamespace(VIDEO='video'),
                                   OrderVideo=SimpleNamespace(CLICK='click')),
            video=Mock(), user=Mock(), request_settings=Mock())
        with patch.dict(sys.modules, bilibili_api=sdk):
            result=asyncio.run(bili_worker.collect('发光车标',10,7))
        sdk.video.Video.assert_not_called()
        sdk.user.User.assert_not_called()
        return result

    def test_zero_results_without_result_field_is_success(self):
        # 真实复现的关键字段：numResults=0、numPages=0，完全没有 result。
        self.assertEqual(self.collect({'page':1,'pagesize':10,'numResults':0,'numPages':0}),
                         {'ok':True,'items':[]})

    def test_empty_list_and_explicit_null_are_success(self):
        for response in ({'result':[]},{'result':None,'numResults':0,'numPages':0}):
            with self.subTest(response=response):
                self.assertEqual(self.collect(response),{'ok':True,'items':[]})

    def test_malformed_responses_are_not_empty_results(self):
        for response in (None,[],{}, {'numResults':1,'numPages':1},
                         {'numResults':False,'numPages':False},
                         {'numResults':'0','numPages':'0'},
                         {'numResults':0,'numPages':1},
                         {'numResults':0,'numPages':0,'result':{}},
                         {'result':'读取失败'}):
            with self.subTest(response=response),self.assertRaises(ValueError):
                self.collect(response)


if __name__=='__main__':unittest.main()
