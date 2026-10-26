"""
Dota 2「机关人攻击」打字机器人

依赖（只需一次）：
    pip install mss opencv-python numpy
    另外需要 Tesseract-OCR（默认路径 C:\\Program Files\\Tesseract-OCR\\tesseract.exe）

用法：
    python calibrate.py                 # 框选游戏画面，生成 config.json
    python bot.py --preview             # 只看识别效果，不按键
    python bot.py                       # 正式运行：F8 开始/暂停，F12 退出
    python bot.py --replay 视频.mp4     # 用录像离线测试识别
"""
import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

import letters
import pinyin

IS_WIN = sys.platform == "win32"
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
DEBUG_DIR = os.path.join(HERE, "debug")

# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
PROC_W = 960            # 识别前把游戏画面缩放到这个宽度（和录像一致，下面的像素阈值都按这个尺寸）
LETTER_H = (22, 48)     # 单词字母的高度范围（像素）；分数小数字、倍率等更小，会被滤掉
WHITE_MIN = 185         # 没打的字母：白色，RGB 最小值要大于它
WHITE_SPREAD = 45       # ……并且 RGB 最大最小差小于它（不偏色）
WEAK_MIN = 140          # 连着亮像素的稍暗像素（压缩/缩放后变细的笔画）
WEAK_SPREAD = 35
WORD_GAP = 0.75         # 同一个词里字母间距 < 字高 × 它
PHRASE_GAP = 2.0        # 词组里空格 < 字高 × 它（右边那段开头不是黄字母时，才当成同一个词）

KEY_HOLD = 0.008        # 每个键按下多久
KEY_GAP = 0.012         # 两个键之间隔多久
SETTLE = 0.10           # 打完一串后，等这么久再看屏幕（等游戏刷新画面）
GONE_SEC = 0.12         # 目标词消失这么久 => 认为打完了
STUCK_BRUTE = 2         # 连续这么多次打完词还在、也没进展 => 26 个字母挨个按一遍（打错不扣分）
STUCK_GIVEUP = 4        # ……还不行 => 先跳过这个词 1.5 秒
BATCH_WORDS = 4         # 一串最多连着打几个词
BATCH_LETTERS = 40      # ……最多多少个字母
HIDE_KEEP = 2.0         # 词被别的锁定词“压暗”时，记住它这么久
ONLY_DOTA_FOCUSED = True  # 只有 Dota 2 窗口在前台时才按键

TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
LETTERS = letters.Letters(os.path.join(HERE, "letters.npz"))   # 字母模板库（运行中自动学习）
VK_F8, VK_F12 = 0x77, 0x7B

# ---------------------------------------------------------------------------
# 键盘：SendInput + 扫描码，DirectX 游戏能收到
# ---------------------------------------------------------------------------
if IS_WIN:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    ULONG_PTR = ctypes.c_size_t

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    def _send_scan(scan, up):
        flags = 0x0008 | (0x0002 if up else 0)  # KEYEVENTF_SCANCODE (| KEYUP)
        inp = INPUT(type=1, ki=KEYBDINPUT(0, scan, flags, 0, 0))
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def scan_of(ch):
        return user32.MapVirtualKeyW(ord(ch.upper()), 0)  # MAPVK_VK_TO_VSC

    def key_pressed(vk):
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)

    def dota_focused():
        hwnd = user32.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        return "dota" in buf.value.lower()

    def set_dpi_aware():
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                user32.SetProcessDPIAware()
            except Exception:
                pass
else:  # 非 Windows：只用于离线测试
    def _send_scan(scan, up):
        pass

    def scan_of(ch):
        return 0

    def key_pressed(vk):
        return False

    def dota_focused():
        return True

    def set_dpi_aware():
        pass


class Keys:
    def __init__(self, dry=False):
        self.dry = dry
        self.scans = {c: scan_of(c) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}

    def type(self, text):
        """依次按下 text 里的字母，返回花了多少秒"""
        t0 = time.perf_counter()
        if self.dry:
            return 0.0
        for ch in text:
            sc = self.scans.get(ch)
            if not sc:
                continue
            _send_scan(sc, up=False)
            time.sleep(KEY_HOLD)
            _send_scan(sc, up=True)
            time.sleep(KEY_GAP)
        return time.perf_counter() - t0


