"""Exact 80-choose-20 combinatorics and prequential forecasting; no network."""
from collections import deque
from decimal import Decimal
from functools import lru_cache
from hashlib import sha256
from math import comb, sqrt

BALLS = tuple(range(1, 81))
MODEL_VERSION = 'prequential-3'
BASE = 'uniform'
METHODS = (BASE, 'hot80', 'cold80', 'recent32')
# The published prize-grade codes also carry the official per-draw amounts.
TIERS = {1:(1,), 2:(2,), 3:(2,3), 4:(2,3,4), 5:(3,4,5), 6:(3,4,5,6),
         7:(0,4,5,6,7), 8:(0,4,5,6,7,8), 9:(0,4,5,6,7,8,9), 10:(0,5,6,7,8,9,10)}

def c(n, k):
    return comb(n, k) if 0 <= k <= n else 0

@lru_cache(maxsize=256)
def single_odds(k):
    den = comb(80, k)
    return {h: c(20, h)*c(60, k-h)/den for h in range(k+1)}

def group_hist(k, n_dan, n_tuo, hit_dan, hit_tuo):
    """Number of single bets per total hit count; never enumerate tickets."""
    take = k-n_dan
    return {hit_dan+i: c(hit_tuo,i)*c(n_tuo-hit_tuo,take-i)
            for i in range(take+1) if c(hit_tuo,i)*c(n_tuo-hit_tuo,take-i)}

@lru_cache(maxsize=128)
def group_odds(k, n_dan, n_tuo):
    """P(any prize)/P(all k)/expected winning tickets, accounting for overlap."""
    den = comb(80,20)
    any_p = all_p = exp_wins = 0
    tier_any = {h:0 for h in TIERS[k]}
    tier_wins = {h:0 for h in TIERS[k]}
    for d in range(n_dan+1):
        for t in range(min(n_tuo,20-d)+1):
            ways = c(n_dan,d)*c(n_tuo,t)*c(80-n_dan-n_tuo,20-d-t)
            if not ways:
                continue
            hist = group_hist(k,n_dan,n_tuo,d,t)
            wins = sum(hist.get(i,0) for i in TIERS[k])
            any_p += ways if wins else 0
            all_p += ways if hist.get(k,0) else 0
            exp_wins += ways*wins
            for h in TIERS[k]:
                if hist.get(h,0): tier_any[h] += ways
                tier_wins[h] += ways*hist.get(h,0)
    return {'any': any_p/den,'all_hit':all_p/den,'expected_winners':exp_wins/den,
            'tier_any':{str(h):x/den for h,x in tier_any.items()},
            'tier_expected_winners':{str(h):x/den for h,x in tier_wins.items()}}

def plans(ranking):
    ranking = list(ranking)
    dan = sorted(ranking[:4])
    tuo = sorted(set(BALLS)-set(dan))
    out = []
    for k in range(1,11):
        out.append({'k':k,'mode':'single','dan':[],'tuo':sorted(ranking[:k]),'count':1})
    for k in range(1,11):
        pool=sorted(ranking[:k+2])
        out.append({'k':k,'mode':'compound','dan':[],'tuo':pool,'count':comb(len(pool),k)})
    for k in range(2,11):
        d=min(4,k-1)
        fixed,tail=sorted(ranking[:d]),sorted(ranking[d:k+2])
        out.append({'k':k,'mode':'dantuo','dan':fixed,'tuo':tail,'count':comb(len(tail),k-d)})
    # All complements are explicitly hypothetical when exceeding official ticket limits.
    for k in range(5,11):
        out.append({'k':k,'mode':'four_full','dan':dan,'tuo':tuo,'count':comb(76,k-4)})
    return out

def settle_plan(plan, draw):
    k = plan['k']
    numbers = set(draw['numbers'])
    d, t = len(numbers.intersection(plan['dan'])), len(numbers.intersection(plan['tuo']))
    hist = group_hist(k,len(plan['dan']),len(plan['tuo']),d,t)
    matched = {h:hist[h] for h in TIERS[k] if h in hist}
    known_amount = Decimal(0)
    unresolved = 0
    for h, count in matched.items():
        prize = draw.get('prizes',{}).get(f'x{k}z{h}')
        if not prize or prize['amount'] is None or Decimal(prize['amount']) <= 0:
            unresolved += count
        else:
            known_amount += Decimal(prize['amount'])*count
    return {'k':k,'mode':plan['mode'],'hit_dan':d,'hit_tuo':t,'hit_hist':hist,
            'winning_tiers':matched,'winning_count':sum(matched.values()),
            'cost':plan['count']*2,'known_prize':format(known_amount,'f'),
            'unknown_prize_count':unresolved,
            'net_if_known':None if unresolved else format(known_amount-plan['count']*2,'f')}

