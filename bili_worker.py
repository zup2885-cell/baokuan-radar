"""由现有 bili-cli Python 运行。改编自同作者 MIT research_bili_search.py。"""
import asyncio
from datetime import date, timedelta
import json
import re
import sys


async def collect(word,limit,days):
    from bilibili_api import search, video, user, request_settings
    request_settings.set_wbi_retry_times(1)
    request_settings.set('timeout',10)
    data=await search.search_by_type(keyword=word,search_type=search.SearchObjectType.VIDEO,
        order_type=search.OrderVideo.CLICK,page=1,page_size=limit,
        time_start=(date.today()-timedelta(days=days)).isoformat(),time_end=date.today().isoformat())
    if not isinstance(data,dict):raise ValueError('B站搜索返回格式不正确')
    results=data.get('result')
    # B站无匹配视频时会省略 result；必须有明确的零计数，不能吞掉损坏的响应。
    if results is None and all(type(data.get(k)) is int and data[k]==0 for k in ('numResults','numPages')):
        results=[]
    if not isinstance(results,list):raise ValueError('B站搜索结果格式不正确')
    rows=[];followers={}
    for row in results[:limit]:
        ident=row.get('bvid','')
        if not re.fullmatch(r'BV[0-9A-Za-z]{10}',ident):continue
        info=await video.Video(bvid=ident).get_info()
        uid=(info.get('owner') or {}).get('mid')
        if uid and uid not in followers:
            try:followers[uid]=(await user.User(uid=uid).get_relation_info()).get('follower')
            except Exception as error:
                if blocked(error):raise
                followers[uid]=None
        info['followers']=followers.get(uid)
        rows.append({k:info.get(k) for k in ('bvid','title','owner','stat','followers','pubdate','pic')})
    return {'ok':True,'items':rows}


def blocked(error):
    return getattr(error,'code',None) in (403,412,429,-403,-412,-352,-509) or 'WbiRetry' in type(error).__name__


if __name__=='__main__':
    try:result=asyncio.run(collect(sys.argv[1],int(sys.argv[2]),int(sys.argv[3])))
    except Exception as error:
        result={'ok':False,'error':{'code':'PLATFORM_BLOCKED' if blocked(error) else 'UPSTREAM_BROKEN'}}
    print(json.dumps(result,ensure_ascii=False))
