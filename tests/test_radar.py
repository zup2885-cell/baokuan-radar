"""离线回归测试：所有数据写入临时目录，禁止真实网络和外部进程。"""
import importlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timedelta
import subprocess
import time
import sqlite3

import radar
import server
import dy_worker


def legacy_analysis():
    return {
        'summary': '用用车成本吸引车主', 'hook': '你每月养车花多少？',
        'hook_type': '痛点提问', 'structure': ['提问', '列举成本', '总结'],
        'template': '每月 ___ 花多少？先看 ___，再看 ___。',
        'topic': '养车成本', 'audience': '车主',
        'reasons': [{'point': '与日常有关', 'evidence': '每月养车'}],
        'tags': ['汽车', '提问'], 'gaps': '传播原因是推测',
    }


def analysis():
    doc=legacy_analysis()
    doc.update(analysis_version=2,account_field=radar.DEFAULTS['field'],hook='养车成本',
        opening_3s={'evidence':'养车成本','technique':'成本痛点','retention_reason':'与车主日常支出相关',
                    'limitation':'没有时间戳，仅按开头文字推测，无法确认真实前 3 秒'},
        reasons=[{'point':'实用价值可能促成收藏','evidence':'养车成本','is_hypothesis':True},
                 {'point':'成本话题可能引发车主共鸣','evidence':'养车成本','is_hypothesis':True}],
        takeaways=[{'point':'用具体开支切入','evidence':'养车成本','application':'汽车领域可以展示一张真实保养账单'}],
        avoid_copying=[{'point':'不能照搬账单金额','reason':'用车情境不同','alternative':'补充自己的真实账单'}],
        adapted_topics=[{'title':'保养账单里的可省项目','opening':'这几项保养真的都要做吗？',
                         'outline':['准备真实账单','核对保养手册','说明适用条件'],'source_mechanism':'成本痛点'},
                        {'title':'新手第一年的用车预算','opening':'买车后还有哪些支出？',
                         'outline':['列出开支类别','补充自己的记录'],'source_mechanism':'实用清单'}])
    return doc


def classified(rows):
    return {'items':[{'rank':r['rank'],'word':r['word'],'track':r.get('track','汽车'),
                      'related':r.get('related',True)} for r in rows]}


class RadarTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='baokuan-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        env = patch.dict(os.environ, BAOKUAN_DATA=str(self.root))
        env.start()
        self.addCleanup(env.stop)
        importlib.reload(radar)
        clock = patch.object(radar, 'now', return_value=datetime(2026, 11, 5, 12, tzinfo=radar.CST))
        clock.start()
        self.addCleanup(clock.stop)
        self.configured_data = radar.DATA
        # 旧版未实现环境变量时也不允许测试写入用户数据目录。
        paths = patch.multiple(radar, DATA=self.root, DY_STATE=self.root/'dy-account', MEDIA=self.root/'media')
        paths.start()
        self.addCleanup(paths.stop)
        for target in ('urllib.request.urlopen', 'urllib.request.build_opener', 'subprocess.run', 'socket.socket'):
            guard = patch(target, side_effect=AssertionError('测试禁止真实网络或外部进程'))
            guard.start()
            self.addCleanup(guard.stop)
        key = patch.object(radar.platforms, 'youtube_key', return_value='')
        key.start()
        self.addCleanup(key.stop)
        aliyun_key = patch.object(radar.aliyun, 'api_key', return_value='')
        aliyun_key.start()
        self.addCleanup(aliyun_key.stop)
        notice = patch.object(radar, 'notify')
        self.notify = notice.start()
        self.addCleanup(notice.stop)
        server.JOB.update(name=None, started=0, result='')
        self.db = radar.connect()
        self.addCleanup(self.db.close)
        self.day = radar.today()

    def keyword(self, word='车评'):
        self.db.execute('insert into keywords(day,word) values(?,?)', (self.day, word))
        self.db.commit()

    def ready_tick(self):
        self.keyword()
        radar.save_hot(self.db, self.day, [{'rank': 1, 'word': '新车上市', 'hot_value': 100}])
        self.db.execute('update hot set track=?,related=1,classification_key=?',('汽车',radar.hot_context(self.db)[2]))
        self.db.commit()
        radar.put(self.db, 'keywords_picked_day', self.day)
        cookies = radar.DY_STATE/'cookies/default.json'
        cookies.parent.mkdir(parents=True)
        cookies.write_text('{"cookies": []}')

    def video(self, ident='12345678901', **changes):
        item = dict(aweme_id=ident, url='https://www.douyin.com/video/'+ident,
                    desc='养车成本', author='汽车小号', followers=1000, likes=20000,
                    comments=100, favorites=200, shares=50,
                    published_at=(radar.now()-timedelta(days=1)).isoformat(),
                    cover='', play_url='https://example.invalid/video.mp4', duration=30)
        item.update(changes)
        return item

    def seed_video(self, ident='12345678901', **changes):
        row = dict(self.video(ident), day=self.day, keyword='车评', status='pending', ratio=20)
        row.update(changes)
        keys = ','.join(row)
        self.db.execute(f"insert into videos({keys}) values({','.join('?' for _ in row)})", list(row.values()))
        self.db.commit()
        return ident

    def request(self, method, path, body=None, raw=None, headers=None):
        handler = object.__new__(server.Handler)
        handler.path = path
        payload = raw if raw is not None else json.dumps(body if body is not None else {}).encode()
        handler.headers = {'Content-Length': str(len(payload)), **(headers or {})}
        handler.rfile = io.BytesIO(payload)
        result = {}
        def send(doc, status=200, kind=None):
            result.update(status=status, body=doc)
        handler.send = send
        getattr(handler, 'do_'+method)()
        return result['status'], result['body']

    def test_data_environment(self):
        self.assertEqual(self.configured_data, self.root)

    def test_pick_keywords_and_deduplicate(self):
        radar.put(self.db, 'fixed_keywords', [' 车评 ', '车评'])
        radar.put(self.db, 'keywords_per_day', 3)
        with patch.object(radar, 'codex', return_value={'keywords': [
            {'word': '车评', 'hot_word': '', 'reason': '重复'},
            {'word': '新车', 'hot_word': '新车上市', 'reason': '相关'},
            {'word': '养车', 'hot_word': '', 'reason': '成本'},
            {'word': '更多', 'hot_word': '', 'reason': '超额'},
        ]}) as mock:
            radar.pick_keywords(self.db, self.day, [{'rank': 1, 'word': '新车上市'}])
            radar.pick_keywords(self.db, self.day, [])
        rows = self.db.execute('select word from keywords order by rowid').fetchall()
        self.assertEqual([r[0] for r in rows], ['车评', '新车', '养车'])
        self.assertIn('汽车', mock.call_args.args[0])

    def test_search_threshold_ratio_duplicates_and_recency(self):
        self.keyword()
        radar.put(self.db, 'per_keyword', 2)
        best = self.video('12345678901', likes=10000, followers=100)
        items = [best, best, self.video('12345678902', likes=30000, followers=1000),
                 self.video('12345678903', likes=100000, followers=None),
                 self.video('12345678904', likes=9999, followers=1),
                 self.video('12345678905', likes=999999, published_at=(radar.now()-timedelta(days=8)).isoformat())]
        with patch.object(radar, 'dy', return_value={'ok': True, 'items': items}):
            self.assertEqual(radar.search_keyword(self.db, self.day, '车评'), 2)
        chosen = self.db.execute("select aweme_id from videos where status='pending' order by ratio desc").fetchall()
        self.assertEqual([r[0] for r in chosen], ['12345678901', '12345678902'])

    def test_done_video_not_selected_again(self):
        self.keyword()
        self.seed_video(status='done', saved=1, note='保留备注', analysis=json.dumps(analysis()))
        with patch.object(radar, 'dy', return_value={'ok': True, 'items': [self.video()]}):
            self.assertEqual(radar.search_keyword(self.db, self.day, '车评'), 0)
        row = self.db.execute('select * from videos').fetchone()
        self.assertEqual((row['status'], row['saved'], row['note']), ('done', 1, '保留备注'))

    def test_auth_errors_pause(self):
        for code in ('NEED_LOGIN', 'PLATFORM_BLOCKED', 'RATE_LIMITED'):
            with self.subTest(code=code), patch.object(radar, 'dy', return_value={'ok': False, 'error': {'code': code}}):
                self.db.execute('delete from keywords')
                self.db.commit()
                self.keyword()
                self.assertIsNone(radar.search_keyword(self.db, self.day, '车评'))
                self.assertEqual(radar.account(self.db)['status'], code)
                self.assertEqual(self.db.execute('select status from keywords').fetchone()[0], 'queued')

    def test_process_success_and_failure(self):
        ident = self.seed_video()
        with patch.object(radar, 'transcribe', return_value=('真实的模拟逐字稿', 'whisper')), patch.object(radar, 'codex', return_value=analysis()):
            self.assertTrue(radar.process(self.db, ident))
        row = self.db.execute('select * from videos').fetchone()
        self.assertEqual(row['status'], 'done')
        self.assertEqual(row['transcript'], '真实的模拟逐字稿')
        self.assertEqual(json.loads(row['tags']), ['汽车', '提问'])
        failed = self.seed_video('12345678902')
        with patch.object(radar, 'transcribe', return_value=('', 'none')), patch.object(radar, 'codex', side_effect=RuntimeError('模拟拆解失败')):
            self.assertFalse(radar.process(self.db, failed))
        self.assertEqual(self.db.execute('select status from videos where aweme_id=?', (failed,)).fetchone()[0], 'failed')

    def test_server_rejects_invalid_settings_atomically(self):
        for change in ({'per_keyword': 0}, {'keywords_per_day': 0}, {'auto': 'false'},
                       {'fixed_keywords': '车评'}, {'recent_days': 181}, {'active_start': 24},
                       {'min_likes': True}, {'search_gap_minutes': 19}, {'per_keyword': '1.5'}):
            with self.subTest(change=change):
                status, _ = self.request('POST', '/api/settings', {'field': '不能写入', **change})
                self.assertEqual(status, 400)
                self.assertEqual(radar.setting(self.db, 'field'), radar.DEFAULTS['field'])

    def test_server_save_note_tags_search(self):
        ident = self.seed_video()
        self.assertEqual(self.request('POST', '/api/save', {'id': ident, 'saved': True})[0], 200)
        self.request('POST', '/api/note', {'id': ident, 'tags': ['汽车', '汽车', ' 提问 '], 'note': '下次拍成本'})
        status, body = self.request('GET', '/api/tags')
        self.assertEqual((status, body['items']), (200, [('汽车', 1), ('提问', 1)]))
        _, body = self.request('GET', '/api/videos?view=library&q=成本&tag=汽车')
        self.assertEqual(len(body['items']), 1)
        self.assertEqual(self.request('POST', '/api/save', {'id': '不存在', 'saved': True})[0], 404)

    def test_login_clears_obsolete_block_state(self):
        self.keyword()
        self.db.execute("update keywords set error='NEED_LOGIN'")
        self.db.commit()
        radar.set_account(self.db, status='NEED_LOGIN', blocked_at=123, need_login_notified=self.day)
        with patch.object(radar, 'dy', return_value={'ok': True}):
            server.login_work()
        self.assertEqual(radar.account(self.db)['status'], 'ready')
        self.assertIsNone(self.db.execute('select error from keywords').fetchone()[0])

    def test_tick_retries_pick_after_hot_was_already_saved(self):
        radar.save_hot(self.db, self.day, [{'rank': 1, 'word': '新车上市', 'hot_value': 100}])
        with patch.object(radar, 'classify_hot', return_value=0), patch.object(radar, 'fetch_hot') as hot, patch.object(radar, 'codex', side_effect=[
            RuntimeError('模拟 Codex 暂时失败'),
            {'keywords': [{'word': '新车', 'hot_word': '新车上市', 'reason': '相关'}]},
        ]) as codex:
            self.assertIn('挑词失败', radar.tick(force=True))
            self.assertIn('关键词', radar.tick(force=True))
            radar.tick(force=True)
        self.assertEqual(codex.call_count, 2)
        hot.assert_not_called()
        self.assertEqual(radar.setting(self.db, 'keywords_picked_day'), self.day)
        self.assertEqual(self.db.execute('select count(*) from keywords').fetchone()[0], 2)
        self.assertEqual(self.notify.call_count, 1)

    def test_tick_login_and_verification_do_not_expire_automatically(self):
        self.ready_tick()
        for code in ('NEED_LOGIN', 'PLATFORM_BLOCKED'):
            with self.subTest(code=code), patch.object(radar, 'dy') as dy:
                radar.set_account(self.db, status=code, blocked_at=time.time()-3*86400)
                radar.tick(force=True)
                dy.assert_not_called()
                self.assertEqual(radar.account(self.db)['status'], code)

    def test_tick_rate_limit_pauses_then_resumes(self):
        self.ready_tick()
        radar.set_account(self.db, status='RATE_LIMITED', blocked_at=time.time()-3600)
        with patch.object(radar, 'dy', return_value={'ok': False, 'error': {'code': 'NO_PUBLIC_DATA'}}) as dy:
            self.assertIn('限流暂停', radar.tick(force=True))
            dy.assert_not_called()
            radar.set_account(self.db, blocked_at=time.time()-86401)
            radar.tick(force=True)
            dy.assert_called_once()
        self.assertEqual(radar.account(self.db)['status'], 'ready')

    def test_force_respects_search_gap_and_only_one_word(self):
        self.ready_tick()
        self.keyword('养车')
        radar.set_account(self.db, last_search_at=time.time())
        with patch.object(radar, 'dy', return_value={'ok': True, 'items': []}) as dy:
            self.assertIn('距离下次搜索', radar.tick(force=True))
            dy.assert_not_called()
            radar.set_account(self.db, last_search_at=time.time()-3601)
            radar.tick(force=True)
            dy.assert_called_once()
        self.assertEqual([r[0] for r in self.db.execute('select status from keywords order by rowid')], ['empty', 'queued'])

    def test_tick_lock_and_automatic_switch(self):
        with radar.single('tick') as acquired, patch.object(radar, 'fetch_hot') as hot:
            self.assertTrue(acquired)
            self.assertEqual(radar.tick(force=True), '上一轮还没跑完')
            hot.assert_not_called()
        radar.put(self.db, 'auto', False)
        with patch.object(radar, 'fetch_hot') as hot:
            self.assertEqual(radar.tick(), '不在运行时段')
            hot.assert_not_called()

    def test_tick_recovers_interrupted_processing(self):
        self.ready_tick()
        self.db.execute("update keywords set status='done'")
        self.db.commit()
        self.seed_video(status='processing', transcript='上次已转写', transcript_source='whisper')
        with patch.object(radar, 'codex', return_value=analysis()), patch.object(radar, 'transcribe') as transcribe:
            radar.tick(force=True)
        transcribe.assert_not_called()
        self.assertEqual(self.db.execute('select status from videos').fetchone()[0], 'done')

    def test_zero_results_completion_notice_only_once(self):
        self.ready_tick()
        with patch.object(radar, 'dy', return_value={'ok': False, 'error': {'code': 'NO_PUBLIC_DATA'}}):
            radar.tick(force=True)
            radar.tick(force=True)
        self.notify.assert_called_once()
        self.assertIn('完成 0 条', self.notify.call_args.args[0])

    def test_empty_search_clears_previous_keyword_error(self):
        self.keyword()
        self.db.execute("update keywords set error='TIMEOUT'")
        self.db.commit()
        with patch.object(radar, 'dy', return_value={'ok': False, 'error': {'code': 'NO_PUBLIC_DATA'}}):
            self.assertEqual(radar.search_keyword(self.db, self.day, '车评'), 0)
        row = self.db.execute('select * from keywords').fetchone()
        self.assertEqual((row['status'], row['found'], row['error']), ('empty', 0, None))

    def test_invalid_search_items_dates_and_unknown_ratio(self):
        self.keyword()
        items = [None, {}, self.video('恶意路径/../'), self.video('12345678901', likes=True),
                 self.video('12345678902', published_at='无效日期'),
                 self.video('12345678903', published_at=''),
                 self.video('12345678904', published_at=(radar.now()+timedelta(days=1)).isoformat()),
                 self.video('12345678905', followers=0),
                 self.video('12345678906', published_at=(radar.now()-timedelta(days=1)).replace(tzinfo=None).isoformat())]
        with patch.object(radar, 'dy', return_value={'ok': True, 'items': items}):
            self.assertEqual(radar.search_keyword(self.db, self.day, '车评'), 2)
        rows = self.db.execute("select aweme_id,ratio from videos where status='pending' order by ratio is null,ratio desc").fetchall()
        self.assertEqual([r['aweme_id'] for r in rows], ['12345678906', '12345678905'])
        self.assertIsNone(rows[-1]['ratio'])

    def test_malformed_search_response_is_retryable(self):
        self.keyword()
        with patch.object(radar, 'dy', return_value={'ok': True, 'items': None}):
            self.assertIsNone(radar.search_keyword(self.db, self.day, '车评'))
        row = self.db.execute('select * from keywords').fetchone()
        self.assertEqual((row['status'], row['error']), ('queued', 'UPSTREAM_BROKEN'))

    def test_process_skips_done_and_missing_video(self):
        ident = self.seed_video(status='done')
        with patch.object(radar, 'transcribe') as transcribe, patch.object(radar, 'codex') as codex:
            self.assertTrue(radar.process(self.db, ident))
            self.assertFalse(radar.process(self.db, '不存在'))
            transcribe.assert_not_called()
            codex.assert_not_called()

    def test_failed_process_retains_transcript_for_retry(self):
        ident = self.seed_video()
        with patch.object(radar, 'transcribe', return_value=('已保存的逐字稿', 'whisper')) as transcribe, patch.object(radar, 'codex', side_effect=[{}, analysis()]):
            self.assertFalse(radar.process(self.db, ident))
            self.assertTrue(radar.process(self.db, ident))
            transcribe.assert_called_once()

    def test_reanalyze_preserves_collection_and_user_notes(self):
        ident = self.seed_video(status='done', saved=1, tags='["自定义"]', note='保留', transcript='逐字稿')
        with patch.object(radar, 'codex', return_value=analysis()):
            self.assertEqual(server.reanalyze_work(ident), '拆完了')
        row = self.db.execute('select * from videos').fetchone()
        self.assertEqual((row['saved'], row['tags'], row['note']), (1, '["自定义"]', '保留'))

    def test_transcribe_download_mock_and_remove_video(self):
        self.seed_video()
        row = self.db.execute('select * from videos').fetchone()
        python = self.root/'模拟解释器'
        python.touch()
        def download(url, target):
            target.write_bytes('模拟视频'.encode())
        with patch.object(radar, 'WHISPER_PYTHON', python), patch.object(radar, 'download', side_effect=download), patch('subprocess.run', return_value=Mock(returncode=0, stdout='提示行\n'+json.dumps({'ok': True, 'text': ' 模拟转写 '}, ensure_ascii=False))):
            self.assertEqual(radar.transcribe(row), ('模拟转写', 'whisper'))
        self.assertEqual(list(radar.MEDIA.iterdir()), [])

    def test_transcribe_failure_also_removes_partial_video(self):
        self.seed_video()
        row = self.db.execute('select * from videos').fetchone()
        python = self.root/'模拟解释器'
        python.touch()
        def download(url, target):
            target.write_bytes('未完成的视频'.encode())
            raise OSError('模拟下载失败')
        with patch.object(radar, 'WHISPER_PYTHON', python), patch.object(radar, 'download', side_effect=download):
            self.assertEqual(radar.transcribe(row), ('', 'none'))
        self.assertEqual(list(radar.MEDIA.iterdir()), [])

    def test_transcribe_empty_or_failed_subprocess_is_not_whisper(self):
        self.seed_video()
        row = self.db.execute('select * from videos').fetchone()
        python = self.root/'模拟解释器'
        python.touch()
        for result in (Mock(returncode=0, stdout='{"ok":true,"text":""}'), Mock(returncode=1, stdout='{"ok":true,"text":"错误输出"}'), Mock(returncode=0, stdout='不正确的内容')):
            with self.subTest(result=result.stdout), patch.object(radar, 'WHISPER_PYTHON', python), patch.object(radar, 'download', side_effect=lambda u,p: p.write_bytes('模拟'.encode())), patch('subprocess.run', return_value=result):
                self.assertEqual(radar.transcribe(row), ('', 'none'))
                self.assertEqual(list(radar.MEDIA.iterdir()), [])

    def test_codex_wrapper_validates_and_cleans_output(self):
        def run(args, **kwargs):
            Path(args[args.index('-o')+1]).write_text(json.dumps(analysis(), ensure_ascii=False))
            return Mock(returncode=0, stderr='')
        with patch('subprocess.run', side_effect=run):
            self.assertEqual(radar.codex('模拟输入', 'analysis.json'), analysis())
        self.assertEqual(list((self.root/'codex-out').iterdir()), [])
        with patch('subprocess.run', return_value=Mock(returncode=1, stderr='模拟失败')):
            with self.assertRaises(RuntimeError):radar.codex('模拟输入', 'analysis.json')

    def test_dy_wrapper_handles_bad_output_and_timeout(self):
        python = self.root/'模拟解释器'
        python.touch()
        with patch.object(radar, 'DY_PYTHON', python):
            for output in ('日志\n{"ok":true,"items":[]}', 'null', '[]', '', '无法读取', '{"ok":false,"error":[]}', '{"ok":false}'):
                with self.subTest(output=output), patch('subprocess.run', return_value=Mock(returncode=0, stdout=output)):
                    self.assertEqual(radar.dy('search', '车评')['ok'], output.startswith('日志'))
            with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('模拟命令', 1)):
                self.assertEqual(radar.dy('search', '车评')['error']['code'], 'TIMEOUT')

    def test_login_cannot_bypass_rate_limit(self):
        radar.set_account(self.db, status='RATE_LIMITED', blocked_at=time.time())
        with patch.object(radar, 'dy', return_value={'ok': True}) as dy:
            server.login_work()
            server.challenge_work()
            dy.assert_called_once_with('login')
        self.assertEqual(radar.account(self.db)['status'], 'RATE_LIMITED')

    def test_challenge_failure_keeps_search_paused_and_success_clears_errors(self):
        self.keyword()
        self.db.execute("update keywords set error='PLATFORM_BLOCKED'")
        self.db.commit()
        with patch.object(radar, 'dy', return_value={'ok': False, 'error': {'code': 'CHALLENGE_CLOSED'}}):
            self.assertIn('继续暂停', server.challenge_work())
        self.assertEqual(radar.account(self.db)['status'], 'PLATFORM_BLOCKED')
        with patch.object(radar, 'dy', return_value={'ok': True}):
            server.challenge_work()
        self.assertEqual(radar.account(self.db)['status'], 'ready')
        self.assertIsNone(self.db.execute('select error from keywords').fetchone()[0])

    def test_server_valid_settings_and_limits(self):
        data = {'field': ' 汽车 ', 'fixed_keywords': ['车评', '车评', ' 养车 '],
                'keywords_per_day': '2', 'per_keyword': 10, 'recent_days': 180,
                'active_start': 0, 'active_end': 24, 'search_gap_minutes': '20', 'auto': False}
        status, body = self.request('POST', '/api/settings', data)
        self.assertEqual(status, 200)
        self.assertEqual(body['settings']['fixed_keywords'], ['车评', '养车'])
        self.assertEqual(radar.setting(self.db, 'field'), '汽车')
        self.assertIs(radar.setting(self.db, 'auto'), False)
        self.assertEqual(self.request('POST', '/api/settings', {'keywords_per_day': 1})[0], 400)

    def test_server_rejects_non_object_and_malformed_requests(self):
        for raw in (b'[]', b'null', b'42', b'{', b'\xff'):
            with self.subTest(raw=raw):
                self.assertEqual(self.request('POST', '/api/settings', raw=raw)[0], 400)
        for length in ('-1', '200001', '错误长度'):
            self.assertEqual(self.request('POST', '/api/settings', headers={'Content-Length': length})[0], 400)
        self.assertEqual(self.request('POST', '/api/run', headers={'Origin': 'https://example.invalid'})[0], 403)
        self.assertEqual(self.request('GET', '/不存在')[0], 404)

    def test_server_rejects_invalid_note_or_collection_values(self):
        ident = self.seed_video()
        for data in ({'tags': '汽车'}, {'tags': [1]}, {'note': '字'*5001}, {'note': []}):
            self.assertEqual(self.request('POST', '/api/note', {'id': ident, **data})[0], 400)
        self.assertEqual(self.request('POST', '/api/save', {'id': ident, 'saved': 'false'})[0], 400)
        self.request('POST', '/api/save', {'id': ident, 'saved': True})
        self.request('POST', '/api/save', {'id': ident, 'saved': False})
        self.assertIsNone(self.db.execute('select saved_at from videos').fetchone()[0])

    def test_server_state_history_and_unknown_ratio_sort(self):
        self.keyword()
        self.seed_video('12345678901', ratio=None, likes=100000)
        self.seed_video('12345678902', ratio=0, likes=0)
        self.seed_video('12345678903', ratio=10)
        self.seed_video('12345678904', ratio=1000, status='candidate')
        self.seed_video('12345678905', day='2026-11-04')
        _, body = self.request('GET', '/api/videos')
        self.assertEqual([v['aweme_id'] for v in body['items']], ['12345678903', '12345678902', '12345678901'])
        _, body = self.request('GET', '/api/state?day=2026-11-04')
        self.assertEqual(body['today_count'], 3)
        self.assertEqual(body['keywords'], [])
        self.assertEqual(body['today_keywords'][0]['word'], '车评')

    def test_busy_reanalyze_does_not_mutate_video(self):
        ident = self.seed_video(status='done')
        server.JOB['name'] = '扫码登录'
        status, body = self.request('POST', '/api/reanalyze', {'id': ident})
        self.assertEqual((status, body['ok']), (200, False))
        self.assertEqual(self.db.execute('select status from videos').fetchone()[0], 'done')

    def test_background_job_and_tick_share_lock(self):
        work = Mock(return_value='完成')
        # 同步运行线程的目标函数，仍使用真正的文件锁。
        def thread(*, target, daemon):
            return Mock(start=target)
        with radar.single('tick'), patch.object(server.threading, 'Thread', side_effect=thread):
            self.assertTrue(server.start_job('模拟任务', work))
        work.assert_not_called()
        self.assertIsNone(server.JOB['name'])
        self.assertIn('上一轮', server.JOB['result'])

    def test_worker_public_data_validation(self):
        self.assertIsNone(dy_worker.public_item({'aweme_id': '../恶意路径'}))
        doc = dy_worker.public_item({'aweme_id': '12345678901', 'create_time': True,
                                    'author': {'follower_count': -1},
                                    'statistics': {'digg_count': True},
                                    'video': {'duration': True, 'play_addr': {'url_list': 'https://错误格式'}}})
        self.assertIsNone(doc['followers'])
        self.assertIsNone(doc['likes'])
        self.assertIsNone(doc['duration'])
        self.assertEqual((doc['published_at'], doc['play_url']), ('', ''))
        self.assertEqual(dy_worker.feed_failure({'status_code': 429}), 'RATE_LIMITED')
        self.assertEqual(dy_worker.feed_failure({'search_nil_info': {'search_nil_type': 'verify_check'}}), 'PLATFORM_BLOCKED')
        self.assertEqual(dy_worker.feed_failure({'status_code': 8}), 'NEED_LOGIN')

    def test_worker_cookie_isolation_and_closed_challenge(self):
        cookie_file = self.root/'cookies/default.json'
        dy_worker.save_cookies(cookie_file, [{'domain': '.douyin.com', 'name': '小号', 'value': '模拟'},
                                           {'domain': '.example.invalid', 'name': '其他', 'value': '模拟'}])
        self.assertEqual(len(json.loads(cookie_file.read_text())['cookies']), 1)
        with patch.object(dy_worker.os, 'umask'), patch.object(dy_worker, 'isolate', return_value=cookie_file), patch.object(dy_worker, 'challenge_window', return_value=([{'domain': '.douyin.com'}], 'PLATFORM_BLOCKED')):
            result = dy_worker.run('challenge', '车评', 1, self.root, 7)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'PLATFORM_BLOCKED')

    def test_collection_search_includes_full_transcript(self):
        self.seed_video(saved=1, transcript='只有逐字稿里才有的句子')
        _, result = self.request('GET', '/api/videos?view=library&q=才有的句子')
        self.assertEqual(len(result['items']), 1)
        self.assertNotIn('transcript', result['items'][0])

    def test_analysis_does_not_overwrite_tags_edited_while_waiting(self):
        ident = self.seed_video(transcript='逐字稿')
        def codex(*args):
            self.db.execute('update videos set tags=? where aweme_id=?', ('["刚编辑的标签"]', ident))
            self.db.commit()
            return analysis()
        with patch.object(radar, 'codex', side_effect=codex):
            self.assertTrue(radar.process(self.db, ident))
        self.assertEqual(self.db.execute('select tags from videos').fetchone()[0], '["刚编辑的标签"]')

    def test_login_failure_and_missing_cookie_do_not_bypass_rate_limit(self):
        self.ready_tick()
        (radar.DY_STATE/'cookies/default.json').unlink()
        radar.set_account(self.db, status='RATE_LIMITED', blocked_at=time.time())
        with patch.object(radar, 'dy', return_value={'ok': False, 'error': {'code': 'TIMEOUT'}}) as dy:
            server.login_work()
            radar.tick(force=True)
            dy.assert_called_once_with('login')
        self.assertEqual(radar.account(self.db)['status'], 'RATE_LIMITED')

    def test_hot_data_normalization_and_transaction_rollback(self):
        body = {'data': {'word_list': [None, {'word': ' 新车 ', 'position': 1, 'hot_value': 10},
                                    {'word': '养车', 'position': 1, 'hot_value': True},
                                    {'word': ' ', 'position': 3}]}}
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(body).encode())):
            rows = radar.fetch_hot()
        self.assertEqual([r['rank'] for r in rows], [1, 2])
        self.assertEqual(rows[0]['word'], '新车')
        self.assertIsNone(rows[1]['hot_value'])
        radar.save_hot(self.db, self.day, rows)
        with self.assertRaises(sqlite3.IntegrityError):
            radar.save_hot(self.db, self.day, [rows[0], rows[0]])
        self.assertEqual(self.db.execute('select count(*) from hot').fetchone()[0], 2)

    def test_worker_keeps_verification_and_rate_limit_errors(self):
        state = {'code': 'PLATFORM_BLOCKED'}
        dy_worker.record_failure(state, 'UPSTREAM_BROKEN')
        self.assertEqual(state['code'], 'PLATFORM_BLOCKED')
        dy_worker.record_failure(state, 'RATE_LIMITED')
        dy_worker.record_failure(state, 'PLATFORM_BLOCKED')
        self.assertEqual(state['code'], 'RATE_LIMITED')

    def test_hot_legacy_database_migrates_idempotently_and_classifies(self):
        legacy=self.root/'旧库';legacy.mkdir()
        with sqlite3.connect(legacy/'radar.sqlite3') as db:
            db.executescript('create table hot(day text,rank int,word text,hot_value int,primary key(day,rank));'
                             'create table settings(key text primary key,value text not null);')
            db.execute('insert into hot values(?,?,?,?)',(self.day,1,'新车上市',0))
            db.execute('insert into settings values(?,?)',('field',json.dumps('汽车')))
        with patch.object(radar,'DATA',legacy):
            for i in range(2):
                with radar.connect() as db:
                    row=dict(db.execute('select * from hot').fetchone())
                    self.assertEqual((row['word'],row['hot_value']),('新车上市',0))
                    if i==0:
                        self.assertEqual((row['track'],row['related']),('未分类',None))
                        with patch.object(radar,'codex',return_value=classified([{'rank':row['rank'],'word':row['word']}])):
                            self.assertEqual(radar.classify_hot(db,self.day),1)
                    else:self.assertEqual((row['track'],row['related']),('汽车',1))
                db.close()

    def test_hot_classifies_batch_once_and_preserves_on_same_refresh(self):
        rows=[{'rank':1,'word':'新车上市','hot_value':0},
              {'rank':2,'word':'芯片发布','hot_value':None,'track':'科技数码','related':False},
              {'rank':3,'word':'新车上市','hot_value':99}]
        radar.save_hot(self.db,self.day,rows)
        with patch.object(radar,'codex',return_value=classified(rows)) as codex:
            self.assertEqual(radar.classify_hot(self.db,self.day),3)
            self.assertEqual(radar.classify_hot(self.db,self.day),0)
        codex.assert_called_once()
        self.assertEqual(codex.call_args.args[1],'hot_classification.json')
        self.assertIn('汽车、车评讲解',codex.call_args.args[0])
        radar.save_hot(self.db,self.day,rows)
        self.assertEqual([r['track'] for r in radar.hot_items(self.db,self.day)],['汽车','科技数码','汽车'])
        rows[0]['word']='新的热词'
        radar.save_hot(self.db,self.day,rows)
        self.assertEqual(radar.hot_items(self.db,self.day)[0]['track'],'未分类')
        self.assertIsNone(radar.hot_items(self.db,self.day)[0]['related'])

    def test_hot_invalid_classification_is_atomic_and_retryable(self):
        rows=[{'rank':1,'word':'新车上市','hot_value':1},{'rank':2,'word':'养车','hot_value':2}]
        radar.save_hot(self.db,self.day,rows)
        valid=classified(rows)
        bad=[{}, {'items':[]}, {'items':valid['items'][:1]}, {'items':valid['items']*2}]
        for change in ({'rank':True},{'related':'false'},{'related':0},{'track':'不存在'},
                       {'word':'未提供的词'},{'rank':999},{'word':''}):
            bad.append({'items':[dict(valid['items'][0],**change),valid['items'][1]]})
        for doc in bad:
            with self.subTest(doc=doc),patch.object(radar,'codex',return_value=doc):
                self.assertIn('分类失败',radar.try_classify_hot(self.db,self.day))
                self.assertEqual([r['track'] for r in radar.hot_items(self.db,self.day)],['未分类']*2)
                self.assertEqual(self.request('GET','/api/hot')[1]['counts']['all'],2)
        with patch.object(radar,'codex',return_value=valid):
            self.assertEqual(radar.classify_hot(self.db,self.day),2)

    def test_tick_classification_failure_keeps_hot_and_pick_then_retries(self):
        rows=[{'rank':1,'word':'新车上市','hot_value':1}]
        calls=[]
        def codex(prompt,schema):
            calls.append(schema)
            if schema=='hot_classification.json':
                if calls.count(schema)==1:raise RuntimeError('模拟分类失败')
                return classified(rows)
            return {'keywords':[{'word':'新车','hot_word':'新车上市','reason':'相关'}]}
        with patch.object(radar,'fetch_hot',return_value=rows) as fetch,patch.object(radar,'codex',side_effect=codex):
            self.assertIn('分类失败',radar.tick(force=True))
            self.assertEqual(radar.hot_items(self.db,self.day)[0]['track'],'未分类')
            self.assertEqual(self.db.execute('select count(*) from keywords').fetchone()[0],2)
            self.assertIn('已补分类 1 条',radar.tick(force=True))
            radar.tick(force=True)
        fetch.assert_called_once()
        self.assertEqual(calls,['hot_classification.json','pick.json','hot_classification.json'])

    def test_hot_filters_counts_and_empty_history(self):
        rows=[{'rank':1,'word':'新车','hot_value':0},
              {'rank':2,'word':'芯片','hot_value':2,'track':'科技数码','related':False},
              {'rank':3,'word':'汽车芯片','hot_value':3,'track':'科技数码'}]
        radar.save_hot(self.db,self.day,rows)
        with patch.object(radar,'codex',return_value=classified(rows)):radar.classify_hot(self.db,self.day)
        radar.save_hot(self.db,self.day,rows+[{'rank':4,'word':'未分类词','hot_value':None}])
        _,all_items=self.request('GET','/api/hot')
        for query,expected in [('&track=汽车',[1]),('&track=科技数码',[2,3]),('&related=1',[1,3]),
                               ('&track=科技数码&related=1',[3]),('&track=未分类',[4]),('&track=美食',[])]:
            status,body=self.request('GET','/api/hot?day='+self.day+query)
            self.assertEqual(status,200)
            self.assertEqual([r['rank'] for r in body['items']],expected)
            self.assertEqual(body['counts'],all_items['counts'])
        self.assertEqual(all_items['counts']['all'],4)
        self.assertEqual(sum(all_items['counts']['tracks'].values()),4)
        self.assertEqual(all_items['counts']['related'],2)
        self.assertEqual(self.request('GET','/api/hot?day=2026-11-04')[1]['items'],[])
        self.assertEqual(self.request('GET','/api/hot?track=不存在')[0],400)
        self.assertEqual(self.request('GET',"/api/hot?track='+OR+1=1--")[0],400)
        self.assertEqual(self.request('GET','/api/hot?related=true')[0],400)

    def test_hot_field_and_config_changes_invalidate_and_retry(self):
        rows=[{'rank':1,'word':'新车','hot_value':1}]
        radar.save_hot(self.db,self.day,rows)
        with patch.object(radar,'codex',return_value=classified(rows)):radar.classify_hot(self.db,self.day)
        self.request('POST','/api/settings',{'field':'教育'})
        body=self.request('GET','/api/hot?related=1')[1]
        self.assertEqual((body['items'],body['counts']['tracks']['未分类']),([],1))
        with patch.object(radar,'codex',return_value=classified([dict(rows[0],related=False)])):
            self.assertEqual(radar.classify_hot(self.db,self.day),1)
        self.assertEqual(radar.hot_items(self.db,self.day)[0]['related'],False)
        with patch.object(radar,'hot_tracks',return_value=['教育','其他']):
            self.assertEqual(radar.hot_items(self.db,self.day)[0]['track'],'未分类')
            with patch.object(radar,'codex',return_value=classified([dict(rows[0],track='其他',related=False)])):
                radar.classify_hot(self.db,self.day)
            self.assertEqual(self.request('GET','/api/hot')[1]['tracks'],['教育','其他'])

    def test_hot_context_changed_during_classification_does_not_write_stale_result(self):
        rows=[{'rank':1,'word':'新车','hot_value':1}]
        radar.save_hot(self.db,self.day,rows)
        def codex(*args):
            radar.put(self.db,'field','教育')
            return classified(rows)
        with patch.object(radar,'codex',side_effect=codex):
            self.assertIn('分类失败',radar.try_classify_hot(self.db,self.day))
        self.assertEqual(self.db.execute('select classification_key from hot').fetchone()[0],'')

    def test_hot_retries_only_pending_and_keeps_valid_classification_on_failure(self):
        rows=[{'rank':1,'word':'新车','hot_value':1}]
        radar.save_hot(self.db,self.day,rows)
        with patch.object(radar,'codex',return_value=classified(rows)):radar.classify_hot(self.db,self.day)
        radar.save_hot(self.db,self.day,rows+[{'rank':2,'word':'待判断','hot_value':2}])
        with patch.object(radar,'codex',side_effect=RuntimeError('暂时失败')):
            self.assertIn('分类失败',radar.try_classify_hot(self.db,self.day))
        self.assertEqual([r['track'] for r in radar.hot_items(self.db,self.day)],['汽车','未分类'])
        with patch.object(radar,'codex',return_value=classified([{'rank':2,'word':'待判断'}])) as codex:
            self.assertEqual(radar.classify_hot(self.db,self.day),1)
        self.assertNotIn('"word": "新车"',codex.call_args.args[0])
        with patch.object(radar,'codex') as codex:
            self.assertEqual(radar.classify_hot(self.db,'2026-11-04'),0);codex.assert_not_called()

    def test_classify_hot_job_only_classifies_and_respects_busy_lock(self):
        radar.save_hot(self.db,self.day,[{'rank':1,'word':'新车','hot_value':1}])
        def thread(*,target,daemon):return Mock(start=target)
        with patch.object(server.threading,'Thread',side_effect=thread),patch.object(radar,'codex',return_value=classified([{'rank':1,'word':'新车'}])) as codex,patch.object(radar,'dy') as dy,patch.object(radar,'fetch_hot') as fetch:
            self.assertTrue(self.request('POST','/api/classify-hot',{})[1]['ok'])
            codex.assert_called_once();dy.assert_not_called();fetch.assert_not_called()
        server.JOB['name']='正在拆解'
        self.assertFalse(self.request('POST','/api/classify-hot',{})[1]['ok'])

    def test_analysis_schema_enforces_new_fields_and_boundaries(self):
        self.assertEqual(radar.validate_result(analysis(),'analysis.json')['analysis_version'],2)
        with self.assertRaises(ValueError):radar.validate_result(legacy_analysis(),'analysis.json')
        invalid=[]
        for key in ('opening_3s','takeaways','avoid_copying','adapted_topics','account_field'):
            doc=analysis();doc.pop(key);invalid.append(doc)
        for change in ({'adapted_topics':[]},{'adapted_topics':analysis()['adapted_topics'][:1]},
                       {'adapted_topics':analysis()['adapted_topics']*2},{'takeaways':[]},
                       {'account_field':' '},{'analysis_version':True},{'analysis_version':1}):
            invalid.append(dict(analysis(),**change))
        for change in ({'is_hypothesis':False},{'is_hypothesis':'true'},{'evidence':''},{'evidence':12}):
            doc=analysis();doc['reasons'][0].update(change);invalid.append(doc)
        for doc in invalid:
            with self.subTest(doc=doc),self.assertRaises(ValueError):radar.validate_result(doc,'analysis.json')

    def test_analysis_uses_current_field_and_rejects_fabricated_evidence(self):
        self.seed_video(transcript='养车成本先看保养账单',transcript_source='whisper')
        row=self.db.execute('select * from videos').fetchone()
        radar.put(self.db,'field','生活、省钱')
        doc=analysis();doc['account_field']='生活、省钱'
        with patch.object(radar,'codex',return_value=doc) as codex:
            radar.analyze(self.db,row)
        prompt=codex.call_args.args[0]
        for text in ('我的账号领域：生活、省钱','反事实','没有视频画面','单样本','时间戳','投流'):
            self.assertIn(text,prompt)
        doc['reasons'][0]['evidence']='材料中没有的证据'
        with patch.object(radar,'codex',return_value=doc),self.assertRaisesRegex(ValueError,'原文证据'):
            radar.analyze(self.db,row)
        doc=analysis()
        with patch.object(radar,'codex',return_value=doc),self.assertRaisesRegex(ValueError,'领域'):
            radar.analyze(self.db,row)

    def test_old_analysis_can_be_read_searched_and_reanalyzed(self):
        old=legacy_analysis()
        ident=self.seed_video(status='done',analysis=json.dumps(old),saved=1,tags='["自定义"]',note='保留备注',transcript='养车成本')
        self.assertEqual(self.request('GET','/api/video?id='+ident)[1]['analysis'],old)
        self.assertEqual(len(self.request('GET','/api/videos?view=library&q=养车')[1]['items']),1)
        with patch.object(radar,'codex',return_value=analysis()),patch.object(radar,'transcribe') as transcribe:
            self.assertEqual(server.reanalyze_work(ident),'拆完了');transcribe.assert_not_called()
        result=self.request('GET','/api/video?id='+ident)[1]
        self.assertEqual(result['analysis']['analysis_version'],2)
        self.assertEqual((result['saved'],result['note'],result['tags']),(1,'保留备注',['自定义']))
        with patch.object(radar,'codex',return_value=old):
            self.assertIn('拆解失败',server.reanalyze_work(ident))
        self.assertEqual(self.request('GET','/api/video?id='+ident)[1]['analysis']['analysis_version'],2)


if __name__ == '__main__':
    unittest.main()
