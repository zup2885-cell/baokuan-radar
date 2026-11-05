"""小号专用的抖音只读进程（改编自抖音创作台 research_dy_worker.py）。

只做三件事：扫码登录、在固定浏览器资料里打开搜索页读取结果、打开可见窗口让本人过验证。
不点赞、不评论、不关注，不读取其他浏览器的 Cookie。由 dy-cli 的独立 Python 运行。
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys


class ReadFailure(Exception):
    def __init__(self, code):self.code=code


DOUYIN_DOMAINS=('douyin.com','www.douyin.com','creator.douyin.com')
SEARCH_FEEDS=('/aweme/v1/web/search/item/','/aweme/v1/web/general/search/single/')
CAPTCHA='#captcha_container, .captcha_verify_container, .vc-captcha-verify'


def isolate(root):
    from dy_cli.utils import config
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(root,0o700)
    config.CONFIG_DIR=str(root)
    config.CONFIG_FILE=str(root/'config.json')
    config.COOKIES_DIR=str(root/'cookies')
    config.DEFAULT_CONFIG['api']['cookie_file']=str(root/'cookies/default.json')
    return root/'cookies/default.json'


def counter(value):
    if isinstance(value,bool) or not isinstance(value,int) or not 0<=value<=9007199254740991:return None
    return value


def public_item(row):
    if not isinstance(row,dict):return None
    ident=str(row.get('aweme_id') or '')
    if not re.fullmatch(r'\d{10,24}',ident):return None
    stat=row.get('statistics') if isinstance(row.get('statistics'),dict) else {}
    author=row.get('author') if isinstance(row.get('author'),dict) else {}
    video=row.get('video') if isinstance(row.get('video'),dict) else {}
    def first_https(node):
        urls=node.get('url_list') or [] if isinstance(node,dict) else []
        if not isinstance(urls,list):return ''
        return next((u for u in urls if isinstance(u,str) and u.startswith('https://')),'')
    published=row.get('create_time')
    try:published=datetime.fromtimestamp(published,timezone.utc).isoformat() if type(published) is int and published>0 else ''
    except (ValueError,OverflowError,OSError):published=''
    duration=video.get('duration')
    return {'aweme_id':ident,'url':'https://www.douyin.com/video/'+ident,
            'desc':str(row.get('desc') or '')[:5000],
            'author':str(author.get('nickname') or '')[:150],
            'followers':counter(author.get('follower_count')),
            'cover':first_https(video.get('cover') or video.get('origin_cover')),
            'play_url':first_https(video.get('play_addr')),
            'duration':duration/1000 if type(duration) is int and duration>0 else None,
            'published_at':published,
            'likes':counter(stat.get('digg_count')),'comments':counter(stat.get('comment_count')),
            'favorites':counter(stat.get('collect_count')),'shares':counter(stat.get('share_count'))}


def feed_failure(body):
    if not isinstance(body,dict):return 'UPSTREAM_BROKEN'
    nil=body.get('search_nil_info') or {}
    if isinstance(nil,dict) and nil.get('search_nil_type')=='verify_check':return 'PLATFORM_BLOCKED'
    if body.get('verify_check') or body.get('verify_info'):return 'PLATFORM_BLOCKED'
    if body.get('status_code')==429:return 'RATE_LIMITED'
    if body.get('status_code') in (8,1105,1109,2483) or '登录' in str(body.get('status_msg') or ''):return 'NEED_LOGIN'
    if body.get('status_code') not in (None,0):return 'UPSTREAM_BROKEN'
    return None


def record_failure(state,code):
    # 已观察到的验证/限流不能被随后普通错误覆盖。
    if state['code']=='RATE_LIMITED':return
    if state['code'] in ('PLATFORM_BLOCKED','NEED_LOGIN') and code not in ('PLATFORM_BLOCKED','NEED_LOGIN','RATE_LIMITED'):return
    state['code']=code


def douyin_cookies(cookies):return [c for c in cookies if isinstance(c,dict) and c.get('domain','').lstrip('.') in DOUYIN_DOMAINS]


def save_cookies(cookie_file,cookies):
    keep=douyin_cookies(cookies)
    if keep:
        cookie_file.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        cookie_file.write_text(json.dumps({'cookies':keep,'origins':[]}));os.chmod(cookie_file,0o600)
    return keep


async def open_profile(pw,root,cookie_file,headless):
    """小号固定用一个浏览器资料，让抖音每次看到的是同一台设备。"""
    profile=root/'browser-profile'
    profile.mkdir(mode=0o700,exist_ok=True);os.chmod(profile,0o700)
    context=await pw.chromium.launch_persistent_context(str(profile),channel='chromium',headless=headless,
                                                        locale='zh-CN',timezone_id='Asia/Shanghai',viewport={'width':1280,'height':860})
    if cookie_file.is_file():
        await context.add_cookies(douyin_cookies(json.loads(cookie_file.read_text()).get('cookies',[])))
    return context


def search_url(query,days):
    from urllib.parse import quote
    # sort_type=1 是「最多点赞」，publish_time 是发布时间筛选（1/7/182 天）。
    return 'https://www.douyin.com/search/'+quote(query,safe='')+'?type=video&sort_type=1&publish_time='+str(days)


def browser_search(root,cookie_file,query,limit,days,wait=35):
    """打开抖音自己的视频搜索页，读取页面本身请求回来的结果；只滚动，不点击。"""
    import asyncio
    from playwright.async_api import async_playwright
    async def go():
        rows,seen,state=[],set(),{'code':None,'received':False}
        async with async_playwright() as pw:
            context=await open_profile(pw,root,cookie_file,headless=True)
            try:
                page=context.pages[0] if context.pages else await context.new_page()
                async def on_response(response):
                    if not any(f in response.url.split('?')[0] for f in SEARCH_FEEDS):return
                    if response.status==429:record_failure(state,'RATE_LIMITED');return
                    if response.status>=400:record_failure(state,'UPSTREAM_BROKEN');return
                    try:body=await response.json()
                    except Exception:record_failure(state,'UPSTREAM_BROKEN');return
                    code=feed_failure(body)
                    if code:record_failure(state,code);return
                    data=body.get('data')
                    if not isinstance(data,list):record_failure(state,'UPSTREAM_BROKEN');return
                    state['received']=True
                    for r in data:
                        row=(r.get('aweme_info') or r) if isinstance(r,dict) else None
                        item=public_item(row)
                        if item and item['aweme_id'] not in seen:seen.add(item['aweme_id']);rows.append(row)
                page.on('response',on_response)
                await page.goto(search_url(query,days),wait_until='domcontentloaded',timeout=30000)
                loop=asyncio.get_running_loop();deadline=loop.time()+wait;scrolls=0
                while loop.time()<deadline and not state['code'] and len(rows)<limit:
                    await page.wait_for_timeout(2500)
                    if await page.locator(CAPTCHA).count():
                        record_failure(state,'PLATFORM_BLOCKED');break
                    if rows and scrolls<4:
                        await page.mouse.wheel(0,1400);scrolls+=1
                if await page.locator(CAPTCHA).count():record_failure(state,'PLATFORM_BLOCKED')
                if not state['received'] and not state['code']:state['code']='TIMEOUT'
                save_cookies(cookie_file,await context.cookies())
            finally:
                await context.close()
        return rows,state['code']
    return asyncio.run(go())


def challenge_window(root,cookie_file,query,seconds=300):
    """可见窗口打开搜索页，验证由本人完成；不替人点任何东西。"""
    import asyncio
    from playwright.async_api import async_playwright
    async def go():
        latest=None;state={'code':'PLATFORM_BLOCKED'}
        async with async_playwright() as pw:
            context=await open_profile(pw,root,cookie_file,headless=False)
            try:
                page=context.pages[0] if context.pages else await context.new_page()
                async def on_response(response):
                    if not any(f in response.url.split('?')[0] for f in SEARCH_FEEDS):return
                    if response.status==429:record_failure(state,'RATE_LIMITED');return
                    try:body=await response.json()
                    except Exception:return
                    code=feed_failure(body)
                    if code:record_failure(state,code)
                    elif state['code']!='RATE_LIMITED' and response.status==200 and isinstance(body.get('data'),list):state['code']=None
                page.on('response',on_response)
                await page.goto(search_url(query,7),wait_until='domcontentloaded')
                front()
                for _ in range(seconds//2):
                    if page.is_closed():break
                    latest=await context.cookies()
                    if await page.locator(CAPTCHA).count():record_failure(state,'PLATFORM_BLOCKED')
                    elif state['code'] is None:break
                    await asyncio.sleep(2)
            except Exception:pass
            finally:
                try:await context.close()
                except Exception:pass
        return latest,state['code']
    return asyncio.run(go())


def front():
    """把扫码窗口切到最前面；从后台服务打开时它常被别的窗口挡住。"""
    import subprocess
    subprocess.run(['osascript','-e','tell application "Google Chrome for Testing" to activate'],capture_output=True,timeout=5)


def qr_login(root,cookie_file,seconds=300):
    """在小号固定的浏览器资料里打开抖音首页登录框，本人扫码。

    以出现登录 Cookie（sessionid）为准，不依赖跳转到哪个页面；最多等 5 分钟。
    """
    import asyncio
    from playwright.async_api import async_playwright
    async def go():
        async with async_playwright() as pw:
            context=await open_profile(pw,root,cookie_file,headless=False)
            try:
                page=context.pages[0] if context.pages else await context.new_page()
                await page.goto('https://www.douyin.com/',wait_until='domcontentloaded',timeout=30000)
                front()
                for _ in range(seconds//2):
                    if page.is_closed():return False
                    cookies=await context.cookies()
                    if any(c.get('name') in ('sessionid','sessionid_ss') and c.get('value') for c in cookies):
                        await page.wait_for_timeout(3000)
                        return bool(save_cookies(cookie_file,await context.cookies()))
                    await asyncio.sleep(2)
                return False
            finally:
                try:await context.close()
                except Exception:pass
    return asyncio.run(go())


def run(action,query,limit,root,days):
    cookie_file=isolate(root)
    os.umask(0o077)
    if action=='login':
        ok=bool(qr_login(root,cookie_file))
        return {'ok':ok,'error':None if ok else {'code':'LOGIN_TIMEOUT'}}
    if not cookie_file.is_file():raise ReadFailure('NEED_LOGIN')
    if not query or len(query)>100:raise ReadFailure('BAD_INPUT')
    if action=='challenge':
        cookies,code=challenge_window(root,cookie_file,query)
        keep=save_cookies(cookie_file,cookies) if cookies else []
        if code or not keep:return {'ok':False,'error':{'code':code or 'CHALLENGE_CLOSED'}}
        return {'ok':True}
    if action!='search' or not 1<=limit<=40 or days not in (1,7,182):raise ReadFailure('BAD_INPUT')
    rows,code=browser_search(root,cookie_file,query,limit,days)
    items=[v for r in rows if (v:=public_item(r))][:limit]
    if code in ('PLATFORM_BLOCKED','NEED_LOGIN','RATE_LIMITED') or code and not items:raise ReadFailure(code)
    if not items:raise ReadFailure('NO_PUBLIC_DATA')
    return {'ok':True,'items':items}


if __name__=='__main__':
    try:result=run(sys.argv[1],sys.argv[2],int(sys.argv[3]),Path(sys.argv[4]),int(sys.argv[5]))
    except ReadFailure as error:result={'ok':False,'error':{'code':error.code}}
    except Exception as error:
        # 不输出原始异常：里面可能带有链接参数或请求头。
        result={'ok':False,'error':{'code':'TIMEOUT' if 'Timeout' in type(error).__name__ else 'UPSTREAM_BROKEN'}}
    print(json.dumps(result,ensure_ascii=False))
