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
        for target in ('radar.dy','radar.download','radar.transcribe','radar.notify','radar.fetch_hot'):
            stack.enter_context(patch(target,side_effect=RuntimeError('预览模式已禁用外部操作')))
        def codex(prompt,schema,*args,**kwargs):
            if schema=='hot_classification.json':
                with radar.connect() as db:
                    rows=radar.hot_items(db,radar.today())
                return {'items':[{'rank':r['rank'],'word':r['word'],'track':'其他','related':False}
                                 for r in rows if r['track']=='未分类']}
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
            radar.put(db,'auto',False)
        server.PORT=18777
        app=ThreadingHTTPServer(('127.0.0.1',18777),server.Handler)
        print('本地模拟预览：http://127.0.0.1:18777（不会执行真实外部操作）',flush=True)
        try:app.serve_forever()
        except KeyboardInterrupt:pass
        finally:app.server_close()


if __name__=='__main__':main()
