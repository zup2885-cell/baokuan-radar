"""数据库迁移回归：只用临时旧库，沿用禁止真实网络和外部进程的夹具。"""
from contextlib import closing
import json
import sqlite3
import unittest
from unittest.mock import patch

import radar
import test_radar as original


def primary_key(db,table):
    info=db.execute(f'pragma table_info({table})').fetchall()
    return tuple(r['name'] for r in sorted(info,key=lambda r:r['pk']) if r['pk'])


class MigrationTest(unittest.TestCase):
    setUp=original.RadarTest.setUp

    def legacy_analyses(self,name='旧分析库'):
        folder=self.root/name;folder.mkdir()
        rows=[('12345678901','codex','codex',json.dumps(original.legacy_analysis(),ensure_ascii=False),'2026-11-05'),
              ('12345678901','qwen','qwen-plus','历史千问分析：保留原文、空格与换行。\n',None)]
        with closing(sqlite3.connect(folder/'radar.sqlite3')) as db,db:
            db.execute('''create table video_analyses(aweme_id text, provider text, model text,
                analysis text not null, updated_at text, primary key(aweme_id,provider))''')
            db.executemany('insert into video_analyses values(?,?,?,?,?)',rows)
        return folder,rows

    def test_legacy_analyses_keep_history_and_allow_multiple_models(self):
        folder,history=self.legacy_analyses()
        with patch.object(radar,'DATA',folder),closing(radar.connect()) as db:
            self.assertEqual(primary_key(db,'video_analyses'),('aweme_id','provider','model'))
            self.assertEqual([tuple(r) for r in db.execute('select * from video_analyses order by rowid')],history)
            version=db.execute('pragma schema_version').fetchone()[0]
            db.execute('''insert into videos(aweme_id,desc,transcript,status,analysis,saved,tags,note,updated_at)
                values(?,?,?,'done',?,1,?,?,?)''',
                (history[0][0],'养车成本','养车成本',history[0][3],'["自定义"]','保留备注',history[0][4]))
            db.commit()
            # 经过实际 process() 写入路径；模型输出在本地模拟，不调用任何模型服务。
            expected={('codex','codex'):(history[0][3],history[0][4])}
            for model,summary in [('qwen-plus','千问的新分析'),('qwen3-max','另一模型的分析'),('qwen3-max','同一模型的最新分析')]:
                doc=original.analysis();doc['summary']=summary
                with patch.object(radar,'analysis_choice',return_value=('qwen',model)),patch.object(radar,'analyze',return_value=doc):
                    self.assertTrue(radar.process(db,history[0][0],force=True),db.execute('select error from videos').fetchone()[0])
                expected[('qwen',model)]=(json.dumps(doc,ensure_ascii=False),radar.now().isoformat(timespec='seconds'))
            self.assertEqual({(r['provider'],r['model']):(r['analysis'],r['updated_at'])
                              for r in db.execute('select * from video_analyses')},expected)
            self.assertEqual(tuple(db.execute('select saved,tags,note,status from videos').fetchone()),
                             (1,'["自定义"]','保留备注','done'))
            records=[tuple(r) for r in db.execute('select * from video_analyses order by rowid')]
        # 再次连接不重建表、不覆盖各模型的历史内容，也不留下迁移临时表。
        with patch.object(radar,'DATA',folder),closing(radar.connect()) as db:
            self.assertEqual(primary_key(db,'video_analyses'),('aweme_id','provider','model'))
            self.assertEqual(db.execute('pragma schema_version').fetchone()[0],version)
            self.assertEqual([tuple(r) for r in db.execute('select * from video_analyses order by rowid')],records)
            self.assertEqual(db.execute("select name from sqlite_master where name like '%_multi'").fetchall(),[])

    def test_other_upsert_targets_and_incomplete_platform_primary_keys(self):
        folder=self.root/'旧约束库';folder.mkdir()
        with closing(sqlite3.connect(folder/'radar.sqlite3')) as db,db:
            # settings / videos 保留原版主键；热榜 / 关键词模拟含 platform 但仍不完整的主键。
            for table,key in {'settings':'key','videos':'aweme_id','hot':'platform,day','keywords':'platform,day'}.items():
                info=[r for r in self.db.execute(f'pragma table_info({table})')
                      if table!='videos' or r['name'] not in ('platform','views','coins','analysis_provider','analysis_model')]
                definitions=[]
                for r in info:
                    definition=f'"{r["name"]}" {r["type"]}'
                    if r['notnull']:definition+=' not null'
                    if r['dflt_value'] is not None:definition+=' default '+r['dflt_value']
                    definitions.append(definition)
                db.execute(f'create table {table}('+','.join(definitions)+f',primary key({key}))')
            db.execute('insert into settings values(?,?)',('auto','false'))
            db.execute('insert into videos(aweme_id,saved,note) values(?,1,?)',('12345678901','原备注'))
            db.execute('insert into hot(day,rank,word,hot_value) values(?,?,?,?)',(self.day,1,'旧热词',0))
            db.execute('insert into keywords(day,word,status,found,error) values(?,?,?,?,?)',
                       (self.day,'车评','queued',0,'原错误'))
        with patch.object(radar,'DATA',folder),closing(radar.connect()) as db:
            for table,key in {'settings':('key',),'videos':('aweme_id',),'hot':('platform','day','rank'),
                              'keywords':('platform','day','word')}.items():
                with self.subTest(table=table):self.assertEqual(primary_key(db,table),key)
            self.assertIs(radar.setting(db,'auto'),False)
            self.assertEqual(tuple(db.execute('select saved,note from videos').fetchone()),(1,'原备注'))
            self.assertEqual(tuple(db.execute('select word,hot_value from hot').fetchone()),('旧热词',0))
            self.assertEqual(tuple(db.execute('select word,status,found,error from keywords').fetchone()),
                             ('车评','queued',0,'原错误'))
            # 实际编译其余所有 ON CONFLICT 目标，既覆盖单列旧主键，也覆盖迁移后的复合主键。
            for sql,args in [
                ('insert into settings values(?,?) on conflict(key) do update set value=excluded.value',('auto','true')),
                ('insert into videos(aweme_id) values(?) on conflict(aweme_id) do update set aweme_id=excluded.aweme_id',('12345678901',)),
                ('insert into keywords(platform,day,word) values(?,?,?) on conflict(platform,day,word) do update set word=excluded.word',('douyin',self.day,'车评')),
            ]:
                with self.subTest(sql=sql):db.execute('explain '+sql,args).fetchall()
            radar.record_public_search(db,self.day,'车评','douyin',1)
            radar.record_public_search(db,self.day,'保养','douyin',0,has_results=False)
            self.assertEqual(db.execute('select count(*) from keywords').fetchone()[0],2)
            db.execute('insert into hot(day,rank,word) values(?,?,?)',(self.day,2,'第二条热词'));db.commit()
            self.assertEqual(db.execute('select count(*) from hot').fetchone()[0],2)

    def test_analysis_migration_rolls_back_and_can_retry(self):
        folder,history=self.legacy_analyses()
        with patch.object(radar,'DATA',folder),patch.object(radar,'setting',side_effect=RuntimeError('模拟迁移中断')):
            with self.assertRaisesRegex(RuntimeError,'模拟迁移中断'):radar.connect()
        with closing(sqlite3.connect(folder/'radar.sqlite3')) as db:
            db.row_factory=sqlite3.Row
            self.assertEqual(primary_key(db,'video_analyses'),('aweme_id','provider'))
            self.assertEqual([tuple(r) for r in db.execute('select * from video_analyses order by rowid')],history)
            self.assertEqual(db.execute("select name from sqlite_master where name like '%_multi'").fetchall(),[])
        with patch.object(radar,'DATA',folder),closing(radar.connect()) as db:
            self.assertEqual(primary_key(db,'video_analyses'),('aweme_id','provider','model'))
            self.assertEqual([tuple(r) for r in db.execute('select * from video_analyses order by rowid')],history)


if __name__=='__main__':unittest.main()
