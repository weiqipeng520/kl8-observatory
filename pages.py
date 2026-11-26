"""Reconstruct a temporary SQLite DB from the public Git ledger and build Pages."""
import argparse
from datetime import date
from http.client import HTTPException
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import time
import os
import hashlib

import app

ROOT = Path(__file__).resolve().parent
LEDGER = ROOT / 'ledger'


class HistoryGap(ValueError):
    pass


def json_line(obj):
    return app.dump(obj) + '\n'


def write(path, val, frozen=False):
    text = json_line(val)
    if path.exists():
        old = path.read_text(encoding='utf-8')
        if old == text:
            return
        if frozen:
            raise ValueError(f'已锁定记录不能替换: {path.name}')
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp')
    tmp.write_text(text,encoding='utf-8')
    tmp.replace(path)


def ledger_rows(name):
    for p in sorted((LEDGER/name).glob('*.json')):
        yield p.stem,json.loads(p.read_text(encoding='utf-8'))


def restore():
    app.init_db()
    app.load_seed()
    # Settle only after all stored draw corrections, forecasts and audit rows are restored.
    with app.db_open(write=True) as db:
        draws=[]
        for issue,raw in ledger_rows('draws'):
            parsed=app.parse_draw(raw)
            if parsed['issue']!=issue: raise ValueError('开奖文件名与期号不符')
            draws.append(parsed)
        app.import_draws(db,draws)
        base_raw={row['issue']:row['raw'] for row in db.execute('SELECT issue,raw FROM draws')}
        for issue,row in ledger_rows('forecasts'):
            keys=('issue','draw_date','created_at','based_on','payload','integrity')
            f=json.loads(row['payload'])
            if (row['issue']!=issue or f['issue']!=issue or f['created_at']!=row['created_at'] or
                f['history_last_issue']!=row['based_on'] or f['estimated_draw_date']!=row['draw_date'] or
                app.sha(row['payload'])!=row['integrity']):
                raise ValueError(f'锁号完整性校验失败: {issue}')
            db.execute('INSERT INTO forecasts VALUES(?,?,?,?,?,?)',tuple(row[k] for k in keys))
        for issue,events in ledger_rows('audit'):
            if not events: raise ValueError(f'验奖记录为空: {issue}')
            for r in events:
                if r['issue']!=issue or json.loads(r['payload'])['issue']!=issue:
                    raise ValueError(f'验奖期号不符: {issue}')
                args=(issue,r['settled_at'],r['draw_hash'],r['payload'])
                db.execute('INSERT INTO settlement_audit(issue,settled_at,draw_hash,payload) VALUES(?,?,?,?)',args)
            db.execute('INSERT INTO settlements VALUES(?,?,?,?)',args)
        rows=db.execute('SELECT * FROM draws ORDER BY issue').fetchall()
        app.audit(rows)
        app.replay(db,rows,False)
    app.load_release_forecast()
    return base_raw


def sync(force=False):
    """Escalate an incomplete local history to full fetch and retry transient failures."""
    full=force
    for attempt in range(3):
        try:
            records,total=app.fetch_official(full)
            with app.db_open(write=True) as db:
                changed=app.import_draws(db,records)
                rows=db.execute('SELECT * FROM draws ORDER BY issue').fetchall()
                try:
                    app.audit(rows)
                    if len(rows)!=total or rows[-1]['issue']!=records[0]['issue']:
                        raise ValueError('本地与官网的总期数或最新期不一致')
                except ValueError as exc:
                    raise HistoryGap(str(exc)) from exc
                engine=app.replay(db,rows,False)
                app.settle(db)
                issue=app.pack_forecast(db,engine,rows[-1])
                status={'status':'ok','checked_at':app.stamp(),'official_total':total,
                        'latest':rows[-1]['issue'],'forecast_issue':issue,'changed':changed,'full':full}
                app.put_meta(db,'sync',status)
            return status
        except (OSError,HTTPException,ValueError,RuntimeError,KeyError,TypeError,AttributeError) as exc:
            if isinstance(exc,HistoryGap): full=True
            if attempt<2: time.sleep(2**(attempt+1))
            else:
                status={'status':'error','checked_at':app.stamp(),
                        'error':'官网数据同步失败，保留上次已保存数据','reason':type(exc).__name__}
                with app.db_open(write=True) as db: app.put_meta(db,'sync',status)
                print(f'Official sync failed: {type(exc).__name__}: {exc}',flush=True)
                return status


