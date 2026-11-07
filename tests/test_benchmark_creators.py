"""对标设置和赛道预设回归；临时数据库与模拟模型，不访问平台。"""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import radar
import test_radar as original


# 预设示例只用虚构名称；真实博主的研究放在本机 docs-research/，不进仓库。
PRESET_DOC={'field':'示例领域：家用车选购与用车知识','fixed_keywords':['新车试驾','家庭用车','用车知识','续航实测','辅助驾驶','新能源选车'],
    'youtube_fixed_keywords':['family SUV review','electric car buying guide','real world EV range test','everyday car features'],
    'benchmark_creators':[{'name':f'示例车评人{i}','platform':'douyin'} for i in range(1,6)]}


class BenchmarkCreatorsTest(unittest.TestCase):
    setUp=original.RadarTest.setUp
    request=original.RadarTest.request

    def test_default_empty_and_existing_settings_survive_reconnect(self):
        self.assertEqual(radar.settings(self.db)['benchmark_creators'],[])
        creators=[{'name':'原有车评人','platform':'bilibili'}]
        radar.put(self.db,'benchmark_creators',creators)
        with radar.connect() as other:
            self.assertEqual(radar.settings(other)['benchmark_creators'],creators)

    def test_preset_roundtrip_changes_only_four_settings(self):
        preset=json.loads(json.dumps(PRESET_DOC,ensure_ascii=False))
        self.assertEqual(set(preset),{'field','fixed_keywords','youtube_fixed_keywords','benchmark_creators'})
        self.assertTrue(6<=len(preset['fixed_keywords'])<=10)
        self.assertTrue(4<=len(preset['youtube_fixed_keywords'])<=6)
        self.assertEqual(len(preset['benchmark_creators']),5)
        before=radar.settings(self.db)
        status,body=self.request('POST','/api/settings',preset)
        self.assertEqual(status,200,body)
        after=self.request('GET','/api/state')[1]['settings']
        for key,value in preset.items():self.assertEqual(after[key],value)
        for key in before:
            if key not in preset:self.assertEqual(after[key],before[key],key)

    def test_trim_deduplicate_platforms_and_clear(self):
        creators=[{'name':' 车评人 ','platform':'douyin'},{'name':'车评人','platform':'douyin'},
                  {'name':'车评人','platform':'bilibili'},{'name':'Reviewer','platform':'youtube'},
                  {'name':'reviewer','platform':'youtube'},{'name':'车评人','platform':'x'}]
        status,body=self.request('POST','/api/settings',{'benchmark_creators':creators})
        self.assertEqual(status,200,body)
        self.assertEqual(body['settings']['benchmark_creators'],[
            {'name':'车评人','platform':'douyin'},{'name':'车评人','platform':'bilibili'},
            {'name':'Reviewer','platform':'youtube'},{'name':'车评人','platform':'x'}])
        self.assertEqual(self.request('POST','/api/settings',{'benchmark_creators':[]})[0],200)
        self.assertEqual(radar.setting(self.db,'benchmark_creators'),[])

    def test_invalid_creators_reject_whole_request(self):
        good={'name':'车评人','platform':'douyin'}
        invalid=[None,{},'车评人',[None],[{}],[{'name':'只有名称'}],
                 [{**good,'extra':True}],[{**good,'name':True}],[{**good,'name':''}],
                 [{**good,'name':'  '}],[{**good,'name':'字'*61}],
                 [{**good,'name':'a\nb'}],[{**good,'name':'a\rb'}],
                 [{**good,'name':'a｜b'}],[{**good,'name':'a|b'}],
                 [{**good,'platform':[]}],[{**good,'platform':'抖音'}],
                 [{**good,'platform':'unknown'}],[good]*21]
        before=radar.settings(self.db)
        for creators in invalid:
            with self.subTest(creators=creators):
                status,_=self.request('POST','/api/settings',{'field':'不能部分保存','benchmark_creators':creators})
                self.assertEqual(status,400)
                self.assertEqual(radar.settings(self.db),before)
        boundary=[{'name':str(i)+'字'*58,'platform':'douyin'} for i in range(20)]
        self.assertEqual(self.request('POST','/api/settings',{'benchmark_creators':boundary})[0],200)

    def test_pick_prompts_follow_field_and_benchmarks_on_each_platform(self):
        preset=json.loads(json.dumps(PRESET_DOC,ensure_ascii=False))
        self.request('POST','/api/settings',preset)
        for platform in ('douyin','bilibili','youtube'):
            with self.subTest(platform=platform):
                word='family car test' if platform=='youtube' else '家庭选车'
                with patch.object(radar,'screening_json',return_value={'keywords':[
                    {'word':word,'hot_word':'','reason':'模拟选题'}]}) as model:
                    words=radar.pick_keywords(self.db,self.day,[],platform)
                prompt=model.call_args.args[1]
                self.assertIn(preset['field'],prompt)
                self.assertIn('参考对标博主的选题风格',prompt)
                for creator in preset['benchmark_creators']:self.assertIn(creator['name'],prompt)
                self.assertIn('"platform": "抖音"',prompt)
                expected=(preset['youtube_fixed_keywords']+[word]) if platform=='youtube' else preset['fixed_keywords']
                self.assertEqual([r['word'] for r in words],expected)
                self.assertFalse(any(r['word'] in [c['name'] for c in preset['benchmark_creators']] for r in words))
                if platform=='youtube':self.assertIn('玩具、遥控车、游戏',prompt)

    def test_classification_uses_current_field_and_rejects_industry_only_match(self):
        preset=json.loads(json.dumps(PRESET_DOC,ensure_ascii=False))
        self.request('POST','/api/settings',preset)
        rows=[{'rank':1,'word':'家庭新车空间体验','hot_value':100}]
        radar.save_hot(self.db,self.day,rows,'bilibili')
        with patch.object(radar,'screening_json',return_value=original.classified(rows)) as model:
            self.assertEqual(radar.classify_hot(self.db,self.day,'bilibili'),1)
        prompt=model.call_args.args[1]
        self.assertIn(preset['field'],prompt)
        self.assertIn('不要仅因同属一个大行业就判为相关',prompt)
        self.assertTrue(radar.hot_items(self.db,self.day)[0]['related'])
        self.request('POST','/api/settings',{'field':'家居装修'})
        self.assertIsNone(radar.hot_items(self.db,self.day)[0]['related'])


if __name__=='__main__':unittest.main()
