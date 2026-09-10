import sys, re, datetime, json
sys.path.insert(0, '/Users/zhangjianqiang/project/stock')
from dotenv import load_dotenv
load_dotenv('/Users/zhangjianqiang/project/stock/.env')
from utils.tavily import tavily_search

CST = datetime.timezone(datetime.timedelta(hours=8))


def id2d(aid):
    return datetime.datetime.fromtimestamp(int(aid) >> 32, CST).strftime('%Y-%m-%d')


def run(tag, **kw):
    r = tavily_search(search_depth='advanced', include_domains=['douyin.com'],
                      include_domains_mode='filter', country='china', language='zh',
                      include_raw_content=True, **kw)
    res = r.get('results', [])
    print('=== %s -> %d 条' % (tag, len(res)))
    hit = 0
    for it in res:
        m = re.search(r'/(?:video|shipin|note)/(\d{15,20})', it.get('url', ''))
        if not m:
            continue
        d = id2d(m.group(1))
        raw = it.get('raw_content') or ''
        pm = re.search(r'发布时间[:：]\s*(\d{4}-\d{2}-\d{2})', raw)
        print('   id日期=%s raw内日期=%s %s' % (d, pm.group(1) if pm else '-', (it.get('title') or '')[:36]))
        hit += 1
    print('   含视频链接:', hit, ' 有raw_content:', sum(1 for i in res if i.get('raw_content')))
    return res


run('A 多关键词+time_range=month', query='全能的野人 野人哥 股票', max_results=30, time_range='month')
run('B 关键词带日期 2026-09-01', query='全能的野人 2026年9月1日', max_results=30)
run('C 昵称+复盘', query='"全能的野人" 复盘', max_results=30, exact_match=True)
