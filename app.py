"""Public read-only dashboard; only the worker/CLI imports official data."""
import argparse
from contextlib import contextmanager
import csv
from datetime import date, datetime, time as clock, timedelta
from decimal import Decimal, InvalidOperation
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from model import Engine, METHODS, MODEL_VERSION, group_hist, odds_for, plans, settle_plan, TIERS

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / 'static'
DB_PATH = Path(os.environ.get('KL8_DB', ROOT / 'data' / 'kl8.sqlite3'))
SEED = ROOT / 'data' / 'official_seed.json.gz'
SEED_SHA256 = '7262d196162c0557c2ae6c01d1c21ae985a7110b3d69dbeeb7164828340d3eb9'
RELEASE_FORECAST = ROOT / 'data' / 'release_forecast.json.gz'
RELEASE_FORECAST_SHA256 = '61ab77e04af53d06c3afb58fe9bc5d458fd6ff73b42fe3d14d557a90c99f747d'
API = 'https://www.cwl.gov.cn/cwl_admin/front/cwlkj/search/kjxx/findDrawNotice'
SOURCE_PAGE = 'https://www.cwl.gov.cn/html5/ygkj/wqkjgg/kl8/'
TZ = ZoneInfo('Asia/Shanghai')
CUTOFF = clock(21, 0)  # Site's conservative lock deadline, not a sales closing time.
LOCK = threading.Lock()
FIRST_ISSUE = '2020001'
FIRST_DATE = '2020-10-28'
OFFICIAL_CLOSED_2026 = ((date(2026,2,14),date(2026,2,24)), (date(2026,10,1),date(2026,10,5)))
PRIZE_TABLE = {
  1:{1:'4.5'}, 2:{2:'19'}, 3:{3:'52',2:'3'}, 4:{4:'93',3:'5',2:'3'},
  5:{5:'1000',4:'20',3:'3'}, 6:{6:'2880',5:'30',4:'10',3:'3'},
  7:{7:'8500',6:'300',5:'30',4:'4',0:'2'}, 8:{8:'50000',7:'800',6:'80',5:'10',4:'3',0:'2'},
  9:{9:None,8:'2000',7:'225',6:'22',5:'5',4:'3',0:'2'},
 10:{10:None,9:'8000',8:'720',7:'80',6:'5',5:'3',0:'2'},
}


def now(): return datetime.now(TZ)
def stamp(t=None): return (t or now()).isoformat(timespec='seconds')
def dump(o): return json.dumps(o,ensure_ascii=False,separators=(',',':'),allow_nan=False)


