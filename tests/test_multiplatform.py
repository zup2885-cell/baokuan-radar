"""多平台离线回归：只用临时库和模拟响应，真实进程/网络被基类夹具禁止。"""
from contextlib import redirect_stdout, redirect_stderr
from datetime import timedelta
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import unittest
from unittest.mock import Mock, patch
import urllib.error

import radar
import platforms
import export_demo
import server
import test_radar as original
from test_radar import analysis, classified

KEY_READER=platforms.youtube_key


class MultiPlatformTest(unittest.TestCase):
    setUp=original.RadarTest.setUp
    seed_video=original.RadarTest.seed_video
    video=original.RadarTest.video
    request=original.RadarTest.request
    ready_tick=original.RadarTest.ready_tick
    keyword=original.RadarTest.keyword

    def public(self,p='bilibili',ident=None,**metrics):
        ident=ident or ('BV1234567890' if p=='bilibili' else 'AbCdEfG1234')
        return platforms.item(p,ident,'养车成本' if p=='bilibili' else 'Car review','模拟作者',
                              {'views':20000,'likes':100,'followers':1000,'coins':20,**metrics},
                              (radar.now()-timedelta(days=1)).isoformat())

    def queue(self,p,word='car review'):
        self.db.execute('insert into keywords(day,word,platform) values(?,?,?)',(self.day,word,p));self.db.commit()
        radar.put(self.db,p+'_keywords_picked_day',self.day)

    def ready_new(self,p):
        radar.put(self.db,p+'_enabled',True)
        if p!='x':self.queue(p);self.queue(p,'second review')
        radar.save_hot(self.db,self.day,[{'rank':1,'word':'测试热词','hot_value':0}],p)
        self.db.execute('update hot set track=?,related=1,classification_key=? where platform=?',('汽车',radar.hot_context(self.db)[2],p));self.db.commit()

    def test_migration_preserves_full_legacy_records_and_auto_false(self):
        self.seed_video(status='done',saved=1,note='保留备注',tags='["原标签"]',transcript='旧逐字稿',analysis=json.dumps(analysis()))
        self.keyword();radar.save_hot(self.db,self.day,[{'rank':1,'word':'旧热词','hot_value':0}]);radar.put(self.db,'auto',False)
        folder=self.root/'legacy';folder.mkdir()
        with sqlite3.connect(folder/'radar.sqlite3') as old:
            for table in ('settings','hot','keywords','videos'):
                infos=[r for r in self.db.execute(f'pragma table_info({table})') if r['name'] not in ('platform','views','coins')]
                defs=[r['name']+' '+r['type']+(' default '+r['dflt_value'] if r['dflt_value'] is not None else '') for r in infos]
                pk='key' if table=='settings' else 'day,rank' if table=='hot' else 'day,word' if table=='keywords' else 'aweme_id'
                old.execute(f'create table {table}('+','.join(defs)+f',primary key({pk}))')
                cols=','.join(r['name'] for r in infos)
                old.executemany(f'insert into {table} values('+','.join('?' for r in infos)+')',[tuple(r) for r in self.db.execute(f'select {cols} from {table}')])
        with patch.object(radar,'DATA',folder):
            for _ in range(2):
                db=radar.connect()
                try:
                    r=db.execute('select * from videos').fetchone()
                    self.assertEqual((r['aweme_id'],r['saved'],r['note'],r['transcript'],r['platform']),('12345678901',1,'保留备注','旧逐字稿','douyin'))
                    self.assertIs(radar.setting(db,'auto'),False)
                    self.assertEqual(db.execute('select platform from keywords').fetchone()[0],'douyin')
                    self.assertEqual(db.execute('select platform from hot').fetchone()[0],'douyin')
                finally:db.close()

    def test_platform_primary_keys_allow_same_day_rank_word(self):
        for p in platforms.NAMES:
            radar.save_hot(self.db,self.day,[{'rank':1,'word':'同名','hot_value':0}],p)
            self.db.execute('insert into keywords(day,word,platform) values(?,?,?)',(self.day,'同名',p))
        self.db.commit()
        self.assertEqual(self.db.execute('select count(*) from hot').fetchone()[0],4)
        self.assertEqual(self.db.execute('select count(*) from keywords').fetchone()[0],4)
        radar.save_hot(self.db,self.day,[{'rank':1,'word':'更新','hot_value':1}],'bilibili')
        self.assertEqual(len(radar.hot_items(self.db,self.day)),4)
        for p in ('douyin','youtube','x'):self.assertEqual(radar.hot_items(self.db,self.day,platform=p)[0]['word'],'同名')

    def test_bili_hot_parsing_boundaries(self):
        doc={'code':0,'data':{'list':[None,{'keyword':' 汽车 ','heat':'100'},{'keyword':'汽车'}, {'show_name':'新能源','heat':True},{'keyword':''}]}}
        rows=platforms.parse_bili_hot(doc)
        self.assertEqual([r['word'] for r in rows],['汽车','新能源']);self.assertEqual(rows[0]['hot_value'],100);self.assertIsNone(rows[1]['hot_value'])
        self.assertEqual(platforms.parse_bili_hot({'code':0,'list':[{'keyword':'汽车'}]})[0]['word'],'汽车')
        for doc in ({'code':-352},{'code':0,'data':{}},{'code':0,'data':{'list':[]}}):
            with self.assertRaises(platforms.PlatformError):platforms.parse_bili_hot(doc)

    def test_bili_item_and_sdk_search_command(self):
        doc={'ok':True,'items':[{'bvid':'BV1234567890','title':'汽车','owner':{'name':'UP'},'pubdate':1793836800,'followers':1000,
                               'stat':{'view':20000,'like':20,'coin':30,'favorite':40,'share':50,'reply':60}}]}
        with patch('platforms.run_json',return_value=doc) as run:
            v=platforms.bili_search('车评',5,7)[0]
        self.assertEqual((v['views'],v['likes'],v['coins'],v['favorites'],v['shares'],v['comments'],v['followers']),(20000,20,30,40,50,60,1000))
        self.assertEqual(v['aweme_id'],'bilibili:BV1234567890')
        self.assertEqual(run.call_args.args[0][-3:],['车评','5','7'])
        self.assertIsNone(platforms.bili_item({'bvid':'../../private'}))

    def test_youtube_public_parser_missing_counts_date_and_command(self):
        doc={'entries':[{'id':'AbCdEfG1234','title':'Car review','view_count':0,'like_count':None,'channel_follower_count':100,
                         'upload_date':'20261104','duration':600}, {'id':'../bad'}, None]}
        with patch('platforms.run_json',return_value=doc) as run:
            rows=platforms.youtube_public_search('car review',5)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['views'],0);self.assertIsNone(rows[0]['likes'])
        self.assertTrue(rows[0]['published_at'].startswith('2026-11-04'))
        args=run.call_args.args[0];self.assertIn('ytsearch5:car review',args);self.assertIn('--skip-download',args);self.assertNotIn('--flat-playlist',args)
        self.assertIsNone(platforms.youtube_item({'id':'AbCdEfG1234','language':'es'}))

    def test_youtube_official_three_calls_en_and_hidden_subscribers(self):
        videos=[{'id':'AbCdEfG1234','snippet':{'title':'Car review','channelId':'channel','publishedAt':'2026-11-04T00:00:00Z','defaultAudioLanguage':'en-US'},
                 'statistics':{'viewCount':'20000','likeCount':'100'},'contentDetails':{'duration':'PT10M'}},
                {'id':'ZZZZZZZZZZZ','snippet':{'title':'Spanish','defaultLanguage':'es'}}]
        with patch('platforms.youtube_api',side_effect=[[{'id':{'videoId':'AbCdEfG1234'}}],videos,[{'id':'channel','statistics':{'subscriberCount':'500','hiddenSubscriberCount':True}}]]) as api:
            rows=platforms.youtube_search('car review',5,'2026-11-01T00:00:00Z','fake-key')
        self.assertEqual([c.args[0] for c in api.call_args_list],['search','videos','channels'])
        params=api.call_args_list[0].args[1];self.assertEqual(params['relevanceLanguage'],'en');self.assertEqual(params['order'],'viewCount')
        self.assertEqual(params['regionCode'],'US');self.assertEqual(params['videoCategoryId'],'2')
        self.assertIn('contentDetails',api.call_args_list[1].args[1]['part'])
        self.assertEqual(len(rows),1);self.assertIsNone(rows[0]['followers']);self.assertEqual(rows[0]['views'],20000)

    def test_youtube_hot_region_category_and_key_header(self):
        body={'items':[{'id':'AbCdEfG1234','snippet':{'title':'Car review'},'statistics':{'viewCount':'10'}}]}
        with patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(body).encode())) as request:
            rows=platforms.youtube_hot('mock-secret','2')
        req=request.call_args.args[0]
        self.assertNotIn('mock-secret',req.full_url)
        self.assertEqual(req.get_header('X-goog-api-key'),'mock-secret')
        self.assertIn('chart=mostPopular',req.full_url);self.assertIn('regionCode=US',req.full_url);self.assertIn('videoCategoryId=2',req.full_url)
        self.assertEqual(rows[0]['hot_value'],10)

    def test_x_first_snapshot_only_entities_duplicates_and_failure(self):
        text='<nav><a>忽略</a></nav><ol class="trend-card__list"><li><a>#Car &amp; EV</a></li><li><a>#Car &amp; EV</a></li><li><a>Auto</a></li></ol><ol class="trend-card__list"><li><a>旧榜</a></li></ol>'
        rows=platforms.parse_x(text)
        self.assertEqual([r['word'] for r in rows],['#Car & EV','Auto']);self.assertIsNone(rows[0]['hot_value'])
        with self.assertRaises(platforms.PlatformError):platforms.parse_x('<html>Cloudflare challenge</html>')

    def test_ratios_thresholds_unknown_zero_and_preserve_saved(self):
        self.assertEqual(radar.viral_ratio('douyin',100,5000,10),10)
        self.assertEqual(radar.viral_ratio('bilibili',100,5000,10),500)
        self.assertEqual(radar.viral_ratio('youtube',100,5000,10),500)
        self.assertIsNone(radar.viral_ratio('youtube',100,None,10));self.assertIsNone(radar.viral_ratio('bilibili',100,100,0))
        self.queue('bilibili');radar.put(self.db,'bilibili_min_views',10000)
        rows=[self.public(),self.public(ident='BV1234567891',views=9999,likes=9999999),self.public(ident='BV1234567892',followers=0),self.public(ident='BV1234567893')]
        rows[-1]['published_at']=(radar.now()+timedelta(days=1)).isoformat()
        with patch('platforms.bili_search',return_value=rows):self.assertEqual(radar.search_public_keyword(self.db,self.day,'car review','bilibili'),1)
        best=self.db.execute("select * from videos where aweme_id='bilibili:BV1234567890'").fetchone();self.assertEqual(best['ratio'],20)
        self.db.execute("update videos set status='done',saved=1,note='保留',tags='[\"原标签\"]' where aweme_id=?",(best['aweme_id'],));self.db.commit()
        with patch('platforms.bili_search',return_value=rows):radar.search_public_keyword(self.db,self.day,'car review','bilibili')
        best=self.db.execute('select * from videos where aweme_id=?',(best['aweme_id'],)).fetchone()
        self.assertEqual((best['status'],best['saved'],best['note'],best['tags']),('done',1,'保留','["原标签"]'))

    def test_tick_one_keyword_per_platform_and_gaps_independent(self):
        self.ready_tick();self.keyword('抖音第二词')
        for p in ('bilibili','youtube'):self.ready_new(p)
        with patch.object(radar,'dy',return_value={'ok':True,'items':[]}) as dy,patch('platforms.bili_search',return_value=[]) as bili,patch('platforms.youtube_public_search',return_value=[]) as yt:
            radar.run_tick(self.db,True);radar.run_tick(self.db,True)
            dy.assert_called_once();bili.assert_called_once();yt.assert_called_once()
            state=radar.platform_state(self.db,'youtube');state['last_search_at']-=1801;radar.put(self.db,'youtube_state',state)
            radar.run_tick(self.db,True)
            self.assertEqual(yt.call_count,2);self.assertEqual(bili.call_count,1);self.assertEqual(dy.call_count,1)
        self.assertEqual(self.db.execute("select count(*) from keywords where status='queued'").fetchone()[0],2)

    def test_rate_limit_or_generic_failure_pauses_only_failed_platform(self):
        self.ready_tick()
        for p in ('bilibili','youtube'):self.ready_new(p)
        with patch.object(radar,'dy',return_value={'ok':True,'items':[]}) as dy,patch('platforms.bili_search',side_effect=platforms.PlatformError('RATE_LIMITED')) as bili,patch('platforms.youtube_public_search',return_value=[]) as yt:
            radar.run_tick(self.db,True);radar.run_tick(self.db,True)
            dy.assert_called_once();bili.assert_called_once();yt.assert_called_once()
        self.assertTrue(radar.platform_paused(self.db,'bilibili'));self.assertFalse(radar.platform_paused(self.db,'youtube'))
        self.assertEqual(radar.account(self.db)['status'],'ready')
        self.assertEqual(self.db.execute("select error from keywords where platform='bilibili' order by rowid").fetchone()[0],'RATE_LIMITED')

    def test_douyin_failure_does_not_stop_other_platforms(self):
        self.ready_tick();self.ready_new('youtube')
        with patch.object(radar,'dy',side_effect=RuntimeError('模拟失败')),patch('platforms.youtube_public_search',return_value=[]) as yt:
            radar.run_tick(self.db,True);yt.assert_called_once()

    def test_disabled_platforms_and_auto_false_do_not_read(self):
        radar.put(self.db,'auto',False)
        with patch('platforms.bili_hot') as bili,patch('platforms.youtube_key') as key,patch('platforms.x_hot') as x:
            self.assertEqual(radar.run_tick(self.db), '不在运行时段')
            self.ready_tick();radar.run_tick(self.db,True)
            bili.assert_not_called();key.assert_not_called();x.assert_not_called()
        self.assertIs(radar.setting(self.db,'auto'),False)

    def test_no_key_skips_hot_and_uses_public_search(self):
        radar.put(self.db,'douyin_enabled',False);radar.put(self.db,'youtube_enabled',True);self.queue('youtube')
        with patch('platforms.youtube_key',return_value=''),patch('platforms.youtube_hot') as hot,patch('platforms.youtube_search') as official,patch('platforms.youtube_public_search',return_value=[]) as public:
            self.assertIn('未配置 YouTube 密钥',radar.run_tick(self.db,True))
        hot.assert_not_called();official.assert_not_called();public.assert_called_once()
        self.assertEqual(len(radar.hot_items(self.db,self.day,platform='youtube')),0)

    def test_official_daily_cap_counts_failures_no_public_fallback(self):
        self.ready_new('youtube');radar.put(self.db,'youtube_searches_per_day',1)
        with patch('platforms.youtube_search',side_effect=platforms.PlatformError()) as api,patch('platforms.youtube_public_search') as public:
            with self.assertRaises(platforms.PlatformError):radar.search_public_keyword(self.db,self.day,'car review','youtube','fake-key')
            with self.assertRaisesRegex(platforms.PlatformError,'DAILY_LIMIT'):radar.search_public_keyword(self.db,self.day,'car review','youtube','fake-key')
            api.assert_called_once();public.assert_not_called()
        self.assertEqual(radar.setting(self.db,'youtube_api_usage')['count'],1)
        radar.put(self.db,'youtube_api_usage',{'day':'2026-11-04','count':50})
        with patch('platforms.youtube_search',return_value=[]):radar.search_public_keyword(self.db,self.day,'car review','youtube','fake-key')
        self.assertEqual(radar.setting(self.db,'youtube_api_usage')['count'],1)

    def test_x_attempt_once_per_day_including_failure(self):
        radar.put(self.db,'douyin_enabled',False);radar.put(self.db,'x_enabled',True)
        with patch('platforms.x_hot',side_effect=platforms.PlatformError()) as fetch,patch.object(radar,'codex') as codex:
            radar.run_tick(self.db,True)
            state=radar.platform_state(self.db,'x');state['paused_until']=0;radar.put(self.db,'x_state',state)
            radar.run_tick(self.db,True)
            fetch.assert_called_once();codex.assert_not_called()
        self.assertEqual(self.db.execute('select count(*) from keywords').fetchone()[0],0)

    def test_classification_same_rank_word_isolated_by_platform(self):
        for p in platforms.NAMES:radar.save_hot(self.db,self.day,[{'rank':1,'word':'车评','hot_value':None}],p)
        with patch.object(radar,'codex',return_value=classified([{'rank':1,'word':'车评'}])):
            self.assertEqual(radar.classify_hot(self.db,self.day,'youtube'),1)
        self.assertEqual(radar.hot_items(self.db,self.day,platform='youtube')[0]['track'],'汽车')
        for p in ('douyin','bilibili','x'):self.assertEqual(radar.hot_items(self.db,self.day,platform=p)[0]['track'],'未分类')

    def test_english_keyword_generation_and_fixed_words(self):
        radar.put(self.db,'youtube_fixed_keywords',['car review'])
        with patch.object(radar,'codex',return_value={'keywords':[{'word':'electric car review','hot_word':'','reason':'新能源'}, {'word':'中文词','hot_word':'','reason':'排除'}]}) as codex:
            radar.pick_keywords(self.db,self.day,[],'youtube')
        self.assertIn('翻译成英文',codex.call_args.args[0]);self.assertIn('汽车',codex.call_args.args[0])
        self.assertEqual([r[0] for r in self.db.execute('select word from keywords order by rowid')],['car review','electric car review'])
        self.assertIsNone(radar.setting(self.db,'keywords_picked_day'))

    def test_key_read_failures_safe_header_errors_never_leak_to_database_or_export(self):
        secret='AIza'+'S'*35
        self.ready_new('youtube');radar.put(self.db,'douyin_enabled',False)
        out=io.StringIO();err=io.StringIO()
        with patch('platforms.youtube_key',return_value=secret),patch('urllib.request.urlopen',side_effect=urllib.error.HTTPError('https://example/?key='+secret,403,secret,{},None)),redirect_stdout(out),redirect_stderr(err):
            radar.run_tick(self.db,True)
        self.assertNotIn(secret,out.getvalue()+err.getvalue())
        dump='\n'.join(self.db.iterdump());self.assertNotIn(secret,dump)
        self.assertNotIn(secret,json.dumps(export_demo.export_snapshot(self.root/'radar.sqlite3')))
        self.assertNotIn(secret,(self.root/'radar.sqlite3').read_bytes().decode('latin1'))
        for failure in (FileNotFoundError(),subprocess.TimeoutExpired('security',5)):
            with patch('subprocess.run',side_effect=failure):self.assertEqual(KEY_READER(),'')

    def test_subtitles_first_english_and_cleanup_without_media(self):
        v=self.public('youtube')
        def subtitles(args,timeout):
            self.assertIn('--skip-download',args);self.assertIn('--write-auto-subs',args)
            target=Path(args[args.index('-o')+1]).parent/'audio.en.json3'
            target.write_text(json.dumps({'events':[{'segs':[{'utf8':'Real car review'}]},{'segs':[{'utf8':'Real car review'}]}]}))
            return ''
        with patch('platforms.run_process',side_effect=subtitles) as read,patch('subprocess.run') as whisper:
            self.assertEqual(radar.transcribe(v),('Real car review','subtitles'))
        read.assert_called_once();whisper.assert_not_called();self.assertEqual(list(radar.MEDIA.iterdir()),[])

    def test_no_subtitles_audio_only_english_whisper_and_cleanup(self):
        v=self.public('youtube');commands=[]
        def download(args,timeout):
            commands.append(args)
            if '--skip-download' not in args:
                self.assertEqual(args[args.index('-f')+1],'bestaudio')
                (Path(args[args.index('-o')+1]).parent/'audio.webm').write_bytes(b'fake')
            return ''
        with patch('platforms.run_process',side_effect=download),patch('subprocess.run',return_value=Mock(returncode=0,stdout='{"ok":true,"text":"English transcript"}')) as whisper:
            self.assertEqual(radar.transcribe(v),('English transcript','whisper'))
        self.assertEqual(len(commands),2);self.assertEqual(whisper.call_args.args[0][-1],'en');self.assertEqual(list(radar.MEDIA.iterdir()),[])

    def test_subtitle_429_falls_back_without_pausing_platform(self):
        v=self.public('youtube')
        def download(args,timeout):
            if '--skip-download' in args:raise platforms.PlatformError('RATE_LIMITED')
            (Path(args[args.index('-o')+1]).parent/'audio.webm').write_bytes(b'fake')
        with patch('platforms.run_process',side_effect=download),patch('subprocess.run',return_value=Mock(returncode=0,stdout='{"ok":true,"text":"English transcript"}')):
            self.assertEqual(radar.transcribe(v),('English transcript','whisper'))
        self.assertFalse(radar.platform_paused(self.db,'youtube'));self.assertEqual(list(radar.MEDIA.iterdir()),[])

    def test_public_requests_include_user_agent(self):
        with patch('urllib.request.urlopen',side_effect=[io.BytesIO(b'{"code":0,"list":[{"keyword":"Car"}]}'),io.BytesIO(b'<ol class="trend-card__list"><a>Car</a></ol>')]) as request:
            self.assertEqual(len(platforms.bili_hot()),1);self.assertEqual(len(platforms.x_hot()),1)
        for call in request.call_args_list:self.assertTrue(call.args[0].get_header('User-agent'))

    def test_youtube_duration_shorts_and_irrelevant_filter_before_ranking(self):
        for value,seconds in [('PT59S',59),('PT1M',60),('PT1H2M3S',3723),('P1DT1H',90000),('PT',None),('',None),(True,None),(-1,None),(float('inf'),None)]:
            self.assertEqual(platforms.duration_seconds(value),seconds)
        for duration,title,accepted in [(59,'Car review',False),(60,'Car review',True),(300,'Car #SHORTS',False),(None,'Car review',False)]:
            self.assertEqual(platforms.youtube_searchable({'desc':title,'duration':duration}),accepted)
        self.queue('youtube')
        good=self.public('youtube',views=50000);good['duration']=600
        bad=self.public('youtube',ident='BBBBBBBBBBB',views=9999999);bad.update(desc='Lightning McQueen Truck toy car review',duration=600)
        with patch('platforms.youtube_public_search',return_value=[bad,good]):
            self.assertEqual(radar.search_public_keyword(self.db,self.day,'car review','youtube'),1)
        self.assertIsNone(self.db.execute('select * from videos where aweme_id=?',(bad['aweme_id'],)).fetchone())
        self.db.execute("update videos set desc='Rc Best Car #shorts',status='failed'");self.db.commit()
        self.assertEqual(radar.review_youtube_candidates(self.db),[good['aweme_id']])
        self.assertEqual(self.db.execute('select status from videos').fetchone()[0],'irrelevant')
        self.assertEqual(self.request('GET','/api/videos?platform=youtube')[1]['items'],[])
        self.assertEqual(self.request('GET','/api/state')[1]['today_count'],0)
        with patch.object(radar,'codex') as codex:
            self.assertFalse(radar.process(self.db,good['aweme_id'],force=True));codex.assert_not_called()

    def youtube_doc(self,evidence='Car review'):
        doc=analysis();doc.update(hook=evidence,hook_translation='车评')
        for r in [doc['opening_3s']]+doc['reasons']+doc['takeaways']:r.update(evidence=evidence,evidence_translation='车评')
        return doc

    def test_youtube_process_saves_translation_schema_and_short_transcript_gaps(self):
        v=self.public('youtube')
        with patch('platforms.youtube_public_search',return_value=[v]):radar.search_public_keyword(self.db,self.day,'car review','youtube')
        with patch.object(radar,'transcribe',return_value=('[Music] la la la...','whisper')),patch.object(radar,'codex',return_value=self.youtube_doc()) as codex:
            self.assertTrue(radar.process(self.db,v['aweme_id']));self.assertEqual(codex.call_args.kwargs['timeout'],480)
        row=self.db.execute('select * from videos').fetchone();doc=json.loads(row['analysis'])
        self.assertEqual(row['status'],'done');self.assertIn('仅依据标题和描述',doc['gaps']);self.assertEqual(doc['hook_translation'],'车评')
        exported=export_demo.export_snapshot(self.root/'radar.sqlite3')['videos'][0]['analysis']
        self.assertEqual(exported['opening_3s']['evidence_translation'],'车评')

    def test_completed_history_preserved_and_restore_requires_real_evidence(self):
        v=self.public('youtube')
        with patch('platforms.youtube_public_search',return_value=[v]):radar.search_public_keyword(self.db,self.day,'car review','youtube')
        self.db.execute("update videos set status='done',desc='Toy car review',transcript='Car review',analysis=?",(json.dumps(self.youtube_doc()),));self.db.commit()
        self.assertEqual(radar.review_youtube_candidates(self.db),[])
        self.db.execute("update videos set status='irrelevant'");self.db.commit()
        self.assertTrue(radar.restore_completed_analysis(self.db,v['aweme_id']))
        self.db.execute("update videos set status='irrelevant',transcript='',desc='No evidence'");self.db.commit()
        with self.assertRaisesRegex(ValueError,'原文证据'):radar.restore_completed_analysis(self.db,v['aweme_id'])

    def test_youtube_long_transcript_head_tail_and_evidence_boundaries(self):
        v=self.public('youtube');v.update(transcript='A'*6000+'OMITTED_ONLY'+'B'*73529+'TAIL_END',transcript_source='whisper',ratio=20,keyword='car review')
        with patch.object(radar,'codex',return_value=self.youtube_doc('TAIL_END')) as codex:doc=radar.analyze(self.db,v)
        prompt=codex.call_args.args[0]
        self.assertIn('TAIL_END',prompt);self.assertNotIn('OMITTED_ONLY',prompt);self.assertIn('已截断',doc['gaps']);self.assertLess(len(prompt),12000)
        with patch.object(radar,'codex',return_value=self.youtube_doc('OMITTED_ONLY')):
            with self.assertRaisesRegex(ValueError,'原文证据'):radar.analyze(self.db,v)

    def test_demo_keeps_latest_hot_per_platform_with_source_day(self):
        yesterday=(radar.now()-timedelta(days=1)).date().isoformat()
        radar.save_hot(self.db,yesterday,[{'rank':1,'word':'Yesterday','hot_value':1}],'douyin')
        radar.save_hot(self.db,self.day,[{'rank':1,'word':'Today','hot_value':1}],'youtube')
        rows=export_demo.export_snapshot(self.root/'radar.sqlite3')['hot']
        self.assertEqual({r['platform'] for r in rows},{'douyin','youtube'})
        self.assertEqual(next(r['day'] for r in rows if r['platform']=='douyin'),yesterday)

    def test_bili_audio_failure_cleans_parts_and_pauses_without_codex(self):
        v=self.public()
        with patch('platforms.bili_search',return_value=[v]):radar.search_public_keyword(self.db,self.day,'车评','bilibili')
        def fail(args,timeout):
            folder=Path(args[args.index('-o')+1]).parent;(folder/'audio.m4a.part').write_bytes(b'partial')
            self.assertNotIn('--write-subs',args);self.assertIn('bestaudio',args)
            raise platforms.PlatformError('PLATFORM_BLOCKED')
        with patch('platforms.run_process',side_effect=fail),patch.object(radar,'codex') as codex:
            self.assertFalse(radar.process(self.db,v['aweme_id']));codex.assert_not_called()
        self.assertEqual(list(radar.MEDIA.iterdir()),[]);self.assertTrue(radar.platform_paused(self.db,'bilibili'))

    def test_youtube_analysis_requires_original_evidence_and_translation(self):
        v=self.public('youtube');v['transcript']='Car review'
        v.update(transcript_source='subtitles',ratio=20,keyword='car review');doc=analysis();doc['hook']='Car review';doc['hook_translation']='车评'
        for r in [doc['opening_3s']]+doc['reasons']+doc['takeaways']:r.update(evidence='Car review',evidence_translation='车评')
        with patch.object(radar,'codex',return_value=doc) as codex:self.assertEqual(radar.analyze(self.db,v),doc)
        self.assertEqual(codex.call_args.args[1],'analysis_youtube.json');self.assertIn('中文车评账号',codex.call_args.args[0]);self.assertIn('播放÷粉丝',codex.call_args.args[0])
        del doc['reasons'][0]['evidence_translation']
        with patch.object(radar,'codex',return_value=doc),self.assertRaises(ValueError):radar.analyze(self.db,v)

    def test_paused_platform_cannot_be_retried_manually(self):
        v=self.public()
        with patch('platforms.bili_search',return_value=[v]):radar.search_public_keyword(self.db,self.day,'车评','bilibili')
        radar.pause_platform(self.db,'bilibili','RATE_LIMITED')
        with patch.object(radar,'transcribe') as transcribe,patch.object(radar,'analyze') as analyze:
            self.assertFalse(radar.process(self.db,v['aweme_id'],force=True))
            transcribe.assert_not_called();analyze.assert_not_called()

    def test_cached_youtube_hot_hidden_when_key_removed(self):
        radar.save_hot(self.db,self.day,[{'rank':1,'word':'English trend','hot_value':1}],'youtube')
        self.assertEqual(self.request('GET','/api/hot?platform=youtube')[1]['counts']['all'],0)
        with patch('platforms.youtube_key',return_value='configured'):
            self.assertEqual(self.request('GET','/api/hot?platform=youtube')[1]['counts']['all'],1)

    def test_api_platform_filters_counts_settings_and_export(self):
        self.seed_video(status='done',saved=1)
        for p in ('bilibili','youtube'):
            self.queue(p)
            with patch('platforms.bili_search',return_value=[self.public(p)]),patch('platforms.youtube_public_search',return_value=[self.public(p)]):radar.search_public_keyword(self.db,self.day,'car review',p)
        self.db.execute("update videos set status='done',saved=1");self.db.commit()
        for p in platforms.NAMES:radar.save_hot(self.db,self.day,[{'rank':1,'word':'车评','hot_value':0}],p)
        for p in ('douyin','bilibili','youtube'):
            for path in ('/api/videos?platform=','/api/videos?view=library&platform='):
                rows=self.request('GET',path+p)[1]['items'];self.assertEqual(len(rows),1);self.assertEqual(rows[0]['platform'],p)
        body=self.request('GET','/api/hot?platform=x')[1];self.assertEqual(body['counts']['all'],1);self.assertEqual(body['items'][0]['platform'],'x')
        self.assertEqual(self.request('GET','/api/videos?platform=unknown')[0],400)
        self.assertFalse(self.request('GET','/api/state')[1]['youtube_key_configured'])
        valid={'bilibili_enabled':True,'youtube_enabled':True,'x_enabled':True,'youtube_fixed_keywords':['car review'],'youtube_searches_per_day':5,'youtube_hot_category':'2','auto':False}
        self.assertEqual(self.request('POST','/api/settings',valid)[0],200)
        for bad in ({'youtube_fixed_keywords':['车评']},{'bilibili_search_gap_minutes':1},{'youtube_searches_per_day':0},{'youtube_hot_category':'3'},{'x_enabled':1}):self.assertEqual(self.request('POST','/api/settings',bad)[0],400)
        before=(self.root/'radar.sqlite3').read_bytes();doc=export_demo.export_snapshot(self.root/'radar.sqlite3')
        self.assertEqual((self.root/'radar.sqlite3').read_bytes(),before)
        self.assertEqual({v['platform'] for v in doc['videos']},{'douyin','bilibili','youtube'})
        self.assertTrue(next(v for v in doc['videos'] if v['platform']=='bilibili')['url'].startswith('https://www.bilibili.com/video/'))
        self.assertTrue(next(v for v in doc['videos'] if v['platform']=='youtube')['url'].startswith('https://www.youtube.com/watch?v='))
        self.assertEqual({h['platform'] for h in doc['hot']},set(platforms.NAMES))
        self.assertEqual({k['platform'] for k in doc['keywords']},{'bilibili','youtube'})


if __name__=='__main__':unittest.main()
