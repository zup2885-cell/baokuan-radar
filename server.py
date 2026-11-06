"""爆款雷达本机界面：只监听 127.0.0.1:8777。"""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from contextlib import closing
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit, parse_qs

import radar

UI=Path(__file__).resolve().parent/'ui'
PORT=8777
JOB={'name':None,'started':0,'result':''}
JOB_LOCK=threading.Lock()


def start_job(name,work,lock=True):
    with JOB_LOCK:
        if JOB['name']:return False
        JOB.update(name=name,started=time.time(),result='')
    def run():
        try:
            if lock:
                with radar.single('tick') as ok:
                    result=(work() or '完成') if ok else '上一轮还没跑完，请稍后再试'
            else:result=work() or '完成'  # tick 自己持锁
        except Exception as error:result='出错：'+str(error)[:200]
        with JOB_LOCK:JOB.update(result=result,name=None)
    threading.Thread(target=run,daemon=True).start()
    return True


def video_dict(row,full=False):
    doc={k:row[k] for k in row.keys() if full or k not in ('transcript','play_url')}
    doc['analysis']=json.loads(row['analysis']) if row['analysis'] else None
    tags=json.loads(row['tags'] or '[]')
    doc['tags']=radar.unique_text(tags) if isinstance(tags,list) else []
    return doc


def login_work():
    result=radar.dy('login')
    with closing(radar.connect()) as db:
        if result.get('ok'):
            radar.account_ready(db,'小号已登录，下一轮会按搜索间隔继续。');return '小号登录成功'
        code=(result.get('error') or {}).get('code') or 'NEED_LOGIN'
        message=radar.MESSAGES.get(code,'这次扫码没有完成，可以再试一次。')
        doc=radar.account(db)
        if doc.get('status')=='RATE_LIMITED' and time.time()-(doc.get('blocked_at') or 0)<24*3600:
            radar.set_account(db,message=message+' 仍需等待限流暂停期结束。')
        else:radar.set_account(db,status='NEED_LOGIN',message=message)
        return message


def challenge_work():
    with closing(radar.connect()) as db:
        doc=radar.account(db)
        if doc.get('status')=='RATE_LIMITED' and time.time()-(doc.get('blocked_at') or 0)<24*3600:
            return '限流暂停 24 小时中，请等待暂停期结束'
        word=(db.execute("select word from keywords where day=? and platform='douyin' and status='queued' order by rowid limit 1",(radar.today(),)).fetchone() or ['车评'])[0]
        result=radar.dy('challenge',word)
        if result.get('ok'):
            radar.account_ready(db,'本人验证已完成，下一轮会按搜索间隔继续。');return '验证完成，已恢复搜索'
        code=(result.get('error') or {}).get('code') or 'PLATFORM_BLOCKED'
        status=code if code in ('NEED_LOGIN','RATE_LIMITED') else 'PLATFORM_BLOCKED'
        message=radar.MESSAGES[status] if code!='CHALLENGE_CLOSED' else '验证尚未确认完成，搜索继续暂停。请重新打开验证窗口并本人完成验证。'
        radar.set_account(db,status=status,blocked_at=time.time(),message=message)
        return message


def reanalyze_work(ident):
    with closing(radar.connect()) as db:
        return '拆完了' if radar.process(db,ident,force=True) else '拆解失败，请查看视频详情和运行记录'


def classify_hot_work():
    with closing(radar.connect()) as db:
        return '；'.join(radar.platforms.NAMES[p]+'：'+radar.try_classify_hot(db,radar.today(),p) for p in radar.platforms.NAMES
                       if db.execute('select 1 from hot where day=? and platform=?',(radar.today(),p)).fetchone()) or '热榜没有待分类词条'


