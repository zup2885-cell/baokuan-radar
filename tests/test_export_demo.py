"""公开快照测试：只在临时库里写入，正式数据库不会被连接。"""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import build_demo
import export_demo


class DemoExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # 包含需要 URI 转义的字符，避免数据库路径被解析成查询参数。
        self.path = Path(self.temp.name) / '快照 #? readonly.sqlite3'
        self.transcript = '这是公开摘录的开头。' * 10 + '全文结尾绝对不能导出'
        self.tracks = json.loads((export_demo.ROOT / 'config/hot_tracks.json').read_text())
        key = hashlib.sha256(json.dumps(['汽车', self.tracks], ensure_ascii=False).encode()).hexdigest()
        with sqlite3.connect(self.path) as db:
            db.executescript('''
                CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE hot(day TEXT, rank INT, word TEXT, hot_value INT,
                                 track TEXT, related INT, classification_key TEXT);
                CREATE TABLE keywords(day TEXT, word TEXT, status TEXT, found INT,
                                      hot_word TEXT, reason TEXT, error TEXT);
                CREATE TABLE log(at TEXT, message TEXT);
                CREATE TABLE videos(aweme_id TEXT, day TEXT, desc TEXT, author TEXT, keyword TEXT,
                    published_at TEXT, followers INT, likes INT, comments INT, favorites INT,
                    shares INT, ratio REAL, url TEXT, status TEXT, saved INT, tags TEXT,
                    analysis TEXT, transcript TEXT, play_url TEXT, cover TEXT, note TEXT);
            ''')
            db.executemany('INSERT INTO settings VALUES(?,?)', [
                ('field', '"汽车"'), ('dy_account', '{"logged_in":true,"token":"secret-login"}'),
                ('media', '"/Users/private/media"')])
            db.executemany('INSERT INTO hot VALUES(?,?,?,?,?,?,?)', [
                ('2026-11-05', 1, '车评新热词', 0, '汽车', 1, key),
                ('2026-11-05', 2, '过期分类', None, '美食', 0, 'old'),
                ('2026-11-04', 1, '昨天热词', 300, '汽车', 0, key)])
            db.executemany('INSERT INTO keywords VALUES(?,?,?,?,?,?,?)', [
                ('2026-11-05', '车评', 'done', 0, '车评新热词', '/Users/private/reason', 'secret-error'),
                ('2026-11-05', '保养', 'queued', None, '', '', ''),
                ('2026-11-04', '昨日词', 'empty', 0, '', '', '')])
            db.execute('INSERT INTO log VALUES(?,?)', ('today', 'secret-log /Users/private'))
            analysis = {'summary': '真实拆解 /Users/private/work.txt', 'template': '可复制模板',
                        'hook': self.transcript, 'note': 'secret-note', 'play_url': 'secret-download',
                        'opening_3s': {'evidence': '证据', 'technique': '提问', 'cover': 'secret-cover'},
                        'reasons': [{'point': 'file:///private/tmp/secret.txt', 'evidence': '证据',
                                     'is_hypothesis': True, 'transcript': self.transcript}],
                        'gaps': r'C:\Users\private\work.txt 及 ~/Library/private.txt 与 /tmp/private.txt'}
            for ident, day, status, saved, ratio in [('101', '2026-11-05', 'done', 1, 2),
                    ('102', '2026-11-04', 'done', 1, None), ('103', '2026-11-05', 'failed', 1, 5),
                    ('104', '2026-11-05', 'processing', 0, 5), ('105', '2026-11-05', 'candidate', 0, 5)]:
                db.execute('INSERT INTO videos VALUES(' + ','.join('?' for _ in range(21)) + ')',
                           (ident, day, '标题', '作者', '车评', day, None, 0, 1, 2, 3, ratio,
                            'file:///Users/private/source', status, saved, '["汽车","汽车","<标签>"]',
                            json.dumps(analysis), self.transcript, 'secret-download', 'secret-cover', 'secret-note'))

    def test_public_allowlist_and_excerpt(self):
        doc = export_demo.export_snapshot(self.path)
        self.assertEqual(doc['snapshot_date'], '2026-11-05')
        self.assertEqual(doc['field'], '汽车')
        self.assertEqual(doc['tracks'], self.tracks)
        self.assertEqual([v['aweme_id'] for v in doc['videos']], ['101', '102'])
        video = doc['videos'][0]
        self.assertEqual(video['transcript_excerpt'], self.transcript[:80])
        self.assertLessEqual(len(video['transcript_excerpt']), 80)
        self.assertEqual(video['url'], 'https://www.douyin.com/video/101')
        self.assertEqual(video['likes'], 0)
        self.assertIsNone(video['followers'])
        self.assertTrue(video['saved'])
        self.assertEqual(video['tags'], ['汽车', '<标签>'])
        text = json.dumps(doc, ensure_ascii=False)
        for secret in [self.transcript, '全文结尾绝对不能导出', 'secret-download', 'secret-cover',
                       'secret-note', 'secret-login', 'secret-log', 'secret-error', '/Users/',
                       'file://', '/private/', '/tmp/', 'C:\\Users', '~/Library']:
            self.assertNotIn(secret, text)
        def check_keys(value):
            if isinstance(value, dict):
                self.assertTrue({'transcript', 'play_url', 'cover', 'note', 'logs', 'account', 'error'}.isdisjoint(value))
                for child in value.values():
                    check_keys(child)
            elif isinstance(value, list):
                for child in value:
                    check_keys(child)
        check_keys(doc)

    def test_date_scope_hot_counts_and_classification(self):
        doc = export_demo.export_snapshot(self.path)
        self.assertEqual(len(doc['keywords']), 2)
        self.assertEqual(doc['keywords'][0]['found'], 0)
        self.assertEqual(doc['hot'][0]['hot_value'], 0)
        self.assertIs(doc['hot'][0]['related'], True)
        self.assertEqual(doc['hot'][1]['track'], '未分类')
        self.assertIsNone(doc['hot'][1]['related'])
        yesterday = export_demo.export_snapshot(self.path, '2026-11-04')
        self.assertEqual([v['aweme_id'] for v in yesterday['videos']], ['102'])
        self.assertEqual(yesterday['keywords'][0]['word'], '昨日词')
        self.assertEqual(yesterday['hot'][0]['word'], '昨天热词')
        self.assertIs(yesterday['hot'][0]['related'], False)
        for invalid in ['2026-02-30', '2026-11-06', "2026-11-05' OR 1=1", '20261105']:
            with self.assertRaises(ValueError):
                export_demo.export_snapshot(self.path, invalid)

    def test_connection_is_readonly_and_database_unchanged(self):
        before = self.path.read_bytes()
        connect = sqlite3.connect
        calls = []
        def readonly(database, **kwargs):
            calls.append(database)
            self.assertTrue(database.endswith('?mode=ro'))
            self.assertTrue(kwargs['uri'])
            db = connect(database, **kwargs)
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("UPDATE settings SET value='changed'")
            db.rollback()
            return db
        with patch('export_demo.sqlite3.connect', side_effect=readonly):
            export_demo.export_snapshot(self.path)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.path.read_bytes(), before)
        missing = self.path.parent / 'missing.sqlite3'
        with self.assertRaises(sqlite3.OperationalError):
            export_demo.export_snapshot(missing)
        self.assertFalse(missing.exists())

    def test_legacy_hot_does_not_trigger_migration(self):
        with sqlite3.connect(self.path) as db:
            db.executescript('DROP TABLE hot; CREATE TABLE hot(day TEXT, rank INT, word TEXT, hot_value INT);'
                             "INSERT INTO hot VALUES('2026-11-05',1,'旧库',20);")
        before = self.path.read_bytes()
        hot = export_demo.export_snapshot(self.path)['hot'][0]
        self.assertEqual(hot['track'], '未分类')
        self.assertIsNone(hot['related'])
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_json_and_missing_material(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE videos SET analysis='broken',tags='{}',transcript=NULL,ratio=NULL")
        v = export_demo.export_snapshot(self.path)['videos'][0]
        self.assertIsNone(v['analysis'])
        self.assertEqual(v['tags'], [])
        self.assertEqual(v['transcript_excerpt'], '')
        self.assertIsNone(v['ratio'])

    def test_paths_and_excerpt_boundaries(self):
        for value in ['/workspace/nested/file.txt', './media/video.mp4', '../notes/private.md',
                      '/自定义文件夹/文稿.txt', r'\\server\share\private.txt']:
            self.assertEqual(export_demo.public_text(value), '［本机路径已隐藏］')
        link = '原视频 https://www.douyin.com/video/101 ，点赞 ÷ 粉丝'
        self.assertEqual(export_demo.public_text(link), link)
        for text in ['短摘录', '字' * 80, '字' * 81, '🚗' * 81, '开头 /workspace/secret.txt ' + '后续' * 80]:
            with sqlite3.connect(self.path) as db:
                db.execute('UPDATE videos SET transcript=?', (text,))
            excerpt = export_demo.export_snapshot(self.path)['videos'][0]['transcript_excerpt']
            self.assertEqual(excerpt, export_demo.public_text(text[:80])[:80])
            self.assertLessEqual(len(excerpt), 80)

    def test_empty_database_reports_no_snapshot(self):
        with sqlite3.connect(self.path) as db:
            for table in ['hot', 'keywords', 'videos']:
                db.execute('DELETE FROM ' + table)
        with self.assertRaisesRegex(ValueError, '没有可导出'):
            export_demo.export_snapshot(self.path)

    def test_cli_output_and_protected_database(self):
        output = Path(self.temp.name) / 'docs/data.json'
        result = subprocess.run([sys.executable, str(export_demo.ROOT / 'export_demo.py'), '--db', str(self.path),
                                 '--day', '2026-11-04', '--output', str(output)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(output.read_text())['snapshot_date'], '2026-11-04')
        for suffix in ('', '-wal', '-shm'):
            result = subprocess.run([sys.executable, str(export_demo.ROOT / 'export_demo.py'), '--db', str(self.path),
                                     '--output', str(self.path) + suffix], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('不能覆盖', result.stderr)

    def test_generated_page_matches_source(self):
        generated = (export_demo.ROOT / 'docs/index.html').read_text(encoding='utf-8')
        self.assertEqual(generated, build_demo.build_html())
        self.assertIn('window.RADAR_STATIC=true;', generated)
        self.assertIn('data-radar-static', generated)
        self.assertTrue((export_demo.ROOT / 'docs/.nojekyll').is_file())

    def test_saved_unanalyzed_reference_and_public_thumbnail(self):
        thumbnail='https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg'
        with sqlite3.connect(self.path) as db:
            db.execute("ALTER TABLE videos ADD COLUMN platform TEXT DEFAULT 'douyin'")
            db.execute("UPDATE videos SET aweme_id='youtube:abcdefghijk',platform='youtube',status='imported',day='',analysis=NULL,transcript='',cover=? WHERE aweme_id='101'",(thumbnail,))
            db.execute("UPDATE videos SET status='imported',saved=0 WHERE aweme_id='104'")
        rows=export_demo.export_snapshot(self.path)['videos']
        imported=next(v for v in rows if v['aweme_id']=='youtube:abcdefghijk')
        self.assertEqual(imported['status'],'imported')
        self.assertEqual(imported['thumbnail'],thumbnail)
        self.assertIsNone(imported['analysis'])
        self.assertEqual(imported['url'],'https://www.youtube.com/watch?v=abcdefghijk')
        self.assertFalse({'103','104','105'}&{v['aweme_id'] for v in rows})
        for bad in [thumbnail+'?token=secret',thumbnail+'#fragment',thumbnail.replace('https:','http:'),
                    thumbnail.replace('i.ytimg.com','i.ytimg.com.evil.example'),
                    thumbnail.replace('i.ytimg.com','user:secret@i.ytimg.com'),
                    thumbnail.replace('i.ytimg.com','i.ytimg.com:invalid'),
                    thumbnail.replace('abcdefghijk','wrongwrong1'),'/Users/private/photo.jpg']:
            self.assertEqual(export_demo.public_thumbnail('youtube','abcdefghijk',bad),'')

    def test_snapshot_content_changes_cache_version(self):
        root=Path(self.temp.name)/'site'
        (root/'ui').mkdir(parents=True);(root/'docs').mkdir()
        (root/'ui/index.html').write_text('<html lang="zh-CN"><script>test</script></html>')
        (root/'docs/data.json').write_text('{"saved":1}')
        with patch('build_demo.ROOT',root):
            first=build_demo.build_html()
            (root/'docs/data.json').write_text('{"saved":312}')
            second=build_demo.build_html()
        self.assertNotEqual(first,second)
        self.assertIn(hashlib.sha256(b'{"saved":312}').hexdigest()[:16],second)


if __name__ == '__main__':
    unittest.main()
