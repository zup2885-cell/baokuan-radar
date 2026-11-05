"""从唯一界面源码生成 GitHub Pages 演示页：python3 build_demo.py。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def build_html():
    html = (ROOT / 'ui/index.html').read_text(encoding='utf-8')
    if html.count('<script>') != 1 or html.count('<html lang="zh-CN">') != 1:
        raise ValueError('界面入口发生变化，请更新构建标记')
    return ('<!-- 由 build_demo.py 从 ui/index.html 生成，请勿手工编辑。 -->\n' + html
            .replace('<html lang="zh-CN">', '<html lang="zh-CN" data-radar-static>')
            .replace('<title>爆款雷达</title>', '<title>爆款雷达 · 只读演示版</title>')
            .replace('<script>', '<script>\nwindow.RADAR_STATIC=true;'))


def main():
    docs = ROOT / 'docs'
    docs.mkdir(exist_ok=True)
    (docs / 'index.html').write_text(build_html(), encoding='utf-8')
    (docs / '.nojekyll').touch()
    print('已构建 docs/index.html（与本地界面共用源码，无外部依赖）。')


if __name__ == '__main__':
    main()