def persist(base_raw):
    cutoffs=[]
    with app.db_open() as db:
        for row in db.execute('SELECT issue,raw FROM draws ORDER BY issue'):
            if base_raw.get(row['issue'])!=row['raw']:
                write(LEDGER/'draws'/f"{row['issue']}.json",json.loads(row['raw']))
        for row in db.execute('SELECT * FROM forecasts ORDER BY issue'):
            path=LEDGER/'forecasts'/f"{row['issue']}.json"
            if not path.exists() and row['issue']!=release_issue():
                cutoffs.append(json.loads(row['payload'])['cutoff_at'])
            write(path,dict(row),frozen=True)
        by_issue={}
        for row in db.execute('SELECT issue,settled_at,draw_hash,payload FROM settlement_audit ORDER BY id'):
            by_issue.setdefault(row['issue'],[]).append(dict(row))
        for issue,events in by_issue.items():
            path=LEDGER/'audit'/f'{issue}.json'
            if path.exists():
                old=json.loads(path.read_text(encoding='utf-8'))
                if events[:len(old)]!=old: raise ValueError(f'不能重写验奖修订历史: {issue}')
            write(path,events)
    return min(cutoffs,default='')


def release_issue():
    # This snapshot was generated before the target draw, independently of this build.
    return '2026316'


def build(output):
    output.mkdir(parents=True,exist_ok=True)
    for name in ('index.html','style.css','extras.css','app.js'):
        shutil.copyfile(ROOT/'static'/name,output/name)
    (output/'.nojekyll').touch()
    data=output/'data'; data.mkdir(exist_ok=True)
    h=hashlib.sha256()
    with app.db_open() as db:
        draw_rows=db.execute('SELECT * FROM draws ORDER BY issue DESC').fetchall()
        issues=[r['issue'] for r in db.execute('SELECT issue FROM draws UNION SELECT issue FROM forecasts ORDER BY issue')]
        history=[app.unpack(r) for r in draw_rows]
    write(data/'history.json',history)
    h.update(json_line(history).encode())
    for issue in issues:
        row=app.get_round(issue)
        write(data/'rounds'/f'{issue}.json',row)
        h.update(json_line(row).encode())
    state=app.get_state()
    state['data_revision']=h.hexdigest()
    state['built_at']=app.stamp()
    # The clock is calculated in the browser. Only this timestamp refers to the build.
    state.pop('server_time',None)
    write(data/'state.json',state)
    (output/'history.csv').write_bytes(app.export_csv())
    return state


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--offline',action='store_true',help='Build from ledger and bundled official seed; no network')
    p.add_argument('--full',action='store_true',help='Fetch and validate all historical issues')
    p.add_argument('--persist',action='store_true',help='Write ledger files (does not commit or push)')
    p.add_argument('--output',type=Path,default=ROOT/'_site')
    a=p.parse_args()
    with TemporaryDirectory(prefix='kl8-pages-') as temp:
        app.DB_PATH=Path(temp)/'work.sqlite3'
        baseline=restore()
        result={} if a.offline else sync(a.full)
        if a.offline:
            with app.db_open(write=True) as db:
                app.put_meta(db,'sync',{'status':'seed','checked_at':None})
        # Generate before persisting, but the release workflow will not upload it
        # unless the locks have been committed and pushed successfully.
        public=build(a.output)
        cutoff=persist(baseline) if a.persist else ''
        print(app.dump({'latest':public['latest_draw']['issue'],'issues':public['history_count'],
                        'forecast':public['forecast']['issue'] if public['forecast'] else None,'sync':result}))
        if os.environ.get('GITHUB_OUTPUT'):
            with Path(os.environ['GITHUB_OUTPUT']).open('a',encoding='utf-8') as out:
                out.write(f'new_lock_cutoff={cutoff}\nsync_status={result.get("status","offline")}\n')


if __name__=='__main__': main()
