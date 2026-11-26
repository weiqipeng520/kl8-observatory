import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import app
from model import MODEL_VERSION


def raw_draw(issue, day, numbers):
    codes=[]
    for k, tiers in app.PRIZE_TABLE.items():
        for hits, amount in tiers.items():
            codes.append({'type':f'x{k}z{hits}','typenum':'1','typemoney':amount or '0'})
    return {'code':issue,'date':day,'red':','.join(f'{n:02}' for n in numbers),
            'kjhyjsx':','.join(f'{n:02}' for n in reversed(numbers)),
            'detailsLink':f'/c/{day.replace("-","/")}/999999.shtml','prizegrades':codes,
            'sales':'100000','poolmoney':'500000'}


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.old_path=app.DB_PATH
        app.DB_PATH=Path(self.tmp.name)/'test.sqlite3'
        app.init_db()
        draw=app.parse_draw(raw_draw('2026315','2026-11-25',list(range(1,21))))
        with app.db_open(write=True) as db:
            app.import_draws(db,[draw])
            rows=db.execute('SELECT * FROM draws ORDER BY issue').fetchall()
            engine=app.replay(db,rows,True)
            with patch('app.now',return_value=datetime.fromisoformat('2026-11-26T18:00:00+08:00')):
                self.issue=app.pack_forecast(db,engine,rows[-1])
        self.assertEqual(self.issue,'2026316')

    def tearDown(self):
        app.DB_PATH=self.old_path
        self.tmp.cleanup()

    def test_forecast_persists_and_cannot_be_edited_or_deleted(self):
        before=app.get_round('2026316')['forecast']
        self.assertEqual(before['top4'], sorted(before['ranking'][:4]))
        with self.assertRaises(sqlite3.IntegrityError):
            with app.db_open(write=True) as db:
                db.execute("UPDATE forecasts SET payload='{}' WHERE issue='2026316'")
        with self.assertRaises(sqlite3.IntegrityError):
            with app.db_open(write=True) as db:
                db.execute("DELETE FROM forecasts WHERE issue='2026316'")
        after=app.get_round('2026316')['forecast']
        self.assertEqual(before['integrity'],after['integrity'])
        self.assertEqual(before['top4'],after['top4'])

    def test_settlement_is_saved_and_audited_idempotently(self):
        f=app.get_round('2026316')['forecast']
        actual=f['top20']
        draw=app.parse_draw(raw_draw('2026316','2026-11-26',actual))
        with app.db_open(write=True) as db:
            app.import_draws(db,[draw])
            app.settle(db)
            app.settle(db)
        result=app.get_round('2026316')['settlement']
        self.assertEqual(result['status'],'settled')
        self.assertEqual(result['top20_hits'],20)
        self.assertTrue(result['details'])
        with app.db_open() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM settlements WHERE issue="2026316"').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM settlement_audit WHERE issue="2026316"').fetchone()[0],1)

    def test_later_draw_revision_appends_audit_record(self):
        f=app.get_round('2026316')['forecast']
        draw=app.parse_draw(raw_draw('2026316','2026-11-26',f['top20']))
        with app.db_open(write=True) as db:
            app.import_draws(db,[draw]); app.settle(db)
        revised=app.parse_draw(raw_draw('2026316','2026-11-26',list(range(21,41))))
        with app.db_open(write=True) as db:
            app.import_draws(db,[revised]); app.settle(db)
        with app.db_open() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM settlement_audit WHERE issue="2026316"').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM forecasts WHERE issue="2026316"').fetchone()[0],1)

    def test_cross_year_issue_sequence_resets(self):
        last={'issue':'2025351','draw_date':'2025-12-31'}
        with patch('app.OFFICIAL_CLOSED_2026',()), patch.dict('os.environ',{'KL8_CLOSED':''}):
            pair=app.forecast_date(last,datetime.fromisoformat('2026-01-01T10:00:00+08:00'))
        self.assertEqual(pair,("2026001",date(2026,1,1)))

    def test_read_only_http_routes_and_history_csv(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_address[1]}'
        try:
            with urlopen(base+'/api/state') as r: state=json.load(r)
            self.assertEqual(state['forecast']['issue'],'2026316')
            def visitor(_):
                with urlopen(base+'/api/state') as r: return json.load(r)['forecast']
            with ThreadPoolExecutor(max_workers=12) as pool:
                visitors=list(pool.map(visitor,range(48)))
            self.assertEqual({tuple(v['top4']) for v in visitors},{tuple(state['forecast']['top4'])})
            self.assertEqual({v['integrity'] for v in visitors},{state['forecast']['integrity']})
            with urlopen(base+'/api/history?limit=1&page=1') as r: history=json.load(r)
            self.assertEqual(history['rows'][0]['issue'],'2026315')
            with urlopen(base+'/api/round/2026316') as r: row=json.load(r)
            self.assertEqual(row['forecast']['issue'],'2026316')
            with urlopen(base+'/history.csv') as r: csv=r.read()
            self.assertIn(b'2026315',csv)
            with urlopen(base+'/') as r: html=r.read()
            self.assertIn('本期锁定预测'.encode(),html)
        finally:
            server.shutdown();thread.join(timeout=2);server.server_close()


if __name__=='__main__': unittest.main()
