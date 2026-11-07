"""筛选与分析模型分流回归：仅临时数据库、模拟密钥和模型返回。"""
from contextlib import closing
import json
import unittest
from unittest.mock import patch

import aliyun
import radar
import test_radar as original


SECRET='sk-offline-routing-secret'


class ModelRoutingTest(unittest.TestCase):
    setUp=original.RadarTest.setUp
    seed_video=original.RadarTest.seed_video
    video=original.RadarTest.video
    request=original.RadarTest.request

    def assert_provider(self,provider,codex,remote):
        if provider=='qwen':
            codex.assert_not_called();remote.assert_called_once()
            self.assertEqual(remote.call_args.args[0]['model'],'qwen3-max')
        else:
            codex.assert_called_once();remote.assert_not_called()

    def test_defaults_split_screening_and_analysis(self):
        self.assertEqual(radar.settings(self.db)['screening_provider'],'qwen')
        self.assertEqual(radar.settings(self.db)['analysis_provider'],'codex')

    def test_screening_uses_its_setting_on_every_platform(self):
        radar.put(self.db,'qwen_model','qwen3-max')
        for screening in ('qwen','codex'):
            for analysis in ('codex','qwen'):
                radar.put(self.db,'screening_provider',screening)
                radar.put(self.db,'analysis_provider',analysis)
                for platform in ('douyin','bilibili','youtube','x'):
                    with self.subTest(screening=screening,analysis=analysis,platform=platform):
                        self.db.execute('delete from hot');self.db.execute('delete from keywords');self.db.commit()
                        hot=[{'rank':1,'word':'车评热词','hot_value':0}]
                        radar.save_hot(self.db,self.day,hot,platform)
                        doc=original.classified(hot)
                        with patch.object(aliyun,'api_key',return_value=SECRET),patch.object(radar,'codex',return_value=doc) as codex,patch.object(aliyun,'completion',return_value=json.dumps(doc)) as remote:
                            self.assertEqual(radar.classify_hot(self.db,self.day,platform),1)
                        self.assert_provider(screening,codex,remote)
                        self.assertEqual(tuple(self.db.execute('select track,related from hot').fetchone()),('汽车',1))
                        message=self.db.execute('select message from log order by rowid desc limit 1').fetchone()[0]
                        self.assertIn(radar.platforms.NAMES[platform]+'热榜分类',message)
                        self.assertIn('千问（qwen3-max）' if screening=='qwen' else 'Codex',message)
                        self.assertNotIn(SECRET,message)
                        if platform=='x':continue
                        word='EV road test' if platform=='youtube' else '保养'
                        doc={'keywords':[{'word':word,'hot_word':'车评热词','reason':'模拟理由'}]}
                        with patch.object(aliyun,'api_key',return_value=SECRET),patch.object(radar,'codex',return_value=doc) as codex,patch.object(aliyun,'completion',return_value=json.dumps(doc)) as remote:
                            self.assertIn(word,[r['word'] for r in radar.pick_keywords(self.db,self.day,hot,platform)])
                        self.assert_provider(screening,codex,remote)
                        message=self.db.execute('select message from log order by rowid desc limit 1').fetchone()[0]
                        self.assertIn(radar.platforms.NAMES[platform]+'挑词',message)
                        self.assertIn('千问（qwen3-max）' if screening=='qwen' else 'Codex',message)
                        self.assertNotIn(SECRET,message)

    def test_analysis_uses_its_setting_and_manual_override_stays_local(self):
        ident=self.seed_video(transcript='养车成本')
        radar.put(self.db,'qwen_model','qwen3-max')
        for screening in ('qwen','codex'):
            for analysis in ('codex','qwen'):
                radar.put(self.db,'screening_provider',screening)
                radar.put(self.db,'analysis_provider',analysis)
                for override in (None,'codex' if analysis=='qwen' else 'qwen'):
                    expected=override or analysis
                    with self.subTest(screening=screening,analysis=analysis,override=override),patch.object(aliyun,'api_key',return_value=SECRET),patch.object(radar,'codex',return_value=original.analysis()) as codex,patch.object(aliyun,'completion',return_value=json.dumps(original.analysis())) as remote:
                        self.assertTrue(radar.process(self.db,ident,force=True,provider=override))
                        self.assert_provider(expected,codex,remote)
                    self.assertEqual(tuple(self.db.execute('select analysis_provider,analysis_model from videos').fetchone()),
                                     (expected,'qwen3-max' if expected=='qwen' else 'codex'))
                    self.assertEqual(radar.setting(self.db,'screening_provider'),screening)
                    self.assertEqual(radar.setting(self.db,'analysis_provider'),analysis)

    def test_no_key_falls_back_for_screening_and_analysis_without_changing_settings(self):
        radar.put(self.db,'screening_provider','qwen');radar.put(self.db,'analysis_provider','qwen')
        hot=[{'rank':1,'word':'车评热词','hot_value':0}]
        radar.save_hot(self.db,self.day,hot)
        ident=self.seed_video(transcript='养车成本')
        docs=[original.classified(hot),{'keywords':[{'word':'保养','hot_word':'车评热词','reason':'模拟理由'}]},original.analysis()]
        with patch.object(aliyun,'api_key',return_value=''),patch.object(aliyun,'completion') as remote,patch.object(radar,'codex',side_effect=docs) as codex:
            self.assertEqual(radar.classify_hot(self.db,self.day),1)
            self.assertTrue(radar.pick_keywords(self.db,self.day,hot))
            self.assertTrue(radar.process(self.db,ident))
        remote.assert_not_called()
        self.assertEqual([c.args[1] for c in codex.call_args_list],['hot_classification.json','pick.json','analysis.json'])
        messages=[r[0] for r in self.db.execute('select message from log')]
        self.assertEqual(len(messages),2)
        self.assertTrue(all('Codex' in m and '回退' in m for m in messages))
        self.assertEqual(tuple(self.db.execute('select analysis_provider,analysis_model from videos').fetchone()),('codex','codex'))
        self.assertEqual((radar.setting(self.db,'screening_provider'),radar.setting(self.db,'analysis_provider')),('qwen','qwen'))

    def test_failed_screening_logs_attempted_model_without_key_or_codex_retry(self):
        radar.put(self.db,'screening_provider','qwen');radar.put(self.db,'qwen_model','qwen3-max')
        for task in ('分类','挑词'):
            with self.subTest(task=task),patch.object(aliyun,'api_key',return_value=SECRET),patch.object(aliyun,'completion',side_effect=aliyun.AliyunError()),patch.object(radar,'codex') as codex:
                if task=='分类':
                    radar.save_hot(self.db,self.day,[{'rank':1,'word':'车评热词','hot_value':0}])
                    self.assertIn('分类失败',radar.try_classify_hot(self.db,self.day))
                else:
                    with self.assertRaises(aliyun.AliyunError):radar.pick_keywords(self.db,self.day,[])
                codex.assert_not_called()
        messages='\n'.join(r[0] for r in self.db.execute('select message from log'))
        self.assertIn('热榜分类：千问（qwen3-max）',messages)
        self.assertIn('挑词：千问（qwen3-max）',messages)
        self.assertNotIn(SECRET,messages)

    def test_migration_only_inserts_missing_screening_and_is_idempotent(self):
        for analysis in (None,'codex','qwen'):
            with self.subTest(analysis=analysis):
                self.db.execute("delete from settings where key in ('screening_provider','analysis_provider')");self.db.commit()
                if analysis is not None:radar.put(self.db,'analysis_provider',analysis)
                for key,value in [('auto',False),('qwen_model','qwen3-max'),('field','自定义领域'),('min_likes',0),('youtube_min_views',10000),('自定义设置',{'保留':['原值',0]})]:
                    radar.put(self.db,key,value)
                before=dict(self.db.execute('select key,value from settings'))
                with closing(radar.connect()) as db:
                    self.assertEqual(dict(db.execute('select key,value from settings')),{**before,'screening_provider':'"qwen"'})
                with closing(radar.connect()) as db:
                    self.assertEqual(dict(db.execute('select key,value from settings')),{**before,'screening_provider':'"qwen"'})
        for screening in ('codex','qwen'):
            radar.put(self.db,'screening_provider',screening)
            before=dict(self.db.execute('select key,value from settings'))
            with closing(radar.connect()) as db:
                self.assertEqual(dict(db.execute('select key,value from settings')),before)

    def test_settings_api_saves_each_provider_independently_and_rejects_invalid(self):
        for key,other in [('screening_provider','analysis_provider'),('analysis_provider','screening_provider')]:
            before=radar.setting(self.db,other)
            for value in ('codex','qwen'):
                status,doc=self.request('POST','/api/settings',{key:value})
                self.assertEqual(status,200)
                self.assertEqual(doc['settings'][key],value)
                state=self.request('GET','/api/state')[1]['settings']
                self.assertEqual(state[key],value);self.assertEqual(state[other],before)
            for value in ('other','',None,False,{},[]):
                saved=dict(self.db.execute('select key,value from settings'))
                status,doc=self.request('POST','/api/settings',{key:value,'field':'不能写入'})
                self.assertEqual(status,400)
                self.assertIn('筛选模型' if key=='screening_provider' else '分析模型',doc['error'])
                self.assertEqual(dict(self.db.execute('select key,value from settings')),saved)


if __name__=='__main__':unittest.main()
