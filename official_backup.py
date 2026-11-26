"""Independent official sources for the recent KL8 archive (no private APIs)."""
from html import unescape
from http.client import HTTPException
import re
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import app

GD_URL = 'https://www.gdfc.org.cn/lottery/kl8.html?period=100'
CWL_HOST = 'https://www.cwl.gov.cn'
ARTICLE = re.compile(r'https://www\.cwl\.gov\.cn(/(?:html5/)?c/(20\d{2})/(\d{2})/(\d{2})/\d+\.shtml)')
NET_ERRORS = (HTTPException, OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError)


class SourceDisagreement(ValueError):
    pass

CHINESE = {c:i for i,c in enumerate('零一二三四五六七八九十')}
EXPECTED = {f'x{k}z{h}' for k,tiers in app.TIERS.items() for h in tiers}


def html(url):
    req = Request(url, headers={'User-Agent':'KL8 Observatory/1.1 (public lottery statistics)', 'Accept':'text/html'})
    with urlopen(req,timeout=9) as res:
        first,last=urlsplit(url),urlsplit(res.geturl())
        if last.scheme!='https' or last.netloc!=first.netloc:
            raise ValueError('官方网页跳转到非预期站点')
        data=res.read(2_000_001)
    if len(data)>2_000_000: raise ValueError('官方页面超过大小限制')
    return data.decode('utf-8')


def rows(text):
    return re.findall(r'<tr\b[^>]*>(.*?)</tr\s*>',text,re.I|re.S)


def scrape_list(text):
    table=re.search(r'<table\b[^>]*lottery-game-draw-table--kl8[^>]*>(.*?)</table\s*>',text,re.I|re.S)
    if not table: raise ValueError('广东福彩网页缺少快乐8开奖表')
    out=[]
    for row in rows(table[1]):
        cells=re.findall(r'<td\b[^>]*>(.*?)</td\s*>',row,re.I|re.S)
        if not cells: continue
        if len(cells)!=3: raise ValueError('广东福彩开奖行格式变化')
        code=cells[0].strip()
        link=re.search(r'href\s*=\s*["\']([^"\']+)["\']',cells[2],re.I)
        match=ARTICLE.fullmatch(unescape(link[1])) if link else None
        if not match or not re.fullmatch(r'20\d{5}',code): raise ValueError('广东福彩期号或链接无效')
        nums=re.findall(r'<span\b[^>]*class=["\']lottery-ball["\'][^>]*>\s*(\d{1,2})\s*</span\s*>',cells[1],re.I)
        raw={'code':code,'date':'-'.join(match.groups()[1:]),'red':','.join(nums),
             'detailsLink':match[1],'_source':'guangdong_list'}
        out.append(app.parse_draw(raw))
    if not out or any(a['issue']<=b['issue'] or a['date']<=b['date'] for a,b in zip(out,out[1:])):
        raise ValueError('广东福彩开奖表为空或期号顺序异常')
    if out[0]['date']>app.now().date().isoformat(): raise ValueError('广东福彩日期在将来')
    return out


def announcement(text,rec):
    raw=dict(rec['raw'])
    issue=rec['issue']; day=rec['date']
    title=re.search(r'快乐\s*8[^<>\n]{0,12}第\s*(20\d{5})\s*期开奖公告',text)
    date=re.search(r'开奖日期\s*[:：]\s*(20\d\d-\d\d-\d\d)',text)
    nums=re.search(r'\bvar\s+kjh\s*=\s*["\']([0-9,\s]+)["\']',text)
    if not(title and title[1]==issue and date and date[1]==day and nums):
        raise ValueError(f'{issue} 期全国福彩公告未确认期号与日期')
    if sorted(app.balls(nums[1],issue))!=rec['numbers']:
        raise SourceDisagreement(f'{issue} 期全国与广东福彩开奖号码不一致')
    prizes={}
    for row in rows(text):
        cells=[re.sub(r'\s+','',unescape(re.sub(r'<[^>]*>','',x))) for x in re.findall(r'<td\b[^>]*>(.*?)</td\s*>',row,re.I|re.S)]
        if len(cells)!=3: continue
        m=re.fullmatch(r'选([一二三四五六七八九十])(?:中([一二三四五六七八九十])|全不中)',cells[0])
        if not m: continue
        code=f'x{CHINESE[m[1]]}z{CHINESE[m[2]] if m[2] else 0}'
        if code in prizes: raise ValueError('全国福彩公告有重复奖级')
        if not cells[1].replace(',','').isdigit() or app.money(cells[2]) is None: continue
        prizes[code]={'type':code,'typenum':cells[1],'typemoney':cells[2]}
    for key,label in [('sales','本期销售金额'),('poolmoney','(?:浮动奖奖池累计金额|奖池累计金额)')]:
        value=re.search(label+r'\s*[:：]\s*([\d,.]+)\s*元',text)
        if value: raw[key]=value[1]
    raw['prizegrades']=list(prizes.values())
    raw['_source']='cwl_announcement' if EXPECTED<=prizes.keys() else 'guangdong_list'
    return app.parse_draw(raw)


def fetch_recent():
    regional=scrape_list(html(GD_URL))
    with app.db_open() as db:
        existing={r['issue']:r for r in db.execute('SELECT * FROM draws WHERE issue>=?',(regional[-1]['issue'],))}
    if not set(existing).intersection(r['issue'] for r in regional):
        raise ValueError('广东福彩近100期未覆盖本地历史末端，不能跳期接续')
    latest=max(existing)
    if regional[0]['issue']<latest: raise ValueError('广东福彩开奖晚于本地数据，等待主源')
    out=[]; budget=4
    for r in regional:
        was=existing.get(r['issue'])
        same=was and app.unpack(was)['numbers']==r['numbers'] and was['draw_date']==r['date']
        need=not same or not EXPECTED<=app.unpack(was,prizes=True)['prizes'].keys()
        if not need: continue
        article=None
        if budget:
            budget-=1
            try: article=announcement(html(r['official_url']),r)
            except SourceDisagreement: raise
            except NET_ERRORS as exc: print(f"Announcement {r['issue']}: {type(exc).__name__}: {exc}",flush=True)
        if article and article['raw']['_source']=='cwl_announcement':
            out.append(article)
        elif was and not same:
            raise ValueError(f"{r['issue']} 期已保存的号码与备用源不同，暂不改写")
        elif not was:
            out.append(article or r)
        elif article:
            former=app.unpack(was,prizes=True)
            if set(article['prizes'])>set(former['prizes']): out.append(article)
    return out,regional[0]['issue']
