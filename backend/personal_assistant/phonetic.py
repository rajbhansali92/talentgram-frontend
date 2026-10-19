"""Personal Assistant — "sounds like" similarity for names. Pure functions, no I/O, no dependencies.

Speech recognition rarely mangles a name by a typo; it replaces it with a name that SOUNDS close ("Disha" → "Doha",
"Dhruv" → "Druv", "Sahal" → "Sahil"). Spelling similarity (difflib) misses many of these, so the resolver also compares a
rough phonetic skeleton tuned for Indian-English names: aspirates collapse (bh → b, dh → d, kh → k, th → t), sh/ch stay single
sounds, w ≈ v, z ≈ s, double letters collapse, and the comparison is a weighted edit distance in which swapping one vowel for
another or dropping a silent h is cheap, while changing a consonant is expensive.

A phonetic match is only EVER a suggestion: the resolver turns it into a card for the user to tap, never into a chosen person.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Tuple

_DIGRAPHS = (("chh", "C"), ("ch", "C"), ("sh", "S"), ("zh", "S"), ("ph", "f"), ("bh", "b"), ("dh", "d"), ("gh", "g"), ("kh", "k"),
             ("th", "t"), ("jh", "j"), ("ck", "k"), ("qu", "kv"), ("ee", "i"), ("ea", "i"), ("oo", "u"), ("ou", "u"), ("aa", "a"),
             ("ai", "e"), ("ay", "e"), ("ey", "e"), ("ie", "i"))
_SINGLE = {"w": "v", "q": "k", "c": "k", "x": "ks", "z": "s"}
_VOWELS = set("aeiou")
_GROUPS = [set("kg"), set("td"), set("pb"), set("fv"), set("szSh"), set("mn"), set("jC"), set("rl")]
PHON_MIN = 0.78                       # per-word phonetic similarity needed to be offered as "sounds like"


@lru_cache(maxsize=8192)
def key(word: str) -> str:
    """Phonetic skeleton of ONE word (lowercase letters in, symbol string out). Empty for non-words."""
    w = re.sub(r"[^a-z]", "", (word or "").lower())
    if not w:
        return ""
    for a, b in _DIGRAPHS:
        w = w.replace(a, b)
    w = "".join(_SINGLE.get(ch, ch) for ch in w)
    w = w[0] + w[1:].replace("y", "i")                                 # a y after the first letter is a vowel ("Ashley"); a leading y stays
    w = re.sub(r"(.)\1+", r"\1", w)                                    # double letters collapse
    return w


def _is_vowel(c: str) -> bool:
    return c in _VOWELS or c == "y"


def _sub(a: str, b: str) -> float:
    if a == b:
        return 0.0
    if _is_vowel(a) and _is_vowel(b):
        return 0.3
    if _is_vowel(a) != _is_vowel(b):
        return 1.0
    return 0.5 if any(a in g and b in g for g in _GROUPS) else 1.0


def _indel(c: str) -> float:
    if c == "h":
        return 0.35                    # silent / weak h ("Doha" ~ "Doa")
    return 0.5 if _is_vowel(c) else 1.0


def _first_class(c: str) -> str:
    if _is_vowel(c):
        return "V"
    for i, g in enumerate(_GROUPS):
        if c in g:
            return str(i)
    return c


def similarity(a: str, b: str) -> float:
    """0..1 phonetic similarity of two words. 1.0 = same skeleton. The first sound must be of the same kind (people rarely
    mishear the first consonant — and without this rule far too many unrelated names would qualify)."""
    ka, kb = key(a), key(b)
    if len(ka) < 3 or len(kb) < 3:
        return 0.0
    if ka == kb:
        return 1.0
    if _first_class(ka[0]) != _first_class(kb[0]):
        return 0.0
    if abs(len(ka) - len(kb)) > 2:
        return 0.0
    return round(_wdist_sim(ka, kb), 4)


def _wdist_sim(a: str, b: str) -> float:
    prev = [0.0]
    for ch in b:
        prev.append(prev[-1] + _indel(ch))
    for ca in a:
        cur = [prev[0] + _indel(ca)]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + _indel(ca), cur[j - 1] + _indel(cb), prev[j - 1] + _sub(ca, cb)))
        prev = cur
    return max(0.0, 1.0 - prev[-1] / max(len(a), len(b)))


def word_sound_match(a: str, b: str) -> Tuple[float, bool]:
    """→ (similarity, is_phonetic_only_hint). Convenience for callers that only need 'does it sound the same'."""
    s = similarity(a, b)
    return s, s >= PHON_MIN
