"""百炼离线回归。仅临时库、假密钥、模拟进程和 HTTP，绝不发真实请求。"""
from contextlib import redirect_stdout, redirect_stderr
from datetime import timedelta
import io
import json
from pathlib import Path
import subprocess
import traceback
import unittest
from unittest.mock import Mock, patch
import urllib.error
import wave

import aliyun
import chinese
import export_demo
import platforms
import radar
import server
import test_radar as original

KEY_READER=aliyun.api_key
SECRET='sk-offline-test-secret-123456'


class AliyunTest(unittest.TestCase):
    setUp=original.RadarTest.setUp
    video=original.RadarTest.video
    seed_video=original.RadarTest.seed_video
    request=original.RadarTest.request
    keyword=original.RadarTest.keyword

    def enable_qwen(self):
        radar.put(self.db,'analysis_provider','qwen')
        patcher=patch.object(aliyun,'api_key',return_value=SECRET)
        patcher.start();self.addCleanup(patcher.stop)

    def public(self,platform,ident='0',views=50000,followers=25000):
        source='BV123456789'+ident if platform=='bilibili' else 'AbCdEfG123'+ident
        row=platforms.item(platform,source,'养车成本' if platform=='bilibili' else 'Car review','模拟作者',
                           {'views':views,'followers':followers,'likes':20000},
                           (radar.now()-timedelta(days=1)).isoformat())
        row['duration']=600
        return row

    def audio_download(self,args,timeout):
        if '--skip-download' not in args:
            (Path(args[args.index('-o')+1]).parent/'audio.webm').write_bytes(b'fake audio')
        return ''

    def fake_ffmpeg(self,args,**kwargs):
        self.assertEqual(args[0],str(Path.home()/'.local/bin/ffmpeg'))
        self.assertEqual(args[args.index('-segment_time')+1],'180')
        folder=Path(args[-1]).parent
        for i in range(2):
            with wave.open(str(folder/f'part-{i:06d}.wav'),'wb') as audio:
                audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(16000)
                audio.writeframes(b'\0'*320)
        return Mock(returncode=0)

    def test_keychain_exact_command_and_no_output_on_errors(self):
        output=io.StringIO()
        with redirect_stdout(output),redirect_stderr(output):
            with patch('subprocess.run',return_value=Mock(returncode=0,stdout=SECRET+'\n',stderr=SECRET)) as run:
                self.assertEqual(KEY_READER(),SECRET)
            self.assertEqual(run.call_args.args[0],['security','find-generic-password','-s','dashscope-api','-a','baokuan-radar','-w'])
            for value in [Mock(returncode=1,stdout=SECRET,stderr=SECRET),FileNotFoundError(SECRET),
                          subprocess.TimeoutExpired('security',5,output=SECRET,stderr=SECRET)]:
                with patch('subprocess.run',**({'side_effect':value} if isinstance(value,Exception) else {'return_value':value})):
                    self.assertEqual(KEY_READER(),'')
        self.assertEqual(output.getvalue(),'')

    def test_http_header_only_and_errors_hide_secrets_and_tracebacks(self):
        opener=Mock()
        reply={'choices':[{'finish_reason':'stop','message':{'content':'成功'}}]}
        opener.open.return_value=io.BytesIO(json.dumps(reply).encode())
        with patch('urllib.request.build_opener',return_value=opener):
            self.assertEqual(aliyun.completion({'model':'qwen-plus'},SECRET),'成功')
        request=opener.open.call_args.args[0]
        self.assertEqual(request.full_url,aliyun.ENDPOINT)
        self.assertNotIn(SECRET,request.full_url+request.data.decode())
        self.assertEqual(request.get_header('Authorization'),'Bearer '+SECRET)
        for error in [urllib.error.HTTPError('https://example.invalid/'+SECRET,429,SECRET,{},io.BytesIO(SECRET.encode())),
                      OSError(SECRET),TimeoutError(SECRET)]:
            opener.open.side_effect=error
            with patch('urllib.request.build_opener',return_value=opener):
                try:aliyun.completion({},SECRET)
                except aliyun.AliyunError as failure:
                    self.assertNotIn(SECRET,''.join(traceback.format_exception(failure)))
                else:self.fail('请求失败必须报固定错误')
        handler=aliyun.urllib.request.HTTPRedirectHandler
        with patch('urllib.request.build_opener',return_value=opener) as build:
            with self.assertRaises(aliyun.AliyunError):aliyun.completion({},SECRET)
        redirect=build.call_args.args[0]()
        self.assertIsInstance(redirect,handler)
        self.assertIsNone(redirect.redirect_request(None,None,302,'',{},'https://example.invalid/'))

    def test_bad_http_success_empty_truncated_and_key_echo_rejected(self):
        for doc in [b'invalid',b'{}',json.dumps({'choices':[{'finish_reason':'length','message':{'content':'text'}}]}).encode(),
                    json.dumps({'choices':[{'finish_reason':'stop','message':{'content':''}}]}).encode(),
                    json.dumps({'choices':[{'finish_reason':'stop','message':{'content':SECRET}}]}).encode(),b' '* (4*1024*1024+1)]:
            with self.subTest(size=len(doc)),patch('urllib.request.build_opener',return_value=Mock(open=Mock(return_value=io.BytesIO(doc)))):
                with self.assertRaises(aliyun.AliyunError) as caught:aliyun.completion({},SECRET)
                self.assertNotIn(SECRET,str(caught.exception))

    def test_no_key_uses_codex_and_whisper_and_status_only_boolean(self):
        radar.put(self.db,'analysis_provider','qwen')
        ident=self.seed_video(transcript='养车成本')
        with patch.object(aliyun,'completion') as remote,patch.object(radar,'codex',return_value=original.analysis()):
            self.assertTrue(radar.process(self.db,ident))
        remote.assert_not_called()
        row=self.db.execute('select * from videos').fetchone()
        self.assertEqual((row['analysis_provider'],row['analysis_model']),('codex','codex'))
        self.assertIs(self.request('GET','/api/state')[1]['aliyun_key_configured'],False)
        with patch.object(radar,'download',side_effect=lambda u,p:p.write_bytes(b'fake')),patch('subprocess.run',return_value=Mock(returncode=0,stdout='{"ok":true,"text":"本地轉寫"}')),patch.object(aliyun,'recognize') as remote:
            self.assertEqual(radar.transcribe(self.video()),('本地转写','whisper'))
        remote.assert_not_called()

    def test_asr_base64_order_language_and_cleanup(self):
        media=self.root/'source.mp4';media.write_bytes(b'fake')
        for language,texts,expected in [('zh',['這是車評','錄音轉寫'],'这是车评\n录音转写'),('en',['First part','Second part'],'First part\nSecond part')]:
            with patch('subprocess.run',side_effect=self.fake_ffmpeg),patch.object(aliyun,'completion',side_effect=texts) as api:
                self.assertEqual(aliyun.recognize(media,language,key=SECRET),expected)
            self.assertEqual(api.call_count,2)
            payload,key=api.call_args_list[0].args
            self.assertEqual((key,payload['model'],payload['asr_options']['language']),(SECRET,'qwen3-asr-flash',language))
            data=payload['messages'][0]['content'][0]['input_audio']['data']
            self.assertTrue(data.startswith('data:audio/wav;base64,'));self.assertNotIn(str(media),data)
            self.assertEqual(list(self.root.glob('aliyun-*')),[])

    def test_asr_limits_abort_before_network_and_cleanup(self):
        media=self.root/'source.mp4';media.write_bytes(b'fake')
        with patch('subprocess.run',side_effect=self.fake_ffmpeg),patch.object(aliyun,'MAX_AUDIO_BYTES',10),patch.object(aliyun,'completion') as api:
            with self.assertRaises(aliyun.AliyunError):aliyun.recognize(media,key=SECRET)
        api.assert_not_called();self.assertEqual(list(self.root.glob('aliyun-*')),[])
        opened=Mock();opened.__enter__=Mock(return_value=Mock(getnframes=lambda:301,getframerate=lambda:1));opened.__exit__=Mock(return_value=False)
        with patch('subprocess.run',side_effect=self.fake_ffmpeg):
            # 先生成有效文件，再模拟读取的时长越界。
            original_open=wave.open
            def read_duration(path,mode):return original_open(path,mode) if mode=='wb' else opened
            with patch('wave.open',side_effect=read_duration),patch.object(aliyun,'completion') as api:
                with self.assertRaises(aliyun.AliyunError):aliyun.recognize(media,key=SECRET)
        api.assert_not_called();self.assertEqual(list(self.root.glob('aliyun-*')),[])

    def test_chinese_simplification_and_fallback_preserve_english_numbers(self):
        text='這個臺灣車評影片的錄音轉寫，繁體中文與English 123。'
        expected='这个台湾车评影片的录音转写，繁体中文与English 123。'
        self.assertEqual(chinese.simplified(text),expected)
        with patch.object(chinese.sys,'platform','other'):self.assertEqual(chinese.simplified(text),expected)

    def test_asr_failures_whole_transcript_falls_back_all_video_platforms(self):
        self.enable_qwen()
        for platform in ('douyin','bilibili','youtube'):
            row=self.video() if platform=='douyin' else self.public(platform)
            for failure in [aliyun.AliyunError(),OSError(SECRET),ValueError('文件太大')]:
                with self.subTest(platform=platform,failure=type(failure).__name__),patch.object(radar,'download',side_effect=lambda u,p:p.write_bytes(b'fake')),patch('platforms.run_process',side_effect=self.audio_download),patch.object(aliyun,'recognize',side_effect=failure),patch('subprocess.run',return_value=Mock(returncode=0,stdout=json.dumps({'ok':True,'text':'Local English' if platform=='youtube' else '本地轉寫'}))):
                    self.assertEqual(radar.transcribe(row),('Local English' if platform=='youtube' else '本地转写','whisper'))
                self.assertEqual(list(radar.MEDIA.iterdir()),[])

    def test_asr_success_source_and_subtitles_still_first(self):
        self.enable_qwen()
        for platform in ('douyin','bilibili','youtube'):
            row=self.video() if platform=='douyin' else self.public(platform)
            with patch.object(radar,'download',side_effect=lambda u,p:p.write_bytes(b'fake')),patch('platforms.run_process',side_effect=self.audio_download),patch.object(aliyun,'recognize',return_value='百炼转写') as asr,patch('subprocess.run') as whisper:
                self.assertEqual(radar.transcribe(row),('百炼转写','aliyun-asr'))
            whisper.assert_not_called();self.assertEqual(asr.call_args.args[1],'en' if platform=='youtube' else 'zh')
        def subtitle(args,timeout):
            (Path(args[args.index('-o')+1]).parent/'audio.en.json3').write_text('{"events":[{"segs":[{"utf8":"English subtitles"}]}]}')
        with patch('platforms.run_process',side_effect=subtitle),patch.object(aliyun,'recognize') as asr:
            self.assertEqual(radar.transcribe(self.public('youtube')),('English subtitles','subtitles'))
        asr.assert_not_called()

    def test_qwen_retries_json_schema_and_evidence_then_errors_safely(self):
        self.enable_qwen();ident=self.seed_video(transcript='养车成本',transcript_source='aliyun-asr')
        row=self.db.execute('select * from videos').fetchone();valid=original.analysis()
        invalid=original.analysis();invalid['hook']='根本不存在的证据'
        for bad in ['not JSON','{}',json.dumps(invalid,ensure_ascii=False)]:
            with patch.object(aliyun,'completion',side_effect=[bad,json.dumps(valid,ensure_ascii=False)]) as api:
                self.assertEqual(radar.analyze(self.db,row),valid)
            self.assertEqual(api.call_count,2)
            payload=api.call_args.args[0]
            self.assertEqual(payload['response_format'],{'type':'json_object'})
            self.assertIs(payload['enable_thinking'],False)
            self.assertEqual(payload['model'],'qwen-plus')
            self.assertIn('JSON Schema',payload['messages'][0]['content'])
            self.assertNotIn(SECRET,json.dumps(payload,ensure_ascii=False))
        with patch.object(aliyun,'completion',return_value='{}') as api,patch.object(radar,'codex') as codex:
            self.assertFalse(radar.process(self.db,ident,force=True))
        self.assertEqual(api.call_count,2);codex.assert_not_called()
        self.assertIn('两次未通过',self.db.execute('select error from videos').fetchone()[0])

    def test_models_coexist_switch_persist_and_failed_retry_keeps_both(self):
        ident=self.seed_video(transcript='养车成本',saved=1,note='保留',tags='["自定义"]')
        with patch.object(radar,'codex',return_value=original.analysis()):self.assertTrue(radar.process(self.db,ident))
        self.enable_qwen();doc=original.analysis();doc['summary']='千问的新分析'
        with patch.object(aliyun,'completion',return_value=json.dumps(doc,ensure_ascii=False)):
            self.assertTrue(radar.process(self.db,ident,force=True,provider='qwen'))
        variants=self.request('GET','/api/video?id='+ident)[1]
        self.assertEqual(set(variants['analyses']),{'codex','qwen'})
        self.assertEqual(variants['analysis_model'],'qwen-plus')
        codex=self.request('GET','/api/video?id='+ident+'&analysis_provider=codex')[1]
        self.assertEqual(codex['analysis_model'],'codex');self.assertEqual(codex['analysis'],original.analysis())
        self.assertEqual(self.request('GET','/api/video?id='+ident)[1]['analysis'],doc)
        self.assertEqual((variants['saved'],variants['note'],variants['tags']),(1,'保留',['自定义']))
        with patch.object(aliyun,'completion',return_value='{}'):self.assertFalse(radar.process(self.db,ident,force=True))
        self.assertEqual(self.request('GET','/api/video?id='+ident)[1]['analyses'],variants['analyses'])
        reopened=radar.connect();reopened.close()
        self.assertEqual(self.db.execute('select count(*) from video_analyses').fetchone()[0],2)

    def test_legacy_analysis_is_codex_and_saved_before_first_qwen(self):
        old=original.legacy_analysis();ident=self.seed_video(status='done',analysis=json.dumps(old),transcript='养车成本')
        self.assertEqual(self.request('GET','/api/video?id='+ident)[1]['analysis_model'],'codex')
        self.enable_qwen()
        with patch.object(aliyun,'completion',return_value=json.dumps(original.analysis())):self.assertTrue(radar.process(self.db,ident,force=True))
        self.assertEqual(self.request('GET','/api/video?id='+ident+'&analysis_provider=codex')[1]['analysis'],old)

    def test_changing_qwen_model_keeps_each_models_latest_result(self):
        self.enable_qwen();ident=self.seed_video(transcript='养车成本')
        first=original.analysis();second=original.analysis();second['summary']='另一模型的结果'
        with patch.object(aliyun,'completion',return_value=json.dumps(first)):
            self.assertTrue(radar.process(self.db,ident))
        radar.put(self.db,'qwen_model','qwen3-max')
        with patch.object(aliyun,'completion',return_value=json.dumps(second)):
            self.assertTrue(radar.process(self.db,ident,force=True))
        result=self.request('GET','/api/video?id='+ident+'&analysis_model=qwen-plus')[1]
        self.assertEqual(result['analysis'],first)
        self.assertEqual(result['analysis_model'],'qwen-plus')
        self.assertEqual(set(result['analysis_versions']),{'qwen-plus','qwen3-max'})
        self.assertEqual(self.request('GET','/api/video?id='+ident)[1]['analysis'],second)
        second['summary']='同一模型的最新结果'
        with patch.object(aliyun,'completion',return_value=json.dumps(second)):
            self.assertTrue(radar.process(self.db,ident,force=True))
        self.assertEqual(self.db.execute('select count(*) from video_analyses').fetchone()[0],2)
        self.assertEqual(self.request('GET','/api/video?id='+ident+'&analysis_model=qwen3-max')[1]['analysis'],second)

    def test_partial_asr_failure_discards_chunks_before_whisper(self):
        self.enable_qwen()
        def process(args,**kwargs):
            if args[0]==str(aliyun.FFMPEG):return self.fake_ffmpeg(args,**kwargs)
            return Mock(returncode=0,stdout='{"ok":true,"text":"完整本地转写"}')
        with patch.object(radar,'download',side_effect=lambda u,p:p.write_bytes(b'fake')),patch('subprocess.run',side_effect=process),patch.object(aliyun,'completion',side_effect=['不能留下的半段',aliyun.AliyunError()]):
            self.assertEqual(radar.transcribe(self.video()),('完整本地转写','whisper'))
        self.assertEqual(list(radar.MEDIA.iterdir()),[])

    def test_keyword_classification_follow_selected_provider(self):
        self.enable_qwen();hot=[{'rank':1,'word':'车评热词','hot_value':1}]
        radar.save_hot(self.db,self.day,hot)
        with patch.object(aliyun,'completion',side_effect=[json.dumps(original.classified(hot)),json.dumps({'keywords':[{'word':'保养','hot_word':'车评热词','reason':'模拟理由'}]})]) as api,patch.object(radar,'codex') as codex:
            self.assertEqual(radar.classify_hot(self.db,self.day),1)
            self.assertTrue(radar.pick_keywords(self.db,self.day,hot))
        self.assertEqual(api.call_count,2);codex.assert_not_called()

    def test_key_not_in_database_logs_export_state_or_error(self):
        self.enable_qwen();ident=self.seed_video(transcript='养车成本')
        output=io.StringIO()
        opener=Mock(open=Mock(side_effect=OSError(SECRET)))
        with redirect_stdout(output),redirect_stderr(output),patch('urllib.request.build_opener',return_value=opener):
            self.assertFalse(radar.process(self.db,ident))
        with patch.object(aliyun,'completion',return_value=json.dumps(original.analysis())):self.assertTrue(radar.process(self.db,ident,force=True))
        self.assertTrue(self.request('GET','/api/state')[1]['aliyun_key_configured'])
        dump='\n'.join(self.db.iterdump())
        snapshot=json.dumps(export_demo.export_snapshot(self.root/'radar.sqlite3'),ensure_ascii=False)
        state=json.dumps(self.request('GET','/api/state')[1],ensure_ascii=False)
        for value in (dump,snapshot,state,output.getvalue(),(self.root/'radar.sqlite3').read_bytes().decode('latin1')):
            self.assertNotIn(SECRET,value)
        self.assertEqual(output.getvalue(),'')
        # 导出器不读钥匙串；对旧自由文本意外带入的 DashScope token 也做脱敏。
        self.db.execute('update videos set desc=?',(SECRET,));self.db.commit()
        self.assertNotIn(SECRET,json.dumps(export_demo.export_snapshot(self.root/'radar.sqlite3')))

    def test_default_migration_preserves_custom_values_and_runs_only_once(self):
        self.db.execute("delete from settings where key='threshold_defaults_v2'")
        for key,value in [('bilibili_min_views',10000),('youtube_min_views',10000),('auto',False),('youtube_enabled',True),('min_likes',12345)]:radar.put(self.db,key,value)
        before={k:radar.setting(self.db,k) for k in ('auto','youtube_enabled','min_likes','douyin_enabled','bilibili_enabled')}
        db=radar.connect();db.close()
        for p in ('bilibili','youtube'):
            self.assertEqual((radar.setting(self.db,p+'_min_views'),radar.setting(self.db,p+'_min_ratio')),(50000,2))
        self.assertEqual(before,{k:radar.setting(self.db,k) for k in before})
        radar.put(self.db,'youtube_min_views',10000)
        db=radar.connect();db.close();self.assertEqual(radar.setting(self.db,'youtube_min_views'),10000)
        self.db.execute("delete from settings where key='threshold_defaults_v2'")
        radar.put(self.db,'bilibili_min_views',80000);radar.put(self.db,'bilibili_min_ratio',3)
        db=radar.connect();db.close()
        self.assertEqual((radar.setting(self.db,'bilibili_min_views'),radar.setting(self.db,'bilibili_min_ratio')),(80000,3))

    def test_new_public_thresholds_require_both_views_and_ratio(self):
        for p in ('bilibili','youtube'):
            rows=[self.public(p,str(i),views,followers) for i,(views,followers) in enumerate([
                (49999,1),(50000,25000),(50000,25001),(90000,0),(90000,None),(60000,1000)])]
            with patch('platforms.bili_search',return_value=rows),patch('platforms.youtube_public_search',return_value=rows):
                self.assertEqual(radar.search_public_keyword(self.db,self.day,'车评',p),2)
            selected=[r['aweme_id'] for r in self.db.execute("select aweme_id from videos where platform=? and status='pending'",(p,))]
            self.assertEqual(set(selected),{rows[1]['aweme_id'],rows[5]['aweme_id']})
            radar.put(self.db,p+'_min_ratio',0)
            with patch('platforms.bili_search',return_value=rows),patch('platforms.youtube_public_search',return_value=rows):
                self.assertEqual(radar.search_public_keyword(self.db,self.day,'车评',p),3)

    def test_douyin_ratio_zero_unlimited_and_custom_boundary(self):
        for minimum,expected in [(0,3),(2,1)]:
            self.db.execute('delete from videos');self.db.execute('delete from keywords');self.db.commit();self.keyword()
            radar.put(self.db,'douyin_min_ratio',minimum)
            rows=[self.video(str(12345678900+i),likes=likes,followers=followers) for i,(likes,followers) in enumerate([(10000,5000),(9999,1),(20000,10001),(20000,0)])]
            with patch.object(radar,'dy',return_value={'ok':True,'items':rows}):self.assertEqual(radar.search_keyword(self.db,self.day,'车评'),expected)

    def test_settings_model_ratio_validation_and_reanalyze_dispatch(self):
        valid={'analysis_provider':'qwen','qwen_model':'qwen-plus','douyin_min_ratio':'0.5','youtube_min_ratio':2.5,'bilibili_min_ratio':0}
        self.assertEqual(self.request('POST','/api/settings',valid)[0],200)
        for value in [-1,True,'nan','inf',1000001,{},[]]:
            self.assertEqual(self.request('POST','/api/settings',{'youtube_min_ratio':value})[0],400)
        for data in [{'analysis_provider':'other'},{'qwen_model':'../secret'},{'qwen_model':SECRET}]:
            self.assertEqual(self.request('POST','/api/settings',data)[0],400)
        ident=self.seed_video()
        with patch.object(server,'start_job',return_value=True) as job:
            self.assertEqual(self.request('POST','/api/reanalyze',{'id':ident,'analysis_provider':'qwen'})[0],200)
            with patch.object(server,'reanalyze_work',return_value='完成') as work:job.call_args.args[1]()
            work.assert_called_once_with(ident,'qwen')
        self.assertEqual(self.request('POST','/api/reanalyze',{'id':ident,'analysis_provider':'other'})[0],400)


if __name__=='__main__':unittest.main()
