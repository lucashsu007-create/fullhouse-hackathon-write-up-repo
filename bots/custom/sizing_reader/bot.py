"""
SIZING_READER — a probe that exploits a bet-sizing tell (leak #1).
Its whole strategy facing a bet: read the SIZE, not the board. A large bet
(>= BIG x pot) is assumed to be value -> FOLD. A small/medium bet (<= SMALL x
pot) is assumed weak/merged -> FLOAT or RAISE as a bluff. Against v7_m2's
strength-coupled sizing this prints; against a single decoupled size it is
blind and should fail. Use it to measure whether FIX_SIZING closes the leak:
compare (v7_m2 vs sizing_reader) before/after the fix.
Plays a tight, non-spewy preflop so it isn't just donating.
"""
import random
BOT_NAME="sizing_reader"
try:
    import eval7; _HAVE=True; _DECK=[eval7.Card(r+s) for r in "23456789TJQKA" for s in "shdc"]
except Exception: eval7=None;_HAVE=False;_DECK=[]
_RV={r:i for i,r in enumerate("23456789TJQKA",2)}
BIG=0.62; SMALL=0.56     # size thresholds (fraction of pot) used to read strength

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
def _eq(hole,board,rng,it=300):
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
    if len(hole)!=2:return 0.4
    hi=max(_RV.get(hole[0][0],7),_RV.get(hole[1][0],7));p=hole[0][0]==hole[1][0]
    return min(0.95,0.4+0.03*(hi-7)+(0.12 if p else 0)+(0.03 if hole[0][1]==hole[1][1] else 0))
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
        if street.startswith("pre") or len(board)<3:
            eq=_eq(hole,board,rng,150)
            if eq>=0.58: 
                a,amt=_raise_to(gs,1.0)
                return {"action":a} if a=="all_in" else {"action":"raise","amount":amt}
            if owed<=0: return {"action":"check"}
            if eq>=0.5 and owed<0.2*pot: return {"action":"call"}
            return {"action":"fold"}
        # postflop: if WE can act first, just check (probe is about FACING bets)
        if owed<=0 or can: 
            eq=_eq(hole,board,rng)
            if eq>=0.75:
                a,amt=_raise_to(gs,0.6); return {"action":a} if a=="all_in" else {"action":"raise","amount":amt}
            return {"action":"check"}
        # FACING A BET: read the size
        bet_frac=owed/pot
        eq=_eq(hole,board,rng)
        if eq>=0.80: 
            a,amt=_raise_to(gs,0.9); return {"action":a} if a=="all_in" else {"action":"raise","amount":amt}
        if bet_frac>=BIG:                 # big bet = "value" -> fold unless we're strong
            if eq>=0.70: return {"action":"call"}
            return {"action":"fold"}
        if bet_frac<=SMALL:               # small bet = "weak/merge" -> attack
            if rng.random()<0.55:
                a,amt=_raise_to(gs,1.0); return {"action":a} if a=="all_in" else {"action":"raise","amount":amt}
            return {"action":"call"}
        # middling size: pot-odds call
        po=owed/(pot+owed)
        return {"action":"call"} if eq>=po else {"action":"fold"}
    except Exception:
        try:
            if gs.get("can_check") or _num(gs.get("amount_owed"))<=0: return {"action":"check"}
        except:pass
        return {"action":"fold"}
