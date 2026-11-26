import unittest
from datetime import datetime
from urllib.error import HTTPError
from unittest.mock import patch

import app
import official_backup as backup
import pages


def listing(*cases):
    result=[]
    for issue,day,numbers in cases:
        spans=''.join(f'<span class="lottery-ball">{n:02}</span>' for n in numbers)
        link='https://www.cwl.gov.cn/c/'+day.replace('-','/')+'/674531.shtml'
        result.append(f'<tr><td>{issue}</td><td>{spans}</td><td><a href="{link}">公告详情</a></td></tr>')
    return '<table class="lottery-game-draw-table--kl8">'+''.join(result)+'</table>'


class BackupTests(unittest.TestCase):
    def test_list_and_announcement_agreement(self):
        numbers=list(range(1,21))
        record=backup.scrape_list(listing(('2026300','2026-11-10',numbers)))[0]
        self.assertEqual(record['numbers'],numbers)
        self.assertEqual(record['prizes'],{})
        markup='''<h1>中国福利彩票“快乐8”第2026300期开奖公告</h1><p>开奖日期：2026-11-10</p>
                 <script>var kjh = "{}".split(',')</script><table><tr><td>选一中一</td><td>1,000</td><td>4.5</td></tr></table>'''
        article=backup.announcement(markup.format(','.join(map(str,numbers))),record)
        self.assertEqual(article['prizes'],{'x1z1':{'amount':'4.5','count':1000}})
        self.assertEqual(article['raw']['_source'],'guangdong_list')
        with self.assertRaises(backup.SourceDisagreement):
            backup.announcement(markup.format(','.join(map(str,range(2,22)))),record)

    def test_update_and_partial_payout_on_primary_403(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp, patch.object(app,'DB_PATH',Path(temp)/'test.sqlite3'):
            pages.restore()
            old=app.get_round('2026316')['forecast']
            first=app.get_round('2026315')['draw']
            data=listing(('2026316','2026-11-26',old['ranking'][:20]),('2026315',first['date'],first['numbers']))
            issue_clock=datetime.fromisoformat('2026-11-26T21:55:00+08:00')
            denied=HTTPError(app.API,403,'Forbidden',None,None)
            with patch.object(app,'now',return_value=issue_clock), patch.object(app,'fetch_official',side_effect=denied) as primary, \
                 patch.object(backup,'html',side_effect=[data,denied]) as regional:
                status=pages.sync()
            self.assertEqual(primary.call_count,1)
            self.assertEqual(regional.call_count,2)
            self.assertEqual(status['status'],'backup')
            self.assertEqual(status['latest'],'2026316')
            self.assertEqual(status['primary_error'],'HTTP 403')
            self.assertEqual(status['prize_pending_count'],1)
            result=app.get_round('2026316')
            self.assertEqual(result['forecast'],old)
            self.assertEqual(result['settlement']['top4_hits'],4)
            play=next(p for p in result['settlement']['details'] if p['mode']=='four_full' and p['k']==5)
            self.assertEqual(play['winning_count'],76)
            self.assertEqual(play['unknown_prize_count'],76)
            self.assertEqual(play['net_if_known'],None)
            self.assertEqual(app.get_round('2026315')['draw'],first)


if __name__=='__main__': unittest.main()
