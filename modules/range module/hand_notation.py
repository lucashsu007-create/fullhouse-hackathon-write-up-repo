"""
Hand notation utilities.

Converts hole cards (engine format) to canonical poker-chart notation:
    ["As", "Kh"] -> "AKo"
    ["As", "Ks"] -> "AKs"
    ["7d", "7c"] -> "77"
"""

RANKS = "23456789TJQKA"
RANK_VAL = {r: i for i, r in enumerate(RANKS)}


def canonical(cards):
    """Return canonical 'AKo' / 'AKs' / 'AA' notation from two cards.

    `cards` is a list/tuple of two strings like ["As", "Kh"].
    Card format is rank + suit, e.g. "As", "Td", "2c".
    """
    if len(cards) != 2:
        raise ValueError(f"Need exactly 2 cards, got {len(cards)}: {cards}")

    c1, c2 = cards[0], cards[1]
    r1, s1 = c1[0].upper(), c1[1].lower()
    r2, s2 = c2[0].upper(), c2[1].lower()

    if r1 not in RANK_VAL or r2 not in RANK_VAL:
        raise ValueError(f"Bad rank in {cards}")

    # Pair
    if r1 == r2:
        return r1 + r2

    # Sort so the higher rank is first
    if RANK_VAL[r1] < RANK_VAL[r2]:
        r1, r2 = r2, r1
        s1, s2 = s2, s1

    suited = "s" if s1 == s2 else "o"
    return r1 + r2 + suited


def is_pair(hand):
    """True if hand notation represents a pocket pair (e.g. 'AA', '77')."""
    return len(hand) == 2 and hand[0] == hand[1]


def is_suited(hand):
    """True if hand is a suited non-pair (e.g. 'AKs')."""
    return len(hand) == 3 and hand[2] == "s"


def all_169_hands():
    """Yield all 169 unique starting hands in standard notation."""
    for i, hi in enumerate(reversed(RANKS)):
        for j, lo in enumerate(reversed(RANKS)):
            if i == j:
                yield hi + hi
            elif i < j:
                yield hi + lo + "s"
            else:
                yield lo + hi + "o"
