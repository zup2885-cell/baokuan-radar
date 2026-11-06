"""手动联网核验，每个平台仅一次；不由 unittest 自动执行。"""
import json
from pathlib import Path
import sys
from concurrent.futures import ThreadPoolExecutor
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import platforms


def run(name,call):
    try:
        rows=call()
        safe=[{k:r[k] for k in ('platform','aweme_id','rank','word','hot_value','desc','author','views','likes','followers','coins','comments','favorites','shares','published_at') if k in r} for r in rows]
        (ROOT/'tests/fixtures'/f'{name}.json').write_text(json.dumps(safe,ensure_ascii=False,indent=2)+'\n')
        return {'source':name,'ok':True,'count':len(rows),'metrics_present':{k:sum(r.get(k) is not None for r in rows) for k in ('views','likes','followers')} if 'search' in name else {}}
    except Exception as error:
        return {'source':name,'ok':False,'error':error.code if isinstance(error,platforms.PlatformError) else type(error).__name__}


if __name__=='__main__':
    jobs=[('bili_hot',platforms.bili_hot),('bili_search',lambda:platforms.bili_search('汽车评测',5,7)),
          ('youtube_search',lambda:platforms.youtube_public_search('car review',5)),('x_hot',platforms.x_hot)]
    with ThreadPoolExecutor(max_workers=4) as executor:results=list(executor.map(lambda job:run(*job),jobs))
    (ROOT/'tests/fixtures/read_results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(results,ensure_ascii=False))
