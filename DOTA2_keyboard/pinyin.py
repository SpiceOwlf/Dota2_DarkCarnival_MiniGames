"""
拼音校验 / 纠错：游戏里的词都是拼音（大写），OCR 认错的字母往往拼不成合法拼音，
用这个约束把 BIJTA 纠正成 BIJIA、OIU 纠正成 QIU。
"""
from functools import lru_cache

# 每行：声母: 能跟它拼的韵母（"-" 表示零声母）
_TABLE = """
-: a ai an ang ao e ei en eng er o ou
b: a ai an ang ao ei en eng i ian iao ie in ing o u
p: a ai an ang ao ei en eng i ian iao ie in ing o ou u
m: a ai an ang ao e ei en eng i ian iao ie in ing iu o ou u
f: a an ang ei en eng o ou u
d: a ai an ang ao e ei en eng i ia ian iao ie ing iu ong ou u uan ui un uo
t: a ai an ang ao e eng i ian iao ie ing ong ou u uan ui un uo
n: a ai an ang ao e ei en eng i ian iang iao ie in ing iu ong ou u uan uo v ve ue
l: a ai an ang ao e ei eng i ia ian iang iao ie in ing iu o ong ou u uan un uo v ve ue
g: a ai an ang ao e ei en eng ong ou u ua uai uan uang ui un uo
k: a ai an ang ao e ei en eng ong ou u ua uai uan uang ui un uo
h: a ai an ang ao e ei en eng ong ou u ua uai uan uang ui un uo
j: i ia ian iang iao ie in ing iong iu u uan ue un
q: i ia ian iang iao ie in ing iong iu u uan ue un
x: i ia ian iang iao ie in ing iong iu u uan ue un
zh: a ai an ang ao e ei en eng i ong ou u ua uai uan uang ui un uo
ch: a ai an ang ao e en eng i ong ou u ua uai uan uang ui un uo
sh: a ai an ang ao e ei en eng i ou u ua uai uan uang ui un uo
r: an ang ao e en eng i ong ou u ua uan ui un uo
z: a ai an ang ao e ei en eng i ong ou u uan ui un uo
c: a ai an ang ao e en eng i ong ou u uan ui un uo
s: a ai an ang ao e en eng i ong ou u uan ui un uo
y: a an ang ao e i in ing o ong ou u uan ue un
w: a ai an ang ei en eng o u
"""


def _build():
    syl = set()
    for line in _TABLE.strip().splitlines():
        ini, finals = line.split(":")
        ini = "" if ini.strip() == "-" else ini.strip()
        for f in finals.split():
            syl.add((ini + f).upper())
    return syl


SYLLABLES = _build()
MAX_SYL = max(len(s) for s in SYLLABLES)
# 音节的后半截（词可能已经打了一部分，看到的是从音节中间开始的剩余部分）
SUFFIXES = {s[i:] for s in SYLLABLES for i in range(1, len(s))}


@lru_cache(maxsize=4096)
def valid(s):
    """s 能不能完整拆成拼音音节"""
    if not s:
        return True
    for k in range(1, min(MAX_SYL, len(s)) + 1):
        if s[:k] in SYLLABLES and valid(s[k:]):
            return True
    return False


@lru_cache(maxsize=4096)
def valid_tail(s):
    """s 是不是某个合法拼音串的后半段（允许从音节中间开始）"""
    if valid(s):
        return True
    for k in range(1, min(MAX_SYL, len(s)) + 1):
        if s[:k] in SUFFIXES and valid(s[k:]):
            return True
    return False


# OCR 常见的认错：每个字母可能被认成的其他字母
CONFUSE = {
    "O": "QDCGU", "Q": "OG", "D": "O", "C": "GO", "G": "CO",
    "I": "JLT", "J": "I", "L": "I", "T": "IJY",
    "U": "VO", "V": "UY", "M": "NWH", "N": "MH", "W": "MV", "H": "N",
    "E": "F", "F": "E", "B": "E", "P": "R", "R": "P",
    "K": "X", "X": "K", "Y": "VTX",
}


def correct(s, max_subs=2):
    """返回 (纠正后的串, 改了几个字母)。合法就原样返回；纠不动也原样返回（可能是英文词）。"""
    if not s or valid_tail(s):
        return s, 0
    frontier = [s]
    seen = {s}
    for subs in range(1, max_subs + 1):
        nxt = []
        for cur in frontier:
            for i, ch in enumerate(cur):
                if cur[i] != s[i]:
                    continue  # 每个位置只改一次
                for alt in CONFUSE.get(ch, ""):
                    cand = cur[:i] + alt + cur[i + 1:]
                    if cand in seen:
                        continue
                    seen.add(cand)
                    if valid_tail(cand):
                        return cand, subs
                    nxt.append(cand)
        frontier = nxt
    return s, 0


if __name__ == "__main__":
    for w in ["XUANWO", "UANWO", "BIJTA", "UAXINOIUSUIPIAN", "SHENGFU", "IGU", "LINGXIA", "PAKE"]:
        print(w, valid_tail(w), correct(w))
