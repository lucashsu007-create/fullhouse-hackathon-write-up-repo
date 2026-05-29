"""
THIN_VALUE — a probe that exploits river over-calling (leak #4).
It plays solid but pot-controls flop/turn (check/call), then on the RIVER
value-bets a WIDE-but-ahead range at a small-ish size designed to get crying
calls from an opponent whose bluff-catcher equity is inflated by a stale range.
A bot that over-calls rivers pays this off; a bot that narrows correctly (v13
narrow) folds more. Use it to measure whether the narrowing fix reduces the
over-call leak. NOT a maniac: it never bluffs, only thin-values, so any chips it
wins are pure value extracted from light calls.
"""
import random
BOT_NAME="thin_value"
try:
    import eval7; _HAVE=True; _DECK=[eval7.Card(r+s) for r in "23456789TJQKA" for s in "shdc"]
except Exception: eval7=None;_HAVE=False;_DECK=[]
_RV={r:i for i,r in enumerate("23456789TJQKA",2)}
RIVER_THIN_LO=0.52; RIVER_THIN_HI=0.88; RIVER_SIZE=0.5    # thin-value window + size
def _num(x,d=0.0):
    try:return float(x)
    except:return float(d)
def _cards(r):
    o=[]
    for c in (r or []):
        try:
            if isinstance(c,str) and len(c)>=2 and c[0].upper() in _RV: o.append(c[0].upper()+c[1].lower())
        except:pass
    return o
def _eq(hole,board,rng,it=350):
    if _HAVE and len(hole)==2:
        try:
            h=[eval7.Card(c) for c in hole];b=[eval7.Card(c) for c in board]
            dead=set(str(c) for c in h+b);live=[c for c in _DECK if str(c) not in dead];need=5-len(b);w=t=0
            for _ in range(it):
                rng.shuffle(live);oh=live[:2];fill=live[2:2+need];full=b+fill
                my=eval7.evaluate(h+full);op=eval7.evaluate(oh+full)
                if my>op:w+=1
                elif my==op:t+=1
            return (w+t/2)/it
        except:pass
    if len(hole)!=2:return 0.45
    hi=max(_RV.get(hole[0][0],7),_RV.get(hole[1][0],7));p=hole[0][0]==hole[1][0]
    return min(0.95,0.42+0.03*(hi-7)+(0.12 if p else 0)+(0.03 if hole[0][1]==hole[1][1] else 0))
def _raise_to(state,frac):
    pot=max(1.0,_num(state.get("pot"),1));owed=max(0.0,_num(state.get("amount_owed")));cur=_num(state.get("current_bet"))
    stack=max(0.0,_num(state.get("your_stack")));minr=_num(state.get("min_raise_to"))
    target=(frac*pot) if owed<=0 else cur+frac*(pot+owed)
    my_in=max(0.0,cur-owed);target=max(target,minr,cur+1)
    if target-my_in>=stack:return ("all_in",None)
    return ("raise",int(round(target)))
def decide(gs):
    if isinstance(gs,dict) and gs.get("type")=="warmup":return {"action":"fold"}
    try:
        hole=_cards(gs.get("your_cards"));board=_cards(gs.get("community_cards"))
        owed=_num(gs.get("amount_owed"));pot=max(1.0,_num(gs.get("pot")));can=bool(gs.get("can_check"))
        rng=random.Random(hash((tuple(hole),tuple(board),int(pot)))&0xFFFFFFFF)
        street=str(gs.get("street","")).lower()
        po=owed/(pot+owed) if (pot+owed)>0 else 1.0
        if street.startswith("pre") or len(board)<3:
            eq=_eq(hole,board,rng,150)
            if eq>=0.60:
                a,amt=_raise_to(gs,1.0);return {"action":a} if a=="all_in" else {"action":"raise","amount":amt}
            if owed<=0:return {"action":"check"}
            if eq>=0.5 and owed<0.25*pot:return {"action":"call"}
            return {"action":"fold"}
        is_river=len(board)>=5
        eq=_eq(hole,board,rng)
        if not is_river:
            # flop/turn: pot-control. value-raise only the nuts; else check/call w odds.
            if eq>=0.85 and can:
                a,amt=_raise_to(gs,0.6);return {"action":a} if a=="all_in" else {"action":"raise","amount":amt}
            if owed<=0 or can:return {"action":"check"}
            return {"action":"call"} if eq>=po*0.9 else {"action":"fold"}
        # RIVER: thin value
        if owed<=0 or can:
            if RIVER_THIN_LO<=eq<=RIVER_THIN_HI:
                a,amt=_raise_to(gs,RIVER_SIZE);return {"action":a} if a=="all_in" else {"action":"raise","amount":amt}
            if eq>RIVER_THIN_HI:
                a,amt=_raise_to(gs,0.8);return {"action":a} if a=="all_in" else {"action":"raise","amount":amt}
            return {"action":"check"}
        # facing a river bet: call only with real strength (don't pay off)
        return {"action":"call"} if eq>=po+0.05 else {"action":"fold"}
    except Exception:
        try:
            if gs.get("can_check") or _num(gs.get("amount_owed"))<=0: return {"action":"check"}
        except:pass
        return {"action":"fold"}