# ---------------------------------------------------------------------------
# 找词：白色 + 黄色字母 => 连通块 => 按行拼成词
# ---------------------------------------------------------------------------
class Word:
    """一帧里识别到的一个词（只包含还没打的字母：黄色的下一个字母 + 白色的）"""
    __slots__ = ("x0", "y0", "x1", "y1", "h", "n", "yellow_first", "crop", "boxes", "mask", "_feats")

    def __init__(self, x0, y0, x1, y1, h, n, yellow_first, crop, boxes, mask):
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1
        self.h, self.n, self.yellow_first, self.crop = h, n, yellow_first, crop
        self.boxes, self.mask, self._feats = boxes, mask, None

    def features(self):
        """每个字母的特征（模板识别用）"""
        if self._feats is None:
            self._feats = [letters.feature(self.mask[y:y + h, x:x + w]) for x, y, w, h in self.boxes]
        return self._feats


def letter_masks(img):
    """返回 (字母掩码, 黄色掩码)。双阈值：亮的像素当种子，连着种子的稍暗像素也算（补上断开的细笔画）"""
    b, g, r = cv2.split(img)
    mn = cv2.min(cv2.min(b, g), r)
    spread = cv2.subtract(cv2.max(cv2.max(b, g), r), mn)
    rb = cv2.subtract(r, b)
    strong = ((mn > WHITE_MIN) & (spread < WHITE_SPREAD)) | ((r > 190) & (g > 180) & (b < 140) & (rb > 90))
    weak = ((mn > WEAK_MIN) & (spread < WEAK_SPREAD)) | ((r > 160) & (g > 150) & (b < 150) & (rb > 60))
    n, lab = cv2.connectedComponents(weak.astype(np.uint8), connectivity=8)
    keep = np.bincount(lab[strong], minlength=n) > 0
    keep[0] = False
    m = keep[lab]
    yellow = m & (rb > 60) & (g > 150)
    return m, yellow


def find_words(img):
    mask, yellow = letter_masks(img)
    m = mask.astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    ycount = np.bincount(lab[yellow], minlength=n)
    H, W = m.shape
    comps = []
    for i in range(1, n):
        x, y, w, h, a = st[i]
        if not (LETTER_H[0] <= h <= LETTER_H[1]) or w > 1.5 * h or a < 20:
            continue
        if x == 0 or y == 0 or x + w >= W or y + h >= H:
            continue
        if w > 8 and a > 0.6 * w * h:      # 实心的一团（光斑），不是细笔画的字母
            continue
        comps.append((x, y, w, h, i, ycount[i] > 0.5 * a))
    comps.sort()

    # 从左到右拼成词：黄色字母一定是某个词的开头
    groups = []
    for c in comps:
        x, y, w, h, i, is_y = c
        best = None
        if not is_y:
            for g in groups:
                lx, ly, lw, lh = g[-1][:4]
                gap = x - (lx + lw)
                if -2 <= gap <= WORD_GAP * lh and abs(y - ly) <= 0.25 * lh and 0.7 < h / lh < 1.45:
                    best = g
                    break
        if best is None:
            groups.append([c])
        else:
            best.append(c)

    # 带空格的词组：右边一段开头不是黄字母、和左边同一行且挨得近 => 合并
    groups.sort(key=lambda g: g[0][0])
    merged = []
    for g in groups:
        if not g[0][5]:
            for p in merged:
                lx, ly, lw, lh = p[-1][:4]
                gap = g[0][0] - (lx + lw)
                if 0 <= gap <= PHRASE_GAP * lh and abs(g[0][1] - ly) <= 0.25 * lh and 0.7 < g[0][3] / lh < 1.45:
                    p.extend(g)
                    break
            else:
                merged.append(g)
            continue
        merged.append(g)

    words = []
    for g in merged:
        if len(g) < 2 and not g[0][5]:
            continue                      # 单个白色块：鼠标、碎屑……；单个黄字母可能是词的最后一个字母
        x0 = min(c[0] for c in g)
        y0 = min(c[1] for c in g)
        x1 = max(c[0] + c[2] for c in g)
        y1 = max(c[1] + c[3] for c in g)
        h = int(np.median([c[3] for c in g]))
        # OCR 用框内所有字母像素（包括没被算成字母的碎笔画）；左边不扩，免得带上已打的灰字母
        pad = max(2, h // 4)
        crop = mask[max(0, y0 - 2):y1 + 2, x0:min(W, x1 + pad)]
        boxes = [c[:4] for c in g]
        words.append(Word(x0, y0, x1, y1, h, len(g), g[0][5], crop, boxes, mask))
    return words


# ---------------------------------------------------------------------------
# OCR：调用 tesseract.exe（多个词并行）
# ---------------------------------------------------------------------------
_pool = ThreadPoolExecutor(max_workers=4)


def _prep(crop, h):
    img = np.where(crop, 0, 255).astype(np.uint8)          # 黑字白底
    s = 50.0 / max(1, h)
    img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)
    img = cv2.threshold(img, 128, 255, cv2.THRESH_BINARY)[1]
    return cv2.copyMakeBorder(img, 25, 25, 25, 25, cv2.BORDER_CONSTANT, value=255)


