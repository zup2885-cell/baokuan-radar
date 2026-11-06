"""公开平台适配器。标准库 + 已安装 SDK/yt-dlp，不登录、不重试挑战。
B站 SDK 和 yt-dlp 调用方式改编自同作者 MIT 抖音数据看板 local-tool/research_*.py。
"""
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.request
from urllib.parse import urlencode

HERE = Path(__file__).resolve().parent
BILI_PYTHON = Path.home()/'.agent-reach/tools/bili-cli/venv/bin/python'
VIDEO_PYTHON = Path.home()/'Library/Application Support/TzFilmDouyin/video-tools/venv/bin/python'
NAMES = {'douyin':'抖音','bilibili':'B站','youtube':'YouTube','x':'X'}
ERROR_LABELS = {'UPSTREAM_BROKEN':'公开数据读取失败','RATE_LIMITED':'请求被拒绝或限流','PLATFORM_BLOCKED':'平台要求验证或拒绝读取','TIMEOUT':'请求超时','NEED_SETUP':'找不到既有 Python 环境','DAILY_LIMIT':'已到每天搜索上限'}


# 公开接口会拒绝没有浏览器标识的请求。
UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'


class PlatformError(Exception):
    """只保留固定错误码，不能携带 URL、响应体、密钥或子进程日志。"""
    def __init__(self, code='UPSTREAM_BROKEN'):
        self.code = code if code in ('UPSTREAM_BROKEN','RATE_LIMITED','PLATFORM_BLOCKED','TIMEOUT','NEED_SETUP','DAILY_LIMIT') else 'UPSTREAM_BROKEN'
        super().__init__(self.code)


def count(value):
    if isinstance(value,str) and re.fullmatch(r'\d+',value):
        try:value=int(value)
        except ValueError:return None
    return value if type(value) is int and 0<=value<=9007199254740991 else None


def published(value):
    try:
        if type(value) in (int,float):return datetime.fromtimestamp(value,timezone.utc).isoformat()
        if isinstance(value,str) and re.fullmatch(r'\d{8}',value):
            return datetime.strptime(value,'%Y%m%d').replace(tzinfo=timezone.utc).isoformat()
        if isinstance(value,str):return datetime.fromisoformat(value.replace('Z','+00:00')).isoformat()
    except (ValueError,OverflowError,OSError):pass
    return ''


def video_url(platform,ident):
    if platform=='douyin' and re.fullmatch(r'\d+',ident):return 'https://www.douyin.com/video/'+ident
    if platform=='bilibili' and re.fullmatch(r'BV[0-9A-Za-z]{10}',ident):return 'https://www.bilibili.com/video/'+ident
    if platform=='youtube' and re.fullmatch(r'[\w-]{11}',ident,flags=re.ASCII):return 'https://www.youtube.com/watch?v='+ident
    return ''


def item(platform,ident,title,author,metrics,date,cover=''):
    url=video_url(platform,str(ident or ''))
    if not url:return None
    result=dict(platform=platform,aweme_id=platform+':'+ident,url=url,desc=str(title or '')[:10000],
                author=str(author or '')[:200],published_at=published(date),cover=str(cover or ''),play_url='',duration=None)
    result.update({k:count(metrics.get(k)) for k in ('views','likes','followers','comments','favorites','shares','coins')})
    return result