def validate_settings(data,current):
    limits={'keywords_per_day':(1,12,'每天关键词数'),'per_keyword':(1,10,'每词拆解数'),
            'min_likes':(0,1000000000,'最低点赞数'),'recent_days':(1,180,'最近天数'),
            'search_gap_minutes':(20,1440,'搜索间隔分钟数'),'active_start':(0,23,'开始时间'),
            'active_end':(1,24,'结束时间')}
    for platform in ('bilibili','youtube'):
        limits[platform+'_search_gap_minutes']=(20,1440,'平台搜索间隔')
        limits[platform+'_min_views']=(0,1000000000,'最低播放数')
    limits['youtube_searches_per_day']=(1,50,'YouTube 每天 API 搜索上限')
    if any(k not in radar.DEFAULTS for k in data):raise ValueError('设置中包含未知项目')
    current=dict(current)
    for key,value in data.items():
        if key in limits:
            low,high,label=limits[key]
            if isinstance(value,str) and re.fullmatch(r'[0-9]+',value.strip()):value=int(value.strip())
            if type(value) is not int or not low<=value<=high:raise ValueError(f'{label}须为 {low}–{high} 之间的整数')
        elif key=='auto' or key.endswith('_enabled'):
            if type(value) is not bool:raise ValueError('自动运行须为开启或关闭')
        elif key in ('fixed_keywords','youtube_fixed_keywords'):
            if not isinstance(value,list) or any(not isinstance(v,str) for v in value):raise ValueError('固定关键词须为文字列表')
            width=80 if key=='youtube_fixed_keywords' else 20
            if len(value)>10 or any(len(v.strip())>width for v in value):raise ValueError(f'固定关键词最多 10 个，每个最多 {width} 字')
            if key=='youtube_fixed_keywords' and any(v.strip() and (not re.search('[A-Za-z]',v) or re.search('[\u4e00-\u9fff]',v)) for v in value):raise ValueError('YouTube 固定关键词请填写英文搜索词')
            value=radar.unique_text(value,limit=10,width=width)
        elif key=='youtube_hot_category':
            if value not in ('','2'):raise ValueError('YouTube 热榜分类须为总榜或汽车')
        elif key=='field':
            if not isinstance(value,str) or not 1<=len(value.strip())<=200:raise ValueError('账号领域须为 1–200 字')
            value=value.strip()
        current[key]=value
    if current['active_start']>=current['active_end']:raise ValueError('结束时间必须晚于开始时间，运行时段不能跨天')
    if len(current['youtube_fixed_keywords'])>current['keywords_per_day']:raise ValueError('英文固定关键词数量不能超过每天关键词数')
    if len(current['fixed_keywords'])>current['keywords_per_day']:raise ValueError('固定关键词数量不能超过每天关键词数')
    return current


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass

    def send(self,body,status=200,kind='application/json; charset=utf-8'):
        data=body if isinstance(body,bytes) else json.dumps(body,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(data)

    def body(self):
        try:size=int(self.headers.get('Content-Length') or 0)
        except ValueError:raise ValueError('请求长度不正确')
        if not 0<=size<=200000:raise ValueError('请求内容大小不正确')
        try:doc=json.loads(self.rfile.read(size) or b'{}')
        except (ValueError,UnicodeError):raise ValueError('请求必须是有效的 JSON 对象')
        if not isinstance(doc,dict):raise ValueError('请求必须是 JSON 对象')
        return doc

    def do_GET(self):
        url=urlsplit(self.path);q={k:v[0] for k,v in parse_qs(url.query).items()}
        if url.path in ('/','/index.html'):return self.send((UI/'index.html').read_bytes(),kind='text/html; charset=utf-8')
        try:
            with closing(radar.connect()) as db:return self.get_api(db,url,q)
        except Exception:return self.send({'error':'读取本地数据失败，请检查后台日志'},500)

    def get_api(self,db,url,q):
        platform=q.get('platform','')
        if platform and platform not in radar.platforms.NAMES:return self.send({'error':'未知平台'},400)
        if url.path=='/api/state':
            day=q.get('day') or radar.today()
            keywords=[dict(r) for r in db.execute('select * from keywords where day=? order by rowid',(day,))]
            today_keywords=keywords if day==radar.today() else [dict(r) for r in db.execute('select * from keywords where day=? order by rowid',(radar.today(),))]
            days=[r[0] for r in db.execute("select day from videos where status not in ('candidate','irrelevant') and day>'' union select day from keywords order by day desc limit 30")]
            logs=[dict(r) for r in db.execute('select * from log order by rowid desc limit 12')]
            counts={r[0]:r[1] for r in db.execute('select status,count(*) from videos where day=? group by 1',(day,))}
            with JOB_LOCK:job={'name':JOB['name'],'result':JOB['result']}
            today_count=db.execute("select count(*) from videos where day=? and status not in ('candidate','irrelevant')",(radar.today(),)).fetchone()[0]
            return self.send({'today':radar.today(),'day':day,'days':days,'account':radar.account(db),'settings':radar.settings(db),
                              'youtube_key_configured':bool(radar.platforms.youtube_key()),
                              'platform_states':{p:radar.platform_state(db,p) for p in ('bilibili','youtube','x')},
                              'keywords':keywords,'today_keywords':today_keywords,'logs':logs,'counts':counts,'today_count':today_count,'job':job,
                              'saved':db.execute('select count(*) from videos where saved=1').fetchone()[0]})
        if url.path=='/api/videos':
            if q.get('view')=='library':
                rows=db.execute('select * from videos where saved=1 order by saved_at desc').fetchall()
                needle=(q.get('q') or '').strip();tag=q.get('tag') or ''
                if needle:rows=[r for r in rows if needle.casefold() in '\n'.join(str(r[k] or '') for k in ('desc','transcript','analysis','note','author','tags')).casefold()]
                items=[video_dict(r) for r in rows]
                if tag:items=[v for v in items if tag in v['tags']]
            else:
                rows=db.execute("select * from videos where day=? and status not in ('candidate','irrelevant') order by ratio is null, ratio desc, likes desc",(q.get('day') or radar.today(),)).fetchall()
                items=[video_dict(r) for r in rows]
            if platform:items=[v for v in items if v['platform']==platform]
            return self.send({'items':items})
        if url.path=='/api/video':
            row=db.execute('select * from videos where aweme_id=?',(q.get('id'),)).fetchone()
            return self.send(video_dict(row,True) if row else {'error':'找不到这条视频'},200 if row else 404)
        if url.path=='/api/hot':
            day=q.get('day') or radar.today()
            context=radar.hot_context(db);field,tracks,_=context
            items=radar.hot_items(db,day,context,platform or None)
            if not radar.platforms.youtube_key():items=[r for r in items if r['platform']!='youtube']
            counts={'all':len(items),'related':sum(r['related'] is True for r in items),
                    'tracks':{t:sum(r['track']==t for r in items) for t in tracks+['未分类']}}
            track=q.get('track','')
            if track and track not in tracks+['未分类']:return self.send({'error':'未知的热榜赛道'},400)
            if q.get('related','') not in ('','0','1'):return self.send({'error':'相关筛选须为 0 或 1'},400)
            if track:items=[r for r in items if r['track']==track]
            if q.get('related')=='1':items=[r for r in items if r['related'] is True]
            return self.send({'items':items,'tracks':tracks,'counts':counts,'field':field,'day':day})
        if url.path=='/api/tags':
            tags={}
            for (text,) in db.execute('select tags from videos where saved=1'+(' and platform=?' if platform else ''),(platform,) if platform else ()):
                for t in radar.unique_text(json.loads(text or '[]')):tags[t]=tags.get(t,0)+1
            return self.send({'items':sorted(tags.items(),key=lambda kv:-kv[1])})
        self.send({'error':'找不到这个地址'},404)

    def do_POST(self):
        if self.headers.get('Origin') not in (None,f'http://127.0.0.1:{PORT}',f'http://localhost:{PORT}'):return self.send({'error':'不允许从其他网站操作本机工具'},403)
        try:data=self.body()
        except ValueError as error:return self.send({'error':str(error)},400)
        try:
            with closing(radar.connect()) as db:return self.post_api(db,urlsplit(self.path).path,data)
        except ValueError as error:return self.send({'error':str(error)},400)
        except Exception:return self.send({'error':'本地操作失败，请检查后台日志'},500)

    def post_api(self,db,path,data):
        stamp=radar.now().isoformat(timespec='seconds')
        if path in ('/api/save','/api/note','/api/reanalyze'):
            if not isinstance(data.get('id'),str) or not db.execute('select 1 from videos where aweme_id=?',(data['id'],)).fetchone():
                return self.send({'error':'找不到这条视频'},404)
        if path=='/api/save':
            if type(data.get('saved')) is not bool:raise ValueError('收藏状态须为开启或关闭')
            db.execute('update videos set saved=?,saved_at=?,updated_at=? where aweme_id=?',(int(data['saved']),stamp if data['saved'] else None,stamp,data['id']));db.commit()
            return self.send({'ok':True})
        if path=='/api/note':
            tags=data.get('tags',[]);note=data.get('note','')
            if not isinstance(tags,list) or any(not isinstance(t,str) for t in tags):raise ValueError('标签须为文字列表')
            if not isinstance(note,str) or len(note)>5000:raise ValueError('备注须为文字，且不超过 5000 字')
            tags=radar.unique_text(tags)
            db.execute('update videos set tags=?,note=?,updated_at=? where aweme_id=?',(json.dumps(tags,ensure_ascii=False),note,stamp,data['id']));db.commit()
            return self.send({'ok':True})
        if path=='/api/settings':
            current=validate_settings(data,radar.settings(db))
            db.executemany('insert into settings values(?,?) on conflict(key) do update set value=excluded.value',
                           [(key,json.dumps(value,ensure_ascii=False)) for key,value in current.items()]);db.commit()
            return self.send({'ok':True,'settings':current})
        jobs={'/api/login':('扫码登录',login_work),'/api/challenge':('验证窗口',challenge_work),
              '/api/classify-hot':('热榜分类',classify_hot_work),
              '/api/run':('立即运行',lambda:radar.tick(force=True))}
        if path=='/api/reanalyze':
            ident=data['id']
            jobs[path]=('拆解文案',lambda:reanalyze_work(ident))
        if path in jobs:
            name,work=jobs[path]
            accepted=start_job(name,work,lock=path!='/api/run')
            with JOB_LOCK:busy=JOB['name']
            return self.send({'ok':accepted,'busy':busy})
        self.send({'error':'找不到这个地址'},404)


if __name__=='__main__':
    radar.connect().close()
    ThreadingHTTPServer(('127.0.0.1',PORT),Handler).serve_forever()
