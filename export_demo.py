"""导出可公开的演示快照；不加载后台、不迁移数据库、不执行外部程序。"""
import argparse
from contextlib import closing
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3

from platforms import video_url

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = Path.home() / 'Library/Application Support/BaokuanRadar/radar.sqlite3'
DEFAULT_OUTPUT = ROOT / 'docs/data.json'
# 自由文本中也可能出现调试路径；不把本机位置带到公开快照。
LOCAL_PATH = re.compile(
    r'file://[^\s<>"\'，。；）]*|'
    r'(?<![\w:/])(?:~[/\\]|\.{1,2}[/\\]|[A-Za-z]:[/\\]|\\\\|/[^/\s<>"\'，。；）])[^\s<>"\'，。；）]*'
)


def public_text(value):
    if not isinstance(value,str):return ''
    value=re.sub(r'AIza[0-9A-Za-z_-]{35}', '［密钥已隐藏］', value)
    return LOCAL_PATH.sub('［本机路径已隐藏］',value)


def public_value(value, spec):
    """沿分析 schema 白名单递归，旧分析缺字段也可展示，额外字段一律不公开。"""
    kind = spec['type']
    if kind == 'object':
        if not isinstance(value, dict):
            return None
        return {key: public_value(value[key], child) for key, child in spec['properties'].items()
                if key in value}
    if kind == 'array':
        return [public_value(item, spec['items']) for item in value] if isinstance(value, list) else []
    if kind == 'string':
        return public_text(value)
    if kind == 'boolean':
        return value if type(value) is bool else False
    return value if type(value) is int else None


def decode_json(value, fallback):
    try:
        return json.loads(value) if value else fallback
    except (ValueError, TypeError):
        return fallback


def valid_day(value):
    try:
        if date.fromisoformat(value).isoformat() == value:
            return value
    except (ValueError, TypeError):
        pass
    raise ValueError('日期须为有效的 YYYY-MM-DD')