@contextmanager
def db_open(write=False):
    db = sqlite3.connect(DB_PATH,timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA recursive_triggers=ON')
        db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    DB_PATH.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(DB_PATH,timeout=30)
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
        CREATE TABLE IF NOT EXISTS draws(issue TEXT PRIMARY KEY, draw_date TEXT NOT NULL UNIQUE,
            numbers TEXT NOT NULL, draw_order TEXT, prizes TEXT NOT NULL, sales TEXT, poolmoney TEXT,
            official_url TEXT NOT NULL, raw TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS forecasts(issue TEXT PRIMARY KEY, draw_date TEXT NOT NULL,
            created_at TEXT NOT NULL, based_on TEXT NOT NULL REFERENCES draws(issue), payload TEXT NOT NULL,
            integrity TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS forecasts_no_update BEFORE UPDATE ON forecasts
            BEGIN SELECT RAISE(ABORT,'immutable forecast'); END;
        CREATE TRIGGER IF NOT EXISTS forecasts_no_delete BEFORE DELETE ON forecasts
            BEGIN SELECT RAISE(ABORT,'immutable forecast'); END;
        CREATE TABLE IF NOT EXISTS settlements(issue TEXT PRIMARY KEY REFERENCES forecasts(issue),
            settled_at TEXT NOT NULL, draw_hash TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settlement_audit(id INTEGER PRIMARY KEY, issue TEXT NOT NULL REFERENCES forecasts(issue),
            settled_at TEXT NOT NULL, draw_hash TEXT NOT NULL, payload TEXT NOT NULL,
            CHECK(length(draw_hash)=64));
        CREATE TABLE IF NOT EXISTS backtests(model_version TEXT NOT NULL, issue TEXT NOT NULL REFERENCES draws(issue),
            payload TEXT NOT NULL, PRIMARY KEY(model_version,issue));
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        '''); db.commit()
    finally: db.close()


def get_meta(db,key,default=None):
    row=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
    return json.loads(row[0]) if row else default


def put_meta(db,key,value):
    db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,dump(value)))


def sha(s): return hashlib.sha256(s.encode()).hexdigest()


def balls(text,issue):
    values=[int(x.strip()) for x in str(text).split(',')]
    if len(values)!=20 or len(set(values))!=20 or min(values)<1 or max(values)>80:
        raise ValueError(f'第 {issue} 期号码格式不符')
    return values


def money(s):
    try:
        value=Decimal(str(s).replace(',',''))
        if value.is_finite() and 0<=value<10**13: return format(value,'f')
    except InvalidOperation: pass
    return None


def parse_draw(raw):
    if not isinstance(raw,dict): raise ValueError('开奖数据行不是对象')
    issue=str(raw.get('code',''))
    text=str(raw.get('date',''))[:10]
    day=date.fromisoformat(text)
    if not re.fullmatch(r'20[0-9]{5}',issue) or issue[:4]!=text[:4] or not 1<=int(issue[4:])<=366:
        raise ValueError(f'期号与年份不符: {issue}')
    nums=sorted(balls(raw.get('red'),issue))
    order_raw=raw.get('kjhyjsx')
    order=balls(order_raw,issue) if order_raw else None
    if order and sorted(order)!=nums: raise ValueError(f'第 {issue} 期开球顺序不符')
    prizes={}
    for g in raw.get('prizegrades') or []:
        code=str(g.get('type',''))
        if re.fullmatch(r'x(?:[1-9]|10)z(?:[0-9]|10)',code):
            count=str(g.get('typenum','')).replace(',','')
            prizes[code]={'count':int(count) if count.isdigit() and len(count)<16 else None,
                          'amount':money(g.get('typemoney'))}
    path=str(raw.get('detailsLink',''))
    url='https://www.cwl.gov.cn'+path if re.fullmatch(r'/(?:html5/)?c/[0-9/]+\.shtml',path) else SOURCE_PAGE
    return {'issue':issue,'date':day.isoformat(),'numbers':nums,'draw_order':order,'prizes':prizes,
            'sales':money(raw.get('sales')),'poolmoney':money(raw.get('poolmoney')),'official_url':url,'raw':raw}


def fetch_page(page,size):
    query=urlencode({'name':'kl8','issueCount':'','issueStart':'','issueEnd':'','dayStart':'','dayEnd':'',
                     'pageNo':page,'pageSize':size,'week':'','systemType':'PC'})
    req=Request(API+'?'+query,headers={'User-Agent':'Mozilla/5.0 (compatible; KL8-Observatory/1.0)',
                                     'Referer':SOURCE_PAGE,'Accept':'application/json,text/plain'})
    with urlopen(req,timeout=20) as res:
        body=res.read(12_000_001)
    if len(body)>12_000_000: raise RuntimeError('官方响应超过大小限制')
    p=json.loads(body)
    if not isinstance(p,dict) or p.get('state')!=0 or not isinstance(p.get('result'),list):
        raise RuntimeError('中国福彩网未返回有效开奖数据')
    if int(p['pageNo'])!=page or int(p['pageSize'])!=size: raise RuntimeError('官方分页格式不符')
    return p


def fetch_official(full=False):
    size=200 if full else 50
    first=fetch_page(1,size)
    total=int(first['total']); pages=int(first['pageNum'])
    if not 1<=total<=25000 or pages!=(total+size-1)//size: raise RuntimeError('官方分页信息异常')
    raw=list(first['result'])
    if len(raw)!=min(size,total): raise RuntimeError('官方首页数量不符')
    if full:
        for pn in range(2,pages+1):
            time.sleep(0.15)
            p=fetch_page(pn,size)
            if int(p['total'])!=total or len(p['result'])!=min(size,total-(pn-1)*size):
                raise RuntimeError('取数期间分页已变化，下次重试')
            raw.extend(p['result'])
    records=[parse_draw(r) for r in raw]
    if any(a['issue']<=b['issue'] or a['date']<=b['date'] for a,b in zip(records,records[1:])):
        raise RuntimeError('官方期号重复或排列不符')
    if date.fromisoformat(records[0]['date'])>now().date(): raise RuntimeError('官方最新日期异常')
    return records,total


def audit(rows):
    prev=None
    for row in rows:
        issue,day=row['issue'],row['draw_date']
        if prev is None:
            if (issue,day)!=(FIRST_ISSUE,FIRST_DATE): raise ValueError('历史未从 2020001 开始，请执行全量同步')
        elif day<=prev[1] or (issue[:4]==prev[0][:4] and int(issue)!=int(prev[0])+1) or (
                issue[:4]!=prev[0][:4] and (int(issue[:4])!=int(prev[0][:4])+1 or issue[4:]!='001')):
            raise ValueError(f'历史数据不连续: {prev[0]} → {issue}；请执行全量同步')
        prev=(issue,day)


def import_draws(db,records):
    changed=0
    for r in records:
        c=db.execute('''INSERT INTO draws VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(issue) DO UPDATE SET
          draw_date=excluded.draw_date,numbers=excluded.numbers,draw_order=excluded.draw_order,
          prizes=excluded.prizes,sales=excluded.sales,poolmoney=excluded.poolmoney,official_url=excluded.official_url,
          raw=excluded.raw WHERE draws.raw<>excluded.raw''',
          (r['issue'],r['date'],dump(r['numbers']),dump(r['draw_order']),dump(r['prizes']),
           r['sales'],r['poolmoney'],r['official_url'],dump(r['raw'])))
        changed+=c.rowcount
    return changed


def unpack(r,prizes=False):
    d={'issue':r['issue'],'date':r['draw_date'],'numbers':json.loads(r['numbers']),
       'draw_order':json.loads(r['draw_order']),'official_url':r['official_url'],
       'sales':r['sales'],'poolmoney':r['poolmoney']}
    if prizes: d['prizes']=json.loads(r['prizes'])
    return d


def forecast_date(last,dt):
    closures=list(OFFICIAL_CLOSED_2026)
    for item in filter(None,os.environ.get('KL8_CLOSED','').split(',')):
        a,_,b=item.partition(':'); a,b=date.fromisoformat(a),date.fromisoformat(b)
        if a>=b: raise ValueError('KL8_CLOSED 日期区间无效')
        closures.append((a,b))
    day=date.fromisoformat(last['draw_date'])+timedelta(days=1)
    while any(start<=day<end for start,end in closures): day+=timedelta(days=1)
    # Do not guess a late issue or silently shift a frozen date after a missing upstream draw.
    if not dt.date()<=day<=dt.date()+timedelta(days=1): return None
    if dt>=datetime.combine(day,CUTOFF,TZ): return None
    issue=(f'{day.year}001' if str(day.year)!=last['issue'][:4] else f'{int(last["issue"])+1:07d}')
    return issue,day


def decorate_plan(p):
    odds=odds_for(p)
    fixed=Decimal(0); floating=Decimal(0)
    for h, amount in PRIZE_TABLE[p['k']].items():
        unit=Decimal(str(odds['tier_expected_winners'].get(str(h),0)))
        if amount is None: floating+=unit
        else: fixed+=Decimal(amount)*unit
    return {**p,'odds':odds,'stake':p['count']*2,
            'expected_fixed_prize':format(fixed,'f'),
            'expected_fixed_net':format(fixed-Decimal(p['count']*2),'f'),
            'expected_floating_tier_hits':format(floating,'f'),
            'ticket':('single' if p['mode']=='single' else 'under_numeric_cap' if p['count']<=10000 else 'math_only')}


def displayed_plans(payload):
    # Early deployments froze only single and four-full plans. The same immutable
    # full ranking defines deterministic supplemental plans for those snapshots.
    frozen={(p['mode'],p['k']):p for p in payload['plans']}
    if payload['model_version']!='prequential-1':
        return [{**p,'derived_from_ranking':False} for p in payload['plans']]
    return [{**decorate_plan(frozen.get((p['mode'],p['k']),p)),
             'derived_from_ranking':(p['mode'],p['k']) not in frozen} for p in plans(payload['ranking'])]


def public_forecast(payload, integrity):
    public={k:v for k,v in payload.items() if k not in ('candidates','candidate_rankings')}
    return {**public,'plans':displayed_plans(payload),'integrity':integrity}


def pack_forecast(db,engine,latest):
    dt=now()
    pair=forecast_date(latest,dt)
    if not pair: return None
    issue,day=pair
    if db.execute('SELECT 1 FROM draws WHERE issue=?',(issue,)).fetchone(): return None
    existing=db.execute('SELECT issue FROM forecasts WHERE issue=?',(issue,)).fetchone()
    if existing: return issue
    payload=engine.predict(issue)
    payload.update({'issue':issue,'estimated_draw_date':str(day),'created_at':stamp(dt),
        'cutoff_at':stamp(datetime.combine(day,CUTOFF,TZ)),'history_last_issue':latest['issue'],
        'top4':sorted(payload['ranking'][:4]),'top20':sorted(payload['ranking'][:20]),'plans':[]})
    payload['plans']=[decorate_plan(p) for p in plans(payload['ranking'])]
    serial=dump(payload)
    db.execute('INSERT INTO forecasts VALUES(?,?,?,?,?,?)',(issue,str(day),stamp(dt),latest['issue'],serial,sha(serial)))
    return issue


def replay(db,rows,force):
    engine=Engine()
    digest=hashlib.sha256()
    for r in rows:
        digest.update((r['issue']+r['numbers']).encode())
    signature=MODEL_VERSION+':'+digest.hexdigest()
    rebuild=force or signature!=get_meta(db,'backtest_signature')
    for r in (rows if rebuild else rows[-200:]):
        proof=engine.predict(r['issue'])
        actual=set(json.loads(r['numbers']))
        metrics=engine.observe(proof,actual)
        if rebuild:
            selected=proof['model']; rank=proof['ranking']; hits=metrics[selected]
            modes={k:len(actual.intersection(rank[:k])) for k in range(1,11)}
            hypothetical=[]
            for p in plans(rank):
                d,t=len(actual.intersection(p['dan'])),len(actual.intersection(p['tuo']))
                hist=group_hist(p['k'],len(p['dan']),len(p['tuo']),d,t)
                hypothetical.append({'k':p['k'],'mode':p['mode'],'winning_count':sum(hist.get(h,0) for h in TIERS[p['k']])})
            result={'method':selected,'metrics':metrics,'selected':hits,'top10':rank[:10],
                    'single_hits':modes,'single_wins':{k:h in TIERS[k] for k,h in modes.items()},
                    'four_dan_hits':hits['top4'],'plans':hypothetical}
            db.execute('''INSERT INTO backtests VALUES(?,?,?) ON CONFLICT(model_version,issue)
                        DO UPDATE SET payload=excluded.payload''',(MODEL_VERSION,r['issue'],dump(result)))
    if rebuild: put_meta(db,'backtest_signature',signature)
    return engine


def settle(db):
    rows=db.execute('''SELECT f.issue,f.payload AS prediction,d.* FROM forecasts f JOIN draws d ON d.issue=f.issue
                       LEFT JOIN settlements s ON s.issue=f.issue ORDER BY f.issue''').fetchall()
    for r in rows:
        revision=sha(json.dumps([r['draw_date'],json.loads(r['numbers']),json.loads(r['prizes']),r['official_url']],sort_keys=True,separators=(',',':')))
        old=db.execute('SELECT draw_hash FROM settlements WHERE issue=?',(r['issue'],)).fetchone()
        if old and old[0]==revision: continue
        f=json.loads(r['prediction']); d=unpack(r,prizes=True)
        # Unexpected rescheduling cannot turn a retrospectively created solution into a live result.
        if datetime.fromisoformat(f['created_at'])>=datetime.combine(date.fromisoformat(d['date']),CUTOFF,TZ):
            result={'issue':d['issue'],'date':d['date'],'official_numbers':d['numbers'],'official_url':d['official_url'],
                    'status':'invalid_cutoff','details':[]}
        else:
            current=set(d['numbers'])
            scores=Engine().observe(f,current)
            result={'issue':d['issue'],'date':d['date'],'official_numbers':d['numbers'],'official_url':d['official_url'],
                    'status':'settled','model':f['model'],'selection_score':scores[f['model']],
                    'top20_hits':len(current.intersection(f['top20'])),'top4_hits':len(current.intersection(f['top4'])),
                    'top4':f['top4'],'candidate_scores':scores,
                    'details':[{**settle_plan(p,d),'derived_from_ranking':p['derived_from_ranking']} for p in displayed_plans(f)]}
        args=(d['issue'],stamp(),revision,dump(result))
        db.execute('''INSERT INTO settlements VALUES(?,?,?,?) ON CONFLICT(issue) DO UPDATE SET
                    settled_at=excluded.settled_at,draw_hash=excluded.draw_hash,payload=excluded.payload''',args)
        db.execute('INSERT INTO settlement_audit(issue,settled_at,draw_hash,payload) VALUES(?,?,?,?)',args)


def load_seed():
    with db_open() as db:
        if db.execute('SELECT 1 FROM draws LIMIT 1').fetchone() or not SEED.exists(): return
    raw=SEED.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=SEED_SHA256: raise ValueError('开奖历史种子 SHA-256 校验失败')
    with gzip.open(io.BytesIO(raw),'rt',encoding='utf-8') as f: records=[parse_draw(r) for r in json.load(f)]
    with db_open(write=True) as db:
        if db.execute('SELECT 1 FROM draws LIMIT 1').fetchone(): return
        import_draws(db,records)
        rows=db.execute('SELECT * FROM draws ORDER BY issue').fetchall(); audit(rows); replay(db,rows,True)
        put_meta(db,'sync',{'status':'seed','checked_at':None,'official_total':len(rows),'latest':rows[-1]['issue']})


def load_release_forecast():
    if not RELEASE_FORECAST.is_file(): return
    raw=RELEASE_FORECAST.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=RELEASE_FORECAST_SHA256: raise ValueError('锁号档案 SHA-256 校验失败')
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream: record=json.loads(stream.read().decode('utf-8'))
    issue,day,created,basis,payload,digest=(record[k] for k in ('issue','draw_date','created_at','based_on','payload','integrity'))
    f=json.loads(payload)
    if (sha(payload)!=digest or f['issue']!=issue or f['history_last_issue']!=basis or
        f['estimated_draw_date']!=day or f['created_at']!=created or
        datetime.fromisoformat(created)>=datetime.combine(date.fromisoformat(day),CUTOFF,TZ)):
        raise ValueError('随包锁号记录字段校验失败')
    with db_open(write=True) as db:
        if not db.execute('SELECT 1 FROM draws WHERE issue=?',(basis,)).fetchone():
            raise ValueError('随包锁号记录的依据开奖不存在')
        db.execute('INSERT INTO forecasts VALUES(?,?,?,?,?,?) ON CONFLICT(issue) DO NOTHING',
                   (issue,day,created,basis,payload,digest))
        settle(db)


def sync_official(full=False):
    with LOCK:
        status={'checked_at':stamp(),'status':'error'}
        try:
            records,total=fetch_official(full)
            with db_open(write=True) as db:
                changed=import_draws(db,records)
                rows=db.execute('SELECT * FROM draws ORDER BY issue').fetchall(); audit(rows)
                if len(rows)!=total or rows[-1]['issue']!=records[0]['issue']:
                    raise ValueError('本地与官方总期数或最新期不一致，请执行全量同步')
                engine=replay(db,rows,False)
                settle(db)
                next_id=pack_forecast(db,engine,rows[-1])
                status={'status':'ok','checked_at':stamp(),'official_total':total,'latest':rows[-1]['issue'],
                        'fetched':len(records),'changed':changed,'forecast_issue':next_id,'full':full}
                put_meta(db,'sync',status)
                if full: put_meta(db,'last_full',now().date().isoformat())
        except (OSError,RuntimeError,ValueError,sqlite3.Error,KeyError,TypeError) as e:
            status={'checked_at':stamp(),'status':'error','error':str(e)[:300]}
            with db_open(write=True) as db: put_meta(db,'sync',status)
        return status


def get_state():
    with db_open() as db:
        latest=db.execute('SELECT * FROM draws ORDER BY issue DESC LIMIT 1').fetchone()
        frozen=db.execute('''SELECT f.* FROM forecasts f LEFT JOIN draws d ON d.issue=f.issue
                           WHERE d.issue IS NULL ORDER BY f.issue DESC LIMIT 1''').fetchone()
        draws=db.execute('SELECT COUNT(*) FROM draws').fetchone()[0]
        tests=db.execute('SELECT COUNT(*) FROM backtests WHERE model_version=?',(MODEL_VERSION,)).fetchone()[0]
        recent=[json.loads(r[0]) for r in db.execute('SELECT payload FROM backtests WHERE model_version=? ORDER BY issue DESC LIMIT 120',(MODEL_VERSION,))]
        n=len(recent)
        aggregates={}
        for m in (*METHODS,'selected'):
            vals=[(r['selected'] if m=='selected' else r['metrics'][m]) for r in recent]
            aggregates[m]={'rounds':n,**{f'mean_{key}':sum(v[key] for v in vals)/n if n else None
                            for key in ('brier','top4','top10')}}
        srows=db.execute('SELECT issue,payload FROM settlements ORDER BY issue DESC LIMIT 30').fetchall()
        latest_settled=db.execute('SELECT issue FROM settlements ORDER BY issue DESC LIMIT 1').fetchone()
        pending=None
        if frozen:
            p=json.loads(frozen['payload'])
            pending=public_forecast(p,frozen['integrity'])
            pending['waiting']=now()>=datetime.fromisoformat(p['cutoff_at'])
        return {'source':SOURCE_PAGE,'latest_draw':unpack(latest) if latest else None,'forecast':pending,
                'history_count':draws,'backtest_count':tests,'backtest_window':aggregates,
                'settled_forecasts':[{'issue':r['issue'],'status':json.loads(r['payload'])['status']} for r in srows],
                'latest_settled_issue':latest_settled[0] if latest_settled else None,
                'sync':get_meta(db,'sync',{'status':'not_run'}),'server_time':stamp(),'standard_prizes':PRIZE_TABLE}


def get_history(page,limit):
    with db_open() as db:
        total=db.execute('SELECT COUNT(*) FROM draws').fetchone()[0]
        rows=db.execute('SELECT * FROM draws ORDER BY issue DESC LIMIT ? OFFSET ?',(limit,(page-1)*limit)).fetchall()
        return {'total':total,'page':page,'limit':limit,'rows':[unpack(r) for r in rows]}


def get_round(issue):
    with db_open() as db:
        f=db.execute('SELECT payload,integrity FROM forecasts WHERE issue=?',(issue,)).fetchone()
        d=db.execute('SELECT * FROM draws WHERE issue=?',(issue,)).fetchone()
        b=db.execute('SELECT payload FROM backtests WHERE issue=? AND model_version=?',(issue,MODEL_VERSION)).fetchone()
        s=db.execute('SELECT settled_at,payload FROM settlements WHERE issue=?',(issue,)).fetchone()
        forecast=None
        if f:
            forecast=public_forecast(json.loads(f['payload']),f['integrity'])
        return {'forecast':forecast,'draw':unpack(d,prizes=True) if d else None,
                'settlement':{**json.loads(s['payload']),'settled_at':s['settled_at']} if s else None,
                'historical_backtest':json.loads(b[0]) if b else None} if (f or d) else None


def export_csv():
    f=io.StringIO(); writer=csv.writer(f)
    writer.writerow(['issue','draw_date','numbers','draw_order','sales_cny','pool_cny','official_url'])
    with db_open() as db:
        for r in db.execute('SELECT * FROM draws ORDER BY issue'):
            d=unpack(r)
            fmt=lambda a: ' '.join(f'{x:02}' for x in a) if a else ''
            writer.writerow([d['issue'],d['date'],fmt(d['numbers']),fmt(d['draw_order']),d['sales'],d['poolmoney'],d['official_url']])
    return f.getvalue().encode('utf-8-sig')


class HTTP(ThreadingHTTPServer):
    daemon_threads=True
    def get_request(self):
        socket, address=super().get_request(); socket.settimeout(10); return socket,address


class Handler(BaseHTTPRequestHandler):
    server_version='KL8/1.0'
    def reply(self,status,body,ctype='application/json; charset=utf-8',headers=None):
        if not isinstance(body,bytes): body=dump(body).encode()
        self.send_response(status); self.send_header('Content-Type',ctype)
        self.send_header('Content-Length',str(len(body))); self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        for k,v in (headers or {}).items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        u=urlparse(self.path)
        try:
            if u.path=='/api/state': return self.reply(200,get_state())
            if u.path=='/api/history':
                query=parse_qs(u.query); page=int(query.get('page',['1'])[0]); limit=int(query.get('limit',['50'])[0])
                if not 1<=page<=25000 or not 1<=limit<=200: raise ValueError('Invalid page or limit')
                return self.reply(200,get_history(page,limit))
            if re.fullmatch(r'/api/round/20[0-9]{5}',u.path):
                r=get_round(u.path.rsplit('/',1)[1]); return self.reply(200 if r else 404,r or {'error':'not found'})
            if u.path=='/api/health':
                with db_open() as db: db.execute('SELECT 1 FROM meta LIMIT 1').fetchone()
                return self.reply(200,{'ok':True})
            if u.path=='/history.csv': return self.reply(200,export_csv(),'text/csv; charset=utf-8',
                {'Content-Disposition':'attachment; filename="kl8-history.csv"'})
            assets={'/':('index.html','text/html'),'/style.css':('style.css','text/css'),
                    '/extras.css':('extras.css','text/css'),'/app.js':('app.js','text/javascript')}
            if u.path in assets:
                name,typ=assets[u.path]; return self.reply(200,(STATIC/name).read_bytes(),typ+'; charset=utf-8')
            self.reply(404,{'error':'not found'})
        except ValueError as e: self.reply(400,{'error':str(e)[:100]})
        except sqlite3.Error: self.reply(503,{'error':'database unavailable'})
        except (BrokenPipeError,ConnectionResetError): pass


def worker(seconds):
    while True:
        with db_open() as db:
            full=get_meta(db,'last_full')!=now().date().isoformat()
        result=sync_official(full=full)
        print(dump(result),flush=True)
        time.sleep(max(300,seconds) if result['status']=='error' else seconds)


def main():
    p=argparse.ArgumentParser(description='快乐8开奖、统计与锁号网站')
    actions=p.add_mutually_exclusive_group()
    actions.add_argument('--sync',action='store_true',help='官方全量同步并退出')
    actions.add_argument('--sync-recent',action='store_true',help='同步最近 50 期并退出')
    actions.add_argument('--export',type=Path,metavar='NEW_CSV',help='导出历史 CSV 到新文件并退出')
    p.add_argument('--host',default=os.environ.get('KL8_HOST','127.0.0.1'))
    p.add_argument('--port',type=int,default=int(os.environ.get('PORT','8080')))
    p.add_argument('--interval',type=int,default=int(os.environ.get('KL8_INTERVAL','120')))
    p.add_argument('--no-sync',action='store_true',help='只读网站和离线回测，用于检查或调试')
    args=p.parse_args()
    if not 60<=args.interval<=86400: p.error('--interval must be 60–86400 seconds')
    init_db(); load_seed(); load_release_forecast()
    if args.sync or args.sync_recent:
        result=sync_official(full=args.sync); print(dump(result)); raise SystemExit(result['status']!='ok')
    if args.export:
        with args.export.open('xb') as out: out.write(export_csv())
        return
    if not args.no_sync: threading.Thread(target=worker,args=(args.interval,),daemon=True,name='cwl-sync').start()
    print(f'http://{args.host}:{args.port}',flush=True)
    with HTTP((args.host,args.port),Handler) as server: server.serve_forever()


if __name__=='__main__': main()
