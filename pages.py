"""Reconstruct a temporary SQLite DB from the public Git ledger and build Pages."""
import argparse
from datetime import date
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import time
import os
import hashlib

import app
import official_backup
from urllib.error import HTTPError

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
    primary_error=None
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
                        'latest':rows[-1]['issue'],'forecast_issue':issue,'changed':changed,'full':full,
                        'source':'cwl_api'}
                app.put_meta(db,'sync',status)
            return status
        except official_backup.NET_ERRORS as exc:
            primary_error=error_reason(exc)
            print(f'Primary sync attempt {attempt+1}: {primary_error}: {exc}',flush=True)
            if isinstance(exc,HistoryGap): full=True
            if isinstance(exc,HTTPError) and exc.code in (401,403,404,429,451): break
            if attempt<2: time.sleep(2**(attempt+1))
    try:
        records, latest=official_backup.fetch_recent()
        with app.db_open(write=True) as db:
            changed=app.import_draws(db,records)
            rows=db.execute('SELECT * FROM draws ORDER BY issue').fetchall()
            app.audit(rows)
            if rows[-1]['issue']!=latest: raise HistoryGap('备用源最新一期尚未完整入库')
            engine=app.replay(db,rows,False)
            app.settle(db)
            issue=app.pack_forecast(db,engine,rows[-1])
            pending=db.execute("SELECT COUNT(*) FROM draws WHERE json_extract(raw,'$._source')='guangdong_list'").fetchone()[0]
            status={'status':'backup','checked_at':app.stamp(),'latest':latest,'forecast_issue':issue,
                    'changed':changed,'source':'guangdong','full':False,'prize_pending_count':pending,
                    'primary_error':primary_error}
            app.put_meta(db,'sync',status)
    except official_backup.NET_ERRORS as exc:
        status={'status':'error','checked_at':app.stamp(),
                'error':'官网数据同步失败，保留上次已保存数据','primary_error':primary_error,
                'reason':error_reason(exc)}
        with app.db_open(write=True) as db: app.put_meta(db,'sync',status)
        print(f'Backup sync failed: {status["reason"]}: {exc}',flush=True)
    return status


def error_reason(exc):
    return f'HTTP {exc.code}' if isinstance(exc,HTTPError) else type(exc).__name__


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


def capture_receipt(status):
    if status['status'] not in ('ok','backup'):
        raise RuntimeError('本机未取得官方数据，不能写同步凭据')
    with app.db_open() as db:
        row=db.execute('SELECT issue,raw FROM draws ORDER BY issue DESC LIMIT 1').fetchone()
    if row['issue']!=status['latest']: raise ValueError('本机同步期号与已保存数据不符')
    write(LEDGER/'local-sync.json',{'issue':row['issue'],'draw_sha256':app.sha(row['raw']),
                                   'checked_at':status['checked_at'],'source':status['source']})


def use_capture(status):
    """Cloud failure must remain visible, even when a committed local import is usable."""
    p=LEDGER/'local-sync.json'
    if status.get('status')!='error' or not p.is_file(): return status
    try:
        doc=json.loads(p.read_text(encoding='utf-8'))
        if doc['source'] not in ('cwl_api','guangdong'): return status
        from datetime import datetime, timedelta
        age=app.now()-datetime.fromisoformat(doc['checked_at'])
        if not timedelta(0)<=age<=timedelta(hours=24): return status
        with app.db_open(write=True) as db:
            row=db.execute('SELECT issue,raw FROM draws ORDER BY issue DESC LIMIT 1').fetchone()
            if row['issue']!=doc['issue'] or app.sha(row['raw'])!=doc['draw_sha256']: return status
            pending=db.execute("SELECT COUNT(*) FROM draws WHERE json_extract(raw,'$._source')='guangdong_list'").fetchone()[0]
            status={**status,'status':'local','local_checked_at':doc['checked_at'],
                    'source':doc['source'],'latest':row['issue'],'prize_pending_count':pending}
            app.put_meta(db,'sync',status)
    except (ValueError,TypeError,KeyError,AttributeError) as exc:
        print(f'Invalid local synchronization receipt: {type(exc).__name__}',flush=True)
    return status


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
    p.add_argument('--capture',action='store_true',help='Verify official data from this machine and persist it without building the site')
    p.add_argument('--output',type=Path,default=ROOT/'_site')
    a=p.parse_args()
    if a.capture and a.offline: p.error('--capture requires an online check')
    with TemporaryDirectory(prefix='kl8-pages-') as temp:
        app.DB_PATH=Path(temp)/'work.sqlite3'
        baseline=restore()
        result={} if a.offline else sync(a.full)
        if a.capture:
            if result.get('status') not in ('ok','backup'):
                print(app.dump(result)); raise SystemExit(2)
            cutoff=persist(baseline)
            capture_receipt(result)
            print(app.dump({'sync':result,'new_lock_cutoff':cutoff})); return
        result=use_capture(result)
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