def tiebreak(issue, num):
    return sha256(f'kl8-uniform-v1:{issue}:{num:02}'.encode()).digest()

class Engine:
    """Predict, then observe. Candidate selection sees ONLY earlier resolved rounds."""
    def __init__(self):
        self.history = deque(maxlen=80)
        self.scores = {m: deque(maxlen=120) for m in METHODS}

    def distributions(self):
        counts = [0]*80
        recent = [0.0]*80
        weight_sum = 0.0
        for index, nums in enumerate(reversed(self.history)):
            w = 0.5**(index/16) if index < 32 else 0.0
            weight_sum += w
            for n in nums:
                counts[n-1] += 1
                recent[n-1] += w
        # Beta-like shrinkage toward the fair 20/80 marginal. Predictions sum to 20.
        hot = [0.25 + 0.5*(n-len(self.history)*0.25)/(len(self.history)+24) for n in counts]
        # Limit the cold slope to the largest feasible uniform slope. Preserves
        # sum(p)=20 while keeping every marginal in [0, 1], even for extreme histories.
        length=len(self.history)
        slope=min(0.5,(length+24)/(3*length)) if length else 0.5
        cold = [max(0.0,0.25 - slope*(n-length*0.25)/(length+24)) for n in counts]
        fast = [0.25 + 0.5*(n-weight_sum*0.25)/(weight_sum+16) for n in recent]
        return {BASE:[0.25]*80,'hot80':hot,'cold80':cold,'recent32':fast}

    def ranking(self, issue, probs):
        return sorted(BALLS, key=lambda n:(-round(probs[n-1],12), tiebreak(issue,n)))

    def select(self):
        n = len(self.scores[BASE])
        out = {'rounds':n,'mean_brier':{},'decision':'baseline'}
        if n:
            out['mean_brier'] = {m:sum(self.scores[m])/n for m in METHODS}
        if n < 60:
            return BASE, out
        candidate = min(METHODS[1:],key=lambda m:out['mean_brier'][m])
        dif = [b-x for b,x in zip(self.scores[BASE],self.scores[candidate])]
        mu = sum(dif)/n
        se = sqrt(sum((x-mu)**2 for x in dif)/(n-1)/n)
        # Conservative selection heuristic, NOT a multiple-testing-adjusted p-value.
        if mu > max(0.0001,3*se):
            out['decision'] = 'candidate_passed_heuristic'
            return candidate,out
        return BASE,out

    def predict(self, issue):
        probs = self.distributions()
        chosen, evidence = self.select()
        rankings = {m:self.ranking(issue,p) for m,p in probs.items()}
        return {'model_version':MODEL_VERSION,'model':chosen,'selection':evidence,
                'ranking':rankings[chosen], 'probabilities':probs[chosen],
                'candidates':probs, 'candidate_rankings':rankings}

    def observe(self, forecast, numbers):
        numbers = frozenset(numbers)
        metrics = {}
        for m, probs in forecast['candidates'].items():
            rank = forecast['candidate_rankings'][m]
            brier = sum(((i+1 in numbers)-p)**2 for i,p in enumerate(probs))/80
            self.scores[m].append(brier)
            metrics[m] = {'brier':brier,'top4':len(numbers.intersection(rank[:4])),
                          'top10':len(numbers.intersection(rank[:10]))}
        self.history.append(numbers)
        return metrics

    def warm(self, draws):
        for draw in draws:
            forecast = self.predict(draw['issue'])
            self.observe(forecast,draw['numbers'])
        return self

def odds_for(plan):
    k = plan['k']
    odds = single_odds(k)
    if plan['mode']=='single':
        grades={str(h):odds[h] for h in TIERS[k]}
        return {'any':sum(odds[h] for h in TIERS[k]),'all_hit':odds[k],
                'hits':odds,'expected_winners':sum(odds[h] for h in TIERS[k]),
                'tier_any':grades,'tier_expected_winners':grades}
    return group_odds(k,len(plan['dan']),len(plan['tuo']))
