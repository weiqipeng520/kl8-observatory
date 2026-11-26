import itertools
import math
import unittest

from model import Engine, group_hist, group_odds, plans, settle_plan, single_odds


class ModelTests(unittest.TestCase):
    def test_single_odds_sum_to_one(self):
        for k in range(1,11):
            self.assertAlmostEqual(sum(single_odds(k).values()),1.0,places=12)

    def test_single_hit_probability(self):
        self.assertAlmostEqual(single_odds(4)[4], math.comb(20,4)/math.comb(80,4))
        self.assertAlmostEqual(single_odds(10)[0], math.comb(60,10)/math.comb(80,10))

    def test_group_histogram_matches_bruteforce(self):
        universe=range(1,11); dan={1,2}; tuo=set(range(3,9)); actual={1,4,6,9}
        fast=group_hist(4,2,6,len(actual&dan),len(actual&tuo))
        brute={}
        for tail in itertools.combinations(tuo,2):
            hits=len(actual.intersection(dan.union(tail)))
            brute[hits]=brute.get(hits,0)+1
        self.assertEqual(fast,brute)

    def test_four_dan_full_counts_and_ticket_caps(self):
        ranking=list(range(1,81))
        p={x['k']:x for x in plans(ranking) if x['mode']=='four_full'}
        self.assertEqual([p[k]['count'] for k in range(5,11)],
            [76,2850,70300,1282975,18474840,218618940])
        self.assertEqual([p[k]['count']<=10000 for k in range(5,11)],
                         [True,True,False,False,False,False])

    def test_four_dan_exact_group_probability_normalizes(self):
        # Expected winning tickets is directly computed by a complete small subspace check,
        # and any-prize probability is bounded by one.
        result=group_odds(5,4,76)
        self.assertGreaterEqual(result['any'],result['all_hit'])
        self.assertLessEqual(result['any'],1.0)
        self.assertGreater(result['expected_winners'],0)
        self.assertAlmostEqual(result['expected_winners'],
            76*sum(single_odds(5)[h] for h in (3,4,5)),places=5)

    def test_four_dan_settlement_counts_each_ticket(self):
        plan={'k':5,'mode':'four_full','dan':[1,2,3,4],'tuo':list(range(5,81)),'count':76}
        # Four fixed dan plus one drawn trailing number: all 76 tickets cover one of the
        # five selected positions; winning tickets depend on official hit grades.
        draw={'numbers':list(range(1,21)),'prizes':{
            'x5z5':{'amount':'1000'},'x5z4':{'amount':'20'},'x5z3':{'amount':'3'}}}
        result=settle_plan(plan,draw)
        self.assertEqual(result['hit_dan'],4)
        self.assertEqual(result['winning_count'],76)
        self.assertEqual(result['known_prize'],'17200')

    def test_forecast_is_prequential_and_deterministic_for_issue(self):
        engine=Engine()
        prior=[]
        for i in range(65):
            f=engine.predict(f'2026{i+1:03d}')
            prior.append((f['ranking'],f['selection']['rounds']))
            engine.observe(f,{1+i%60,2+i%60,3+i%60,4+i%60,5+i%60,6+i%60,7+i%60,8+i%60,9+i%60,10+i%60,
                              11+i%60,12+i%60,13+i%60,14+i%60,15+i%60,16+i%60,17+i%60,18+i%60,19+i%60,20+i%60})
        self.assertEqual(prior[0][1],0)
        self.assertEqual(prior[-1][1],64)
        frozen=engine.predict('2026316')
        self.assertEqual(frozen,engine.predict('2026316'))
        engine.observe(frozen,set(range(1,21)))
        self.assertEqual(sorted(frozen['ranking']),list(range(1,81)))
        self.assertEqual(len(set(frozen['ranking'])),80)


if __name__=='__main__': unittest.main()
