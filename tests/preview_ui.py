"""安全的电脑端验收预览：仅本地模拟数据，禁用搜索、登录、下载和真实 Codex。

运行 python3 tests/preview_ui.py 后访问 http://127.0.0.1:18777；按 Ctrl+C 退出。
临时库只创建于当前项目下，退出后自动删除。不要用正式数据目录运行预览。
"""
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))


def main():
    with tempfile.TemporaryDirectory(prefix='.radar-preview-',dir=ROOT) as data,ExitStack() as stack:
        os.environ['BAOKUAN_DATA']=data
        import radar
        import server
        from test_radar import analysis,legacy_analysis
        for target in ('radar.dy','radar.download','radar.transcribe','radar.notify','radar.fetch_hot','radar.platforms.bili_hot','radar.platforms.bili_search','radar.platforms.youtube_hot','radar.platforms.youtube_search','radar.platforms.youtube_public_search','radar.platforms.x_hot'):
            stack.enter_context(patch(target,side_effect=RuntimeError('预览模式已禁用外部操作')))
        stack.enter_context(patch.object(radar.platforms,'youtube_key',return_value=''))
        def codex(prompt,schema,*args,**kwargs):
            if schema=='hot_classification.json':
                rows=json.loads(prompt.split('热词材料：',1)[1].split('\n',1)[0])
                return {'items':[{'rank':r['rank'],'word':r['word'],'track':'其他','related':False}
                                 for r in rows]}
            return analysis()
        stack.enter_context(patch.object(radar,'codex',side_effect=codex))
        with radar.connect() as db:
            tracks=radar.hot_tracks();key=radar.hot_context(db)[2]
            titles=['新车用车成本怎么省','手机芯片新品发布','电影节开幕','篮球联赛决赛',
                    '社区便民服务上新','车险价格讨论','家常菜省时做法','秋日自驾路线',
                    '家庭账单整理','新手驾驶课堂','游戏赛事开赛','国际展会动态','城市新鲜事']
            radar.save_hot(db,radar.today(),[{'rank':i+1,'word':word,'hot_value':9800000-i*310000} for i,word in enumerate(titles)])
            for i,track in enumerate(tracks):
                if track=='其他':continue
                db.execute('update hot set track=?,related=?,classification_key=? where rank=?',
                           (track,int(track in ('汽车','财经','旅行','教育')),key,i+1))
            for ident,title,doc in [('12345678901','养车成本：这几项开支值得核对',analysis()),
                                    ('12345678902','旧版收藏：每月养车花多少',legacy_analysis())]:
                db.execute('''insert into videos(aweme_id,day,keyword,desc,author,followers,likes,comments,favorites,
                    shares,ratio,status,transcript,transcript_source,analysis,saved,tags,note)
                    values(?,?,?,?,?,?,?,?,?,?,?,'done',?,'whisper',?,1,'["汽车","成本"]','验收模拟数据')''',
                    (ident,radar.today(),'车评',title,'模拟车评账号',1000,20000,100,200,50,20,
                     '养车成本先看保养账单，再核对停车与保险。',json.dumps(doc,ensure_ascii=False)))
            for platform,ident,title in [('bilibili','BV1234567890','B站样例：保养账单怎么拆'),('youtube','AbCdEfG1234','Car review: real running costs')]:
                radar.save_hot(db,radar.today(),[{'rank':1,'word':title,'hot_value':100000}],platform)
                db.execute('update hot set track=?,related=1,classification_key=? where platform=?',('汽车',key,platform))
                doc=analysis()
                if platform=='youtube':
                    doc['hook']='real running costs';doc['hook_translation']='真实用车成本'
                    for r in [doc['opening_3s']]+doc['reasons']+doc['takeaways']:r.update(evidence='real running costs',evidence_translation='真实用车成本')
                db.execute('''insert into videos(aweme_id,platform,day,keyword,url,desc,author,views,coins,followers,likes,
                    comments,favorites,shares,ratio,status,transcript,analysis,saved,tags) values(?,?,?,?,?,?,?,100000,300,5000,2000,100,200,30,20,'done',?,?,1,'["汽车"]')''',
                    (platform+':'+ident,platform,radar.today(),'car review' if platform=='youtube' else '车评',radar.platforms.video_url(platform,ident),title,'模拟作者',
                     'Real running costs start with a service bill.' if platform=='youtube' else '养车成本先核对保养账单。',json.dumps(doc,ensure_ascii=False)))
            radar.save_hot(db,radar.today(),[{'rank':1,'word':'#CarReview','hot_value':None}],'x')
            db.execute("update hot set track='汽车',related=1,classification_key=? where platform='x'",(key,));db.commit()
            radar.put(db,'auto',False)
        server.PORT=18777
        app=ThreadingHTTPServer(('127.0.0.1',18777),server.Handler)
        print('本地模拟预览：http://127.0.0.1:18777（不会执行真实外部操作）',flush=True)
        try:app.serve_forever()
        except KeyboardInterrupt:pass
        finally:app.server_close()


if __name__=='__main__':main()