def read_json(url,headers=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':UA,**(headers or {})}),timeout=20) as response:
            body=response.read(5*1024*1024+1)
        if len(body)>5*1024*1024:raise PlatformError()
        doc=json.loads(body)
        if not isinstance(doc,dict):raise PlatformError()
        return doc
    except urllib.error.HTTPError as error:
        raise PlatformError('RATE_LIMITED' if error.code in (403,412,429) else 'UPSTREAM_BROKEN') from None
    except (OSError,ValueError):raise PlatformError() from None


def youtube_key():
    # 不向调用者传递 stderr/异常文本；此值只在内存中用于 HTTPS 请求头。
    try:
        result=subprocess.run(['security','find-generic-password','-s','youtube-api','-w'],
                              capture_output=True,text=True,timeout=5,stdin=subprocess.DEVNULL)
        return result.stdout.strip() if result.returncode==0 else ''
    except (OSError,subprocess.TimeoutExpired):return ''


def youtube_api(endpoint,params,key):
    doc=read_json('https://www.googleapis.com/youtube/v3/'+endpoint+'?'+urlencode(params),{'X-Goog-Api-Key':key})
    if 'error' in doc:raise PlatformError('RATE_LIMITED')
    if not isinstance(doc.get('items'),list):raise PlatformError()
    return doc['items']


def parse_bili_hot(doc):
    if doc.get('code') in (-352,-412,-509):raise PlatformError('PLATFORM_BLOCKED')
    if doc.get('code')!=0:raise PlatformError()
    # 热搜接口把列表放在顶层 list，热搜广场接口放在 data.trending.list。
    data=doc.get('data') or {};rows=data.get('trending',{}).get('list') or data.get('list') or doc.get('list')
    if not isinstance(rows,list):raise PlatformError()
    result=[];seen=set()
    for row in rows:
        word=(row.get('keyword') or row.get('show_name')) if isinstance(row,dict) else ''
        if not isinstance(word,str) or not word.strip() or word.strip() in seen:continue
        seen.add(word.strip());result.append({'rank':len(result)+1,'word':word.strip()[:100],'hot_value':count(row.get('heat'))})
    if not result:raise PlatformError()
    return result


def bili_hot():
    return parse_bili_hot(read_json('https://s.search.bilibili.com/main/hotword?limit=50'))


def bili_item(row):
    stat=row.get('stat') or {};owner=row.get('owner') or {}
    return item('bilibili',row.get('bvid'),row.get('title'),owner.get('name'),
                {'views':stat.get('view'),'likes':stat.get('like'),'coins':stat.get('coin'),
                 'favorites':stat.get('favorite'),'shares':stat.get('share'),'comments':stat.get('reply'),
                 'followers':row.get('followers')},row.get('pubdate'),row.get('pic'))


def bili_search(word,limit,days):
    doc=run_json([str(BILI_PYTHON),str(HERE/'bili_worker.py'),word,str(limit),str(days)],timeout=150)
    if not doc.get('ok'):raise PlatformError((doc.get('error') or {}).get('code'))
    if not isinstance(doc.get('items'),list):raise PlatformError()
    return [v for r in doc['items'] if isinstance(r,dict) and (v:=bili_item(r))]


def run_process(args,timeout=120):
    try:
        result=subprocess.run(args,capture_output=True,text=True,timeout=timeout,stdin=subprocess.DEVNULL,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
    except FileNotFoundError:raise PlatformError('NEED_SETUP') from None
    except subprocess.TimeoutExpired:raise PlatformError('TIMEOUT') from None
    except OSError:raise PlatformError() from None
    if result.returncode:
        err=(result.stderr or '').lower()
        code='RATE_LIMITED' if any(s in err for s in ('429','too many requests','rate limit')) else 'PLATFORM_BLOCKED' if any(s in err for s in ('403','412','not a bot','sign in to confirm','captcha','verify')) else 'UPSTREAM_BROKEN'
        raise PlatformError(code)
    if len(result.stdout)>8*1024*1024:raise PlatformError()
    return result.stdout


def run_json(args,timeout=120):
    try:
        doc=json.loads(run_process(args,timeout))
        if not isinstance(doc,dict):raise ValueError()
        return doc
    except ValueError:raise PlatformError() from None


def yt_args():
    return [str(VIDEO_PYTHON),'-m','yt_dlp','--ignore-config','--no-warnings','--retries','0',
            '--extractor-retries','0','--fragment-retries','0','--file-access-retries','0','--socket-timeout','15','--no-cache-dir']


def duration_seconds(value):
    if type(value) in (int,float):return value if 0<=value<31536000 else None
    if isinstance(value,str):
        match=re.fullmatch(r'P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?',value)
        if match and any(match.groups()):
            return sum(float(n or 0)*scale for n,scale in zip(match.groups(),(86400,3600,60,1)))
    return None


def youtube_searchable(row,min_duration=60):
    duration=duration_seconds(row.get('duration'))
    return duration is not None and duration>=min_duration and not re.search(r'#shorts\b',row.get('desc') or '',re.I)


def youtube_item(row):
    if row.get('language') and not row['language'].lower().startswith('en'):return None
    v=item('youtube',row.get('id'),row.get('title'),row.get('channel') or row.get('uploader'),
                {'views':row.get('view_count'),'likes':row.get('like_count'),'followers':row.get('channel_follower_count'),
                 'comments':row.get('comment_count')},row.get('upload_date'),row.get('thumbnail'))
    if v:v['duration']=duration_seconds(row.get('duration'))
    return v


def youtube_public_search(word,limit=30):
    # 不使用 flat-playlist，逐条提取公开计数、订阅与 upload_date，绝不下载媒体。
    doc=run_json(yt_args()+['--skip-download','--dump-single-json','--playlist-end',str(limit),
                           'ytsearch'+str(limit)+':'+word],timeout=240)
    if not isinstance(doc.get('entries'),list):raise PlatformError()
    return [v for r in doc['entries'][:limit] if isinstance(r,dict) and (v:=youtube_item(r)) and youtube_searchable(v)]


def youtube_official_items(videos,channels):
    followers={r['id']:None if (r.get('statistics') or {}).get('hiddenSubscriberCount') else (r.get('statistics') or {}).get('subscriberCount') for r in channels}
    result=[]
    for row in videos:
        snippet=row.get('snippet') or {};stats=row.get('statistics') or {}
        lang=snippet.get('defaultAudioLanguage') or snippet.get('defaultLanguage') or ''
        if lang and not lang.lower().startswith('en'):continue
        v=item('youtube',row.get('id'),snippet.get('title'),snippet.get('channelTitle'),
               {'views':stats.get('viewCount'),'likes':stats.get('likeCount'),'comments':stats.get('commentCount'),
                'followers':followers.get(snippet.get('channelId'))},snippet.get('publishedAt'))
        if v:
            v['duration']=duration_seconds((row.get('contentDetails') or {}).get('duration'))
            if snippet.get('description'):v['desc']+='\n\n'+str(snippet['description'])[:3000]
            result.append(v)
    return result


def youtube_search(word,limit,after,key,min_duration=60):
    hits=youtube_api('search',{'part':'snippet','q':word,'type':'video','order':'viewCount','maxResults':limit,
                             'publishedAfter':after,'relevanceLanguage':'en','regionCode':'US','videoCategoryId':'2'},key)
    ids=[r.get('id',{}).get('videoId') for r in hits];ids=[i for i in ids if video_url('youtube',str(i or ''))]
    if not ids:return []
    videos=youtube_api('videos',{'part':'snippet,statistics,contentDetails','id':','.join(ids)},key)
    # 仍是一次 search、一次 videos、一次 channels，不增加逐条判断的模型或接口调用。
    videos=[r for r in videos if youtube_searchable({'desc':r.get('snippet',{}).get('title',''),
                                                   'duration':(r.get('contentDetails') or {}).get('duration')},min_duration)]
    channel_ids=list(dict.fromkeys(r.get('snippet',{}).get('channelId') for r in videos if r.get('snippet',{}).get('channelId')))
    channels=youtube_api('channels',{'part':'statistics','id':','.join(channel_ids)},key) if channel_ids else []
    return youtube_official_items(videos,channels)


def youtube_hot(key,category=''):
    params={'part':'snippet,statistics','chart':'mostPopular','regionCode':'US','maxResults':50}
    if category:params['videoCategoryId']=category
    rows=youtube_api('videos',params,key)
    return [{'rank':i+1,'word':str(r.get('snippet',{}).get('title') or '')[:200],
             'hot_value':count(r.get('statistics',{}).get('viewCount'))} for i,r in enumerate(rows) if r.get('snippet',{}).get('title')]


class TrendsParser(HTMLParser):
    """只取第一张时间榜，忽略脚本、导航、历史时段和推文数。"""
    def __init__(self):
        super().__init__();self.in_list=False;self.finished=False;self.anchor=False;self.words=[];self.parts=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='ol' and 'trend-card__list' in attrs.get('class','').split() and not self.finished:self.in_list=True
        if self.in_list and tag=='a':self.anchor=True;self.parts=[]
    def handle_data(self,data):
        if self.anchor:self.parts.append(data)
    def handle_endtag(self,tag):
        if tag=='a' and self.anchor:
            word=''.join(self.parts).strip();self.anchor=False
            if word and word not in self.words:self.words.append(word[:100])
        if tag=='ol' and self.in_list:self.in_list=False;self.finished=True


def parse_x(text):
    parser=TrendsParser();parser.feed(text)
    if not parser.words:raise PlatformError()
    return [{'rank':i+1,'word':unescape(word),'hot_value':None} for i,word in enumerate(parser.words[:50])]


def x_hot():
    try:
        with urllib.request.urlopen(urllib.request.Request('https://trends24.in/united-states/',headers={'User-Agent':UA}),timeout=20) as response:body=response.read(3*1024*1024+1)
        if len(body)>3*1024*1024:raise PlatformError()
        return parse_x(body.decode('utf-8'))
    except urllib.error.HTTPError as error:raise PlatformError('RATE_LIMITED' if error.code in (403,429) else 'UPSTREAM_BROKEN') from None
    except (OSError,UnicodeError):raise PlatformError() from None