def export_snapshot(db_path=DEFAULT_DB, day=None):
    path = Path(db_path).expanduser().resolve()
    # as_uri 会转义空格、中文、#、?；mode=ro 禁止写入，也不会创建不存在的库。
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only = ON')
        db.execute('BEGIN')  # 所有表在同一个一致性读事务中读取。
        days = {row[0] for row in db.execute(
            'SELECT day FROM hot UNION SELECT day FROM keywords UNION SELECT day FROM videos')
            if row[0] and re.fullmatch(r'\d{4}-\d{2}-\d{2}', row[0])}
        days = {d for d in days if _is_day(d)}
        day = valid_day(day) if day is not None else max(days, default=None)
        if day is None:
            raise ValueError('数据库中还没有可导出的日期')
        if day not in days:
            raise ValueError('指定日期没有快照数据')
        row = db.execute("SELECT value FROM settings WHERE key='field'").fetchone()
        raw_field = decode_json(row[0], '') if row else ''
        tracks = json.loads((ROOT / 'config/hot_tracks.json').read_text(encoding='utf-8'))
        classification_key = hashlib.sha256(json.dumps([raw_field, tracks], ensure_ascii=False).encode()).hexdigest()
        hot_columns = {r['name'] for r in db.execute('PRAGMA table_info(hot)')}
        hot = []
        if 'platform' in hot_columns:
            hot_query='SELECT * FROM hot WHERE day=(SELECT max(h.day) FROM hot h WHERE h.platform=hot.platform AND h.day<=?) ORDER BY platform,rank'
        else:hot_query='SELECT * FROM hot WHERE day=? ORDER BY rank'
        for row in db.execute(hot_query, (day,)):
            classified = ({'track', 'related', 'classification_key'} <= hot_columns
                          and row['classification_key'] == classification_key
                          and row['track'] in tracks and row['related'] in (0, 1))
            hot.append({'platform': row['platform'] if 'platform' in row.keys() else 'douyin', 'day':row['day'], 'rank': row['rank'], 'word': public_text(row['word']), 'hot_value': row['hot_value'],
                        'track': row['track'] if classified else '未分类',
                        'related': bool(row['related']) if classified else None})
        keywords = [{'platform':r['platform'] if 'platform' in r.keys() else 'douyin', 'word': public_text(r['word']), 'status': public_text(r['status']), 'found': r['found'],
                     'hot_word': public_text(r['hot_word'])} for r in db.execute(
                         'SELECT * FROM keywords WHERE day=? ORDER BY rowid', (day,))]
        schemas={p:json.loads((ROOT / 'schemas'/('analysis_youtube.json' if p=='youtube' else 'analysis.json')).read_text(encoding='utf-8')) for p in ('douyin','bilibili','youtube')}
        videos = []
        # 保留完成的历史收藏；今日列表由 day 筛选。指定历史日时不混入之后的视频。
        for row in db.execute("SELECT * FROM videos WHERE status='done' AND coalesce(day,'')<=? "
                              'ORDER BY ratio IS NULL, ratio DESC, likes DESC, aweme_id', (day,)):
            video = {key: public_text(row[key]) for key in
                     ('aweme_id', 'day', 'desc', 'author', 'keyword', 'published_at')}
            video['platform']=row['platform'] if 'platform' in row.keys() else 'douyin'
            for key in ('followers', 'likes', 'views', 'coins', 'comments', 'favorites', 'shares', 'ratio'):
                number = row[key] if key in row.keys() else None
                video[key] = number if isinstance(number, (int, float)) and math.isfinite(number) else None
            # 只保留原作品链接；拒绝 file:、脚本、本机地址和下载地址。
            ident = row['aweme_id'] or ''
            video['url'] = video_url(video['platform'],ident.removeprefix(video['platform']+':'))
            video['status'] = 'done'
            video['saved'] = row['saved'] == 1
            tags = decode_json(row['tags'], [])
            video['tags'] = list(dict.fromkeys(public_text(t).strip() for t in tags
                                             if isinstance(t, str) and t.strip())) if isinstance(tags, list) else []
            video['analysis'] = public_value(decode_json(row['analysis'], None), schemas.get(video['platform'],schemas['douyin']))
            video['transcript_excerpt'] = public_text((row['transcript'] or '')[:80])[:80]
            # 防止旧分析的自由文本意外粘贴全文或媒体地址。
            transcript = row['transcript'] or ''
            secrets = [s for s in (row['play_url'], row['cover']) if s]
            if len(transcript) > 80:
                secrets.append(transcript)
            def scrub(value):
                if isinstance(value, str):
                    for secret in sorted(secrets, key=len, reverse=True):
                        value = value.replace(secret, '［原始材料已省略］')
                    return value
                if isinstance(value, list):
                    return [scrub(v) for v in value]
                if isinstance(value, dict):
                    return {k: scrub(v) for k, v in value.items()}
                return value
            video = scrub(video)
            video['transcript_excerpt'] = video['transcript_excerpt'][:80]
            videos.append(video)
        return {'snapshot_date': day, 'field': public_text(raw_field), 'tracks': tracks,
                'hot': hot, 'keywords': keywords, 'videos': videos}


def _is_day(value):
    try:
        valid_day(value)
        return True
    except ValueError:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=DEFAULT_DB, help='只读 SQLite 数据库路径')
    parser.add_argument('--day', help='导出日期 YYYY-MM-DD，默认最新一天')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT, help='输出 JSON，默认 docs/data.json')
    args = parser.parse_args()
    try:
        # 不允许误把数据库或其 WAL/SHM 指为输出文件。
        output = args.output.expanduser().resolve()
        source = args.db.expanduser().resolve()
        if (output in (source, Path(str(source) + '-wal'), Path(str(source) + '-shm'))
                or (output.exists() and source.exists() and output.samefile(source))):
            raise ValueError('输出文件不能覆盖数据库或其辅助文件')
        snapshot = export_snapshot(args.db, args.day)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    except (OSError, sqlite3.Error, ValueError) as error:
        parser.exit(1, f'导出失败：{error}\n')
    print(f"已导出 {snapshot['snapshot_date']}：热榜 {len(snapshot['hot'])} 条，"
          f"关键词 {len(snapshot['keywords'])} 个，已拆解视频 {len(snapshot['videos'])} 条，"
          f"其中收藏 {sum(v['saved'] for v in snapshot['videos'])} 条。")


if __name__ == '__main__':
    main()
