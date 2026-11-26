import contextlib
from datetime import datetime
import gzip
import http.client
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app
import pages
from model import Engine, MODEL_VERSION
from test_app import raw_draw


class PagesTests(unittest.TestCase):
    def setUp(self):
        stack=contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.root=Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.db=stack.enter_context(patch.object(app,'DB_PATH',self.root/'work.sqlite3'))
        stack.enter_context(patch.object(pages,'LEDGER',self.root/'ledger'))
        stack.enter_context(patch.object(pages.time,'sleep',return_value=None))

    @staticmethod
    def extra(values=range(1,21)):
        return app.parse_draw(raw_draw('2026316','2026-11-26',list(values)))

    def sync(self, rows, time='2026-11-26T21:48:00+08:00'):
        with patch.object(app,'now',return_value=datetime.fromisoformat(time)), \
             patch.object(app,'fetch_official',return_value=(rows,2137)):
            return pages.sync()

    def test_ledger_survives_fresh_runner_and_settles_every_revision(self):
        baseline=pages.restore()
        before=app.get_round('2026316')['forecast']
        self.assertEqual(before['top4'],[35,37,61,69])
        self.assertEqual(self.sync([self.extra()])['forecast_issue'],'2026317')
        self.assertEqual(pages.persist(baseline),'2026-11-27T21:00:00+08:00')
        frozen={p.name:p.read_bytes() for p in (pages.LEDGER/'forecasts').glob('*.json')}
        with patch.object(app,'DB_PATH',self.root/'next-run.sqlite3'):
            baseline=pages.restore()
            self.assertEqual(self.sync([self.extra(range(21,41))])['status'],'ok')
            pages.persist(baseline)
            self.assertEqual(self.sync([self.extra()])['status'],'ok')
            self.assertEqual(pages.persist(baseline),'')
            self.assertEqual(frozen,{p.name:p.read_bytes() for p in (pages.LEDGER/'forecasts').glob('*.json')})
            rev=json.loads((pages.LEDGER/'audit/2026316.json').read_text())
            self.assertEqual(len(rev),3)
            self.assertEqual(rev[0]['draw_hash'],rev[2]['draw_hash'])
            self.assertNotEqual(rev[0]['draw_hash'],rev[1]['draw_hash'])
            result=app.get_round('2026316')
            self.assertEqual(before,result['forecast'])
            self.assertEqual(sum(d['derived_from_ranking'] for d in result['settlement']['details']),19)
            self.assertEqual(len(result['settlement']['details']),35)
            self.assertEqual(app.get_round('2026317')['forecast']['model_version'],MODEL_VERSION)

    def test_interrupted_response_retries_and_gap_escalates(self):
        pages.restore()
        with app.db_open(write=True) as db:
            db.execute("DELETE FROM backtests WHERE issue='2021001'")
            db.execute("DELETE FROM draws WHERE issue='2021001'")
        with gzip.open(app.SEED,'rt',encoding='utf-8') as f:
            all_rows=[app.parse_draw(row) for row in json.load(f)]
        with patch.object(app,'fetch_official',side_effect=[http.client.IncompleteRead(b''),
             ([self.extra()],2137),([self.extra(),*all_rows],2137)]) as fetch, \
             patch.object(app,'now',return_value=datetime.fromisoformat('2026-11-26T21:48:00+08:00')):
            status=pages.sync()
        self.assertEqual([args.args[0] for args in fetch.call_args_list],[False,False,True])
        self.assertEqual(status['status'],'ok')
        self.assertEqual(app.get_state()['history_count'],2137)

    def test_failure_build_keeps_old_lock_and_data(self):
        baseline=pages.restore()
        before=app.get_round('2026316')['forecast']
        with patch.object(app,'fetch_official',side_effect=http.client.IncompleteRead(b'')) as fetch, \
                patch('official_backup.fetch_recent',side_effect=OSError('offline')):
            self.assertEqual(pages.sync()['status'],'error')
        self.assertEqual(fetch.call_count,3)
        self.assertEqual(app.get_round('2026316')['forecast'],before)
        self.assertEqual(app.get_state()['history_count'],2136)
        pages.persist(baseline)
        self.assertEqual([p.stem for p in (pages.LEDGER/'forecasts').glob('*.json')],['2026316'])

    def test_capture_receipt_requires_matching_draw_and_recent_check(self):
        pages.restore()
        dt=datetime.fromisoformat('2026-11-26T21:40:00+08:00')
        with patch.object(app,'now',return_value=dt):
            fail={'status':'error','primary_error':'HTTP 403','reason':'URLError'}
            self.assertEqual(pages.use_capture(fail),fail)
            pages.capture_receipt({'status':'ok','latest':'2026315','checked_at':app.stamp(),'source':'cwl_api'})
            self.assertEqual(pages.use_capture(fail)['status'],'local')
            with app.db_open(write=True) as db: app.import_draws(db,[self.extra()])
            self.assertEqual(pages.use_capture(fail),fail)

    def test_late_start_does_not_backfill_live_lock(self):
        pages.restore()
        # Latest official result is from the day before the issue that just closed.
        res=self.sync([self.extra()],'2026-11-27T21:01:00+08:00')
        self.assertIsNone(res['forecast_issue'])
        self.assertIsNone(app.get_round('2026317'))

    def test_cold_marginals_are_bounded_and_sum_to_twenty(self):
        for n in range(81):
            engine=Engine();engine.history.extend([frozenset(range(1,21))]*n)
            for p in engine.distributions().values():
                self.assertTrue(all(0<=x<=1 for x in p))
                self.assertAlmostEqual(sum(p),20,places=12)


if __name__=='__main__': unittest.main()