def _tess(img):
    if not os.path.exists(TESSERACT):
        return ""
    ok, png = cv2.imencode(".png", img)
    flags = 0x08000000 if IS_WIN else 0                       # CREATE_NO_WINDOW
    r = subprocess.run([TESSERACT, "stdin", "stdout", "--psm", "7",
                        "-c", "tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                        "-c", "load_system_dawg=0", "-c", "load_freq_dawg=0"],
                       input=png.tobytes(), capture_output=True, creationflags=flags)
    return "".join(ch for ch in r.stdout.decode("ascii", "ignore").upper() if "A" <= ch <= "Z")


def ocr_words(words):
    """返回 [(纠正后, 原始, 改了几个字母)]"""
    raws = list(_pool.map(lambda w: _tess(_prep(w.crop, w.h)), words))
    out = []
    for raw in raws:
        fixed, subs = pinyin.correct(raw)
        out.append((fixed, raw, subs))
    return out


# ---------------------------------------------------------------------------
# 跟踪：同一个词在多帧之间对上号（主要看右边缘，因为打字时左边的字母会变灰消失）
# ---------------------------------------------------------------------------
class Track:
    _next = 1

    def __init__(self, w, t):
        self.id = Track._next
        Track._next += 1
        self.first_seen = t
        self.text = None          # 识别出的字母（识别时还没打的那部分）
        self.raw = ""
        self.ocr_tries = 0
        self.next_ocr = 0.0       # 认不准时，隔一会儿再认
        self.seen = 0             # 被看到了几帧
        self.vx = self.vy = 0.0
        self.dead = False
        self.skip_until = -1e9    # 怎么都打不掉的词，暂时跳过
        self.src = ""             # 识别方式：模板 / OCR
        self.learn = None         # (每个字母的特征, 文字)：一次打掉就说明认对了，加进模板库
        self.sends = 0            # 打过几串
        self.fails = 0            # 连续几串没进展
        self.progress = False     # 确认已经打进去至少一个字母（= 已锁定）
        self.word = None
        self.last_seen = t
        self.update(w, t)

    def update(self, w, t):
        if self.word is not None:
            dt = t - self.last_seen
            if 0 < dt < 0.3:
                a = 0.5
                self.vx = (1 - a) * self.vx + a * (w.x1 - self.word.x1) / dt
                self.vy = (1 - a) * self.vy + a * (w.y0 - self.word.y0) / dt
        self.word = w
        self.n = w.n
        self.seen += 1
        self.last_seen = t

    def predict(self, t):
        dt = min(0.5, t - self.last_seen)
        return self.word.x1 + self.vx * dt, self.word.y0 + self.vy * dt


class Tracker:
    def __init__(self, log=None):
        self.tracks = []
        self.log = log

    def update(self, words, t):
        live = [tr for tr in self.tracks if not tr.dead and t - tr.last_seen < HIDE_KEEP]
        pairs = []
        for wi, w in enumerate(words):
            for tr in live:
                gap = t - tr.last_seen
                px, py = tr.predict(t)
                dx, dy = abs(w.x1 - px), abs(w.y0 - py)
                if dx > 30 + 150 * gap or dy > 15 + 150 * gap:
                    continue
                if abs(w.h - tr.word.h) > 0.3 * tr.word.h:
                    continue
                if w.n == 1 and not (tr.text and len(tr.text) <= 3):
                    continue                    # 单个黄字母只用来接上快打完的词
                if gap > 0.15 and tr.text and abs(w.n - len(tr.text)) > 1:
                    continue                    # 被压暗后重新出现：字母数应该没变
                pairs.append((dx + dy, wi, tr))
        pairs.sort(key=lambda p: p[0])
        used_w, used_t = set(), set()
        for d, wi, tr in pairs:
            if wi in used_w or tr.id in used_t:
                continue
            used_w.add(wi)
            used_t.add(tr.id)
            tr.update(words[wi], t)
        for wi, w in enumerate(words):
            if wi not in used_w and w.n >= 2 and w.yellow_first:   # 新词的第一个字母一定是黄色的
                live.append(Track(w, t))
        self.tracks = live
        return [tr for tr in live if tr.last_seen == t]

    def read_templates(self, tr):
        """先用字母模板认；认得出、拼得成拼音就直接用"""
        s, conf = LETTERS.read(tr.word.features())
        if conf > 0 and pinyin.valid_tail(s):
            tr.text, tr.raw, tr.src = s, s, "模板"
            tr.learn = (tr.word.features(), s)
            return True
        return False

    def run_ocr(self, visible, t):
        """给还没识别的新词做识别（连续看到 2 帧以上才认，避免认到正在淡入的字）"""
        todo = [tr for tr in visible if tr.text is None and tr.seen >= 2 and tr.ocr_tries < 4
                and t >= tr.next_ocr]
        tess = []
        for tr in todo:
            if self.read_templates(tr):
                if self.log:
                    self.log(f"  新词 #{tr.id} {tr.text}")
            else:
                tess.append(tr)
        if not tess:
            return
        for tr, (fixed, raw, subs) in zip(tess, ocr_words([tr.word for tr in tess])):
            tr.ocr_tries += 1
            tr.next_ocr = t + 0.1
            good = fixed and pinyin.valid_tail(fixed) and abs(len(fixed) - tr.word.n) <= 1
            if not good and tr.ocr_tries < 3:
                continue                         # 不像拼音 / 字母数差太多：下一帧再认
            if not fixed:
                continue
            tr.text, tr.raw, tr.src = fixed, raw, "OCR"
            tr.learn = (tr.word.features(), fixed)
            if self.log:
                note = f"（原始 {raw}，纠正 {subs} 个字母）" if subs else ""
                warn = "" if good else f"  ⚠ 可能认错（看到 {tr.word.n} 个字母）"
                self.log(f"  新词 #{tr.id} {fixed}  [OCR]{note}{warn}")

    def reocr(self, tr):
        """重新识别一个词现在还没打的部分"""
        tr.learn = None
        if self.read_templates(tr):
            return tr.text
        fixed, raw, subs = ocr_words([tr.word])[0]
        if fixed:
            tr.text, tr.raw, tr.src = fixed, raw, "OCR"
        return fixed

    def get(self, tid):
        for tr in self.tracks:
            if tr.id == tid:
                return tr
        return None


# ---------------------------------------------------------------------------
# 打字决策
#
# 关键：打错不扣分，游戏只在按对“下一个字母”时前进。所以把整个词（或剩下的部分）
# 一口气打出去总是安全的——还没打的部分一定是整个词的后缀，多按的字母都被忽略。
# 同理可以把好几个词连成一串打：前一个词没打掉的话，后面的字母只是打错，不会出问题。
# 打完一串后词还在 => 重新识别剩下的字母再打；还不行 => 26 个字母挨个按一遍。
# ---------------------------------------------------------------------------
BRUTE_ORDER = "AEIOUNGHSZCXYWLDJQTBMKRFPV"


class Typer:
    def __init__(self, keys, log, snaps):
        self.keys, self.log, self.snaps = keys, log, snaps
        self.batch = []           # 上一串打了哪些词：[(词, 打之前剩几个字母, 是否只按了首字母)]
        self.wait_until = 0.0
        self.done_words = 0
        self.done_letters = 0
        self.focus_warned = False

    def reset(self):
        self.batch = []

    def priority(self, tr):
        return tr.first_seen                     # 出现得越早越危险，先打

    def check_batch(self, t, tracker, vis_ids, frame_vis):
        """上一串打完了：消失的词算打掉；还在的第一个词重新识别剩下的字母。返回要接着打的词"""
        for tr, _, probe in self.batch:
            if tr.id not in vis_ids and not probe and t - tr.last_seen < GONE_SEC:
                return False                     # 可能只是闪了一下，再等一帧
        focus = None
        for tr, prev_len, probe in self.batch:
            if tr.id not in vis_ids:
                if probe:
                    continue                     # 只按了首字母，结果锁定了别的词，这个词被压暗了
                self.done_words += 1
                self.done_letters += len(tr.text or "")
                self.log(f"✓ #{tr.id} {tr.text}  {t - tr.first_seen:.2f}s  打了 {tr.sends} 次")
                if tr.sends == 1 and tr.learn and LETTERS.add(*tr.learn):
                    if LETTERS.added % 10 == 0:
                        LETTERS.save()
                tr.dead = True
            elif focus is None:
                left = tracker.reocr(tr)
                if left and len(left) < prev_len:
                    tr.fails, tr.progress = 0, True
                else:
                    tr.fails += 1
                    self.log(f"  #{tr.id} 没打掉，剩下识别为 {left or '?'}（第 {tr.fails} 次）")
                    if self.snaps and frame_vis is not None:
                        self.snaps.save(frame_vis, f"stuck_{tr.id}_{left or 'x'}")
                if tr.fails >= STUCK_GIVEUP:
                    self.log(f"  #{tr.id} 放弃，先打别的")
                    tr.skip_until, tr.fails = t + 1.5, 0
                else:
                    focus = tr
        self.batch = []
        return focus

    def step(self, t, tracker, visible, frame_vis=None):
        if t < self.wait_until:
            return
        vis_ids = {tr.id for tr in visible}
        focus = None
        if self.batch:
            focus = self.check_batch(t, tracker, vis_ids, frame_vis)
            if focus is False:
                return

        cands = [tr for tr in visible if tr.text and tr.skip_until < t and not tr.dead]
        if focus is not None and focus.fails >= STUCK_BRUTE:
            rem = focus.text
            seq = "".join(dict.fromkeys(pinyin.CONFUSE.get(rem[0], "") + BRUTE_ORDER)) + rem[1:]
            batch = [(focus, len(rem), False)]
            self.log(f"  #{focus.id} 26 个字母挨个试")
        else:
            order = ([focus] if focus is not None else []) + \
                sorted((tr for tr in cands if tr is not focus), key=self.priority)
            seq, batch = "", []
            for tr in order:
                if len(batch) >= BATCH_WORDS or len(seq) >= BATCH_LETTERS:
                    break
                rem = tr.text
                # 还没锁定的词：别的（还没排进这一串的）词也是同一个字母开头时，先只按首字母，看锁定的是谁
                queued = {b[0].id for b in batch}
                if not tr.progress and any(o is not tr and o.id not in queued and o.text[0] == rem[0] for o in cands):
                    if not batch:
                        seq, batch = rem[0], [(tr, len(rem), True)]
                    break
                seq += rem
                batch.append((tr, len(rem), False))
        if not seq:
            return
        if ONLY_DOTA_FOCUSED and not self.keys.dry and not dota_focused():
            if not self.focus_warned:
                self.log("Dota 2 不在前台，暂不按键")
                self.focus_warned = True
            return
        self.focus_warned = False
        self.keys.type(seq)
        for tr, _, _ in batch:
            tr.sends += 1
        self.batch = batch
        self.wait_until = time.perf_counter() + SETTLE


# ---------------------------------------------------------------------------
# 画面来源
# ---------------------------------------------------------------------------
def to_proc(frame):
    h, w = frame.shape[:2]
    if w == PROC_W:
        return frame
    s = PROC_W / w
    return cv2.resize(frame, (PROC_W, int(round(h * s))), interpolation=cv2.INTER_AREA)


class ScreenSource:
    def __init__(self, region):
        import mss
        self.sct = (getattr(mss, "MSS", None) or mss.mss)()
        self.region = region

    def grab(self):
        return np.asarray(self.sct.grab(self.region))[:, :, :3], time.perf_counter()


class VideoSource:
    def __init__(self, path):
        self.cap = cv2.VideoCapture(path)
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30
        self.i = 0

    def grab(self):
        ok, f = self.cap.read()
        if not ok:
            return None, None
        t = self.i / self.fps
        self.i += 1
        return f, t


# ---------------------------------------------------------------------------
# 调试
# ---------------------------------------------------------------------------
def draw_overlay(img, visible, target_ids=(), status=""):
    """红框 = 正在打的词，绿框 = 已认出的词，橙框 = 还没认出"""
    vis = img.copy()
    for tr in visible:
        w = tr.word
        color = (0, 0, 255) if tr.id in target_ids else ((0, 255, 0) if tr.text else (0, 200, 255))
        cv2.rectangle(vis, (w.x0 - 3, w.y0 - 3), (w.x1 + 3, w.y1 + 3), color, 2)
        label = f"#{tr.id} {tr.text or '?'} n={tr.n}"
        cv2.putText(vis, label, (w.x0, w.y1 + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)
    if status:
        cv2.putText(vis, status, (10, vis.shape[0] - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
    return vis


class Snapshots:
    def __init__(self, enabled=True, keep=40):
        self.enabled, self.keep = enabled, keep

    def save(self, img, tag):
        if not self.enabled:
            return
        os.makedirs(DEBUG_DIR, exist_ok=True)
        name = time.strftime("%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}_{tag}.jpg"
        cv2.imwrite(os.path.join(DEBUG_DIR, name), img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        files = sorted(os.listdir(DEBUG_DIR))
        for f in files[:-self.keep]:
            try:
                os.remove(os.path.join(DEBUG_DIR, f))
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
def run(args):
    set_dpi_aware()
    if not os.path.exists(TESSERACT):
        print(f"⚠ 找不到 Tesseract：{TESSERACT}\n  只用字母模板识别（认不出的词会多试几次）。"
              "想更准就安装 Tesseract-OCR，或修改 bot.py 顶部的 TESSERACT 路径。")
    if len(LETTERS) == 0:
        print("⚠ 没有 letters.npz（字母模板库），全部用 Tesseract 识别，会慢一些。")
    if args.replay:
        src = VideoSource(args.replay)
    else:
        if not os.path.exists(CONFIG):
            print("找不到 config.json，请先运行: python calibrate.py")
            sys.exit(1)
        with open(CONFIG, encoding="utf-8") as f:
            region = json.load(f)
        src = ScreenSource({k: int(region[k]) for k in ("left", "top", "width", "height")})
        print(f"游戏区域: {region}")

    logf = None if args.replay else open(os.path.join(HERE, "bot_log.txt"), "a", encoding="utf-8")

    def log(msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line)
        if logf:
            logf.write(line + "\n")
            logf.flush()

    dry = args.preview or bool(args.replay)
    keys = Keys(dry=dry)
    tracker = Tracker(log)
    snaps = Snapshots(enabled=not args.no_debug and not args.replay)
    typer = Typer(keys, log, snaps)
    show = args.preview or args.show
    running = False          # 预览 / 回放只识别，不打字
    if not dry:
        log("准备就绪。切到 Dota 2 窗口，按 F8 开始 / 暂停，F12 退出。")
    f8_prev = False
    frames, fps_t0 = 0, time.perf_counter()

    try:
        while True:
            if key_pressed(VK_F12):
                log("F12：退出")
                break
            f8 = key_pressed(VK_F8)
            if f8 and not f8_prev and not dry:
                running = not running
                typer.reset()
                log("开始运行" if running else "已暂停")
            f8_prev = f8

            frame, t = src.grab()
            if frame is None:
                break
            img = to_proc(frame)
            words = find_words(img)
            visible = tracker.update(words, t)
            tracker.run_ocr(visible, t)
            vis = None
            if show or (snaps.enabled and running):
                vis = draw_overlay(img, visible, {b[0].id for b in typer.batch})
            if running:
                typer.step(t, tracker, visible, vis)

            frames += 1
            now = time.perf_counter()
            if show:
                cv2.imshow("typer (Esc to close)", vis)
                if cv2.waitKey(1) & 0xFF == 27:
                    break
            if not args.replay and now - fps_t0 > 10:
                log(f"  {frames / (now - fps_t0):.0f} fps，已打完 {typer.done_words} 个词 / {typer.done_letters} 个字母")
                frames, fps_t0 = 0, now
            if not running and not args.replay:
                time.sleep(0.02)
    finally:
        cv2.destroyAllWindows()
        if LETTERS.added:
            LETTERS.save()
            log(f"字母模板库已保存（{len(LETTERS)} 个样本，新学了 {LETTERS.added} 个词）")
        if logf:
            logf.close()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true", help="只显示识别结果，不按键")
    ap.add_argument("--replay", help="用录像文件离线测试识别（不按键）")
    ap.add_argument("--show", action="store_true", help="--replay 时显示画面")
    ap.add_argument("--no-debug", action="store_true", help="不保存 debug 截图")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
