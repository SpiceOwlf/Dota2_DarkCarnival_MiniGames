"""
Dota 2 打砖块小游戏 —— 自动机器人 v7.1

用法（Windows，Dota 2 设为“无边框窗口”）：
    1. pip install mss opencv-python numpy
    2. python calibrate.py          # 只需做一次：框选游戏区域
    3. python bot.py --preview      # 可选：只看识别效果，不按键
    4. python bot.py                # 正式运行，切到 Dota 窗口后按 F8 开始

热键：F8 = 开始/暂停     F12 = 退出
"""
import argparse
import ctypes
import glob
import json
import os
import sys
import time
from collections import deque

import cv2
import numpy as np

IS_WIN = sys.platform == "win32"
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")

# ---------------------------------------------------------------------------
# 可调参数（坐标都是相对游戏区域的比例）
# ---------------------------------------------------------------------------
PROC_W = 420            # 识别时把游戏区缩放到这个宽度，越小越快
CATCH_ABOVE_ROOF = 0.0485  # 靴子弹起时的中心 = 马车顶 - 这个值 × 游戏区宽度（实测）
BLACK_P95 = 40          # 游戏区 95% 分位亮度低于它 => 过关黑屏
BLACK_SUSTAIN = 0.4     # 黑屏（且看不到马车）要持续这么久才算过关；真正过关黑屏约 2 秒
DEADZONE = 0.018        # 马车与目标的距离小于它就不动（游戏区宽度比例）
NO_BOOT_SEC = 0.7       # 这么久看不到飞行中的靴子 => 认为靴子停在马车上，需要发射
LOST_LIFE_DELAY = 3.5   # 掉了一条命之后，等这么久再发射（靴子要重新回到马车上，太早按会出问题）
AFTER_BLACK_SEC = 0.9   # 黑屏结束后等多久再发射（等画面淡入）
LAUNCH_COOLDOWN = 1.0   # 一轮发射结束后，等多久再开始下一轮
LAUNCH_PRESSES = 5      # 一轮发射最多按几次空格（每 0.45 秒一次，看到靴子飞出就停）
MAX_FAILED_LAUNCH = 6   # 连续这么多次发射都没看到靴子 => 自动暂停（可能游戏结束了）
LOST_SEC = 0.35         # 靴子短暂丢失时，按预测位置继续追这么久

# 瞄准（v2）
AIM = True              # False = 关掉瞄准，只管接住
AIM_K = 650.0           # 反弹角度(度) ≈ AIM_K × (靴子相对车中心的偏移 / 游戏区宽度)；运行中会自动学习修正
AIM_MAX = 28            # 最大瞄准角度（度），实测 ±30° 以内可靠
AIM_DEADZONE = 0.005    # 瞄准时马车停得更准
LOOKAHEAD = True        # 往后多看一步：考虑这一下落在哪，下一下能不能打进好位置
LOOKAHEAD_W = 0.6       # 下一下的分数打几折
LOOKAHEAD_TOP = 5       # 只对期望分最高的前几个角度做两步模拟（控制计算量）
AIM_SIGMA0 = 6.0        # 瞄准误差（度）的初始估计；运行中按实际反弹自动学习
BRAKE0 = 0.006          # 松开方向键后马车还会滑多远（宽度比例）的初始估计；运行中自动学习
AIM_MAX_OFF = 0.040     # 瞄准时马车最多偏离落点多少（游戏区宽度比例），留余量防止漏接

# 拿分策略（v5）
EXIT_PENALTY = 6.0      # 还不想过关时，飞出顶部的路线至少扣这么多分
EXIT_PENALTY_FRAC = 0.1  # ……或者扣「这一关剩下的砖总价值」的 10%（取大的）：剩得越多越不该走
GREEN_VALUE = 12.0      # 绿色砖（加一条命）的价值，约等于 2~3 块金砖
BOOT_RAD = 0.026        # 靴子半径（游戏区宽度比例），模拟碰撞用
EXIT_FRACTION = 0.25    # 剩下的砖价值低于开局的 25% => 开始找出口过关
LEVEL_TIME_CAP = 150    # 一关打了这么多秒还没过 => 开始找出口
EXIT_AFTER_MISSES = 2   # 这一关已经掉了这么多条命 => 开始找出口，保命要紧
CART_SPEED = 0.70       # 马车速度（游戏区宽度/秒），运行中会自动测量
CELL = 0.025            # 砖块地图格子大小（游戏区宽度比例）


# ---------------------------------------------------------------------------
# 键盘：用 SendInput + 扫描码，DirectX 游戏能收到
# ---------------------------------------------------------------------------
SCAN = {"left": (0x4B, True), "right": (0x4D, True), "space": (0x39, False)}
VK_F8, VK_F12 = 0x77, 0x7B

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

    def _send_scan(scan, extended, up):
        flags = 0x0008  # KEYEVENTF_SCANCODE
        if extended:
            flags |= 0x0001
        if up:
            flags |= 0x0002
        inp = INPUT(type=1, ki=KEYBDINPUT(0, scan, flags, 0, 0))
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def key_pressed(vk):
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)

    def set_dpi_aware():
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                user32.SetProcessDPIAware()
            except Exception:
                pass
else:  # 非 Windows：只用于离线测试
    def _send_scan(scan, extended, up):
        pass

    def key_pressed(vk):
        return False

    def set_dpi_aware():
        pass


class Keys:
    def __init__(self, dry=False, log=None):
        self.dry = dry
        self.held = None
        self.log = log or (lambda *a: None)

    def _send(self, name, up):
        if not self.dry:
            scan, ext = SCAN[name]
            _send_scan(scan, ext, up)

    def hold(self, name):
        """name: 'left' / 'right' / None。保持某个方向键按下，其它松开。"""
        if name == self.held:
            return
        if self.held:
            self._send(self.held, up=True)
        if name:
            self._send(name, up=False)
        self.held = name

    def tap(self, name, dur=0.06):
        self._send(name, up=False)
        time.sleep(dur)
        self._send(name, up=True)

    def release_all(self):
        self.hold(None)


# ---------------------------------------------------------------------------
# 自动找游戏区（校准时用）
# ---------------------------------------------------------------------------
def _runs(mask):
    out=[]; s=None
    for i,v in enumerate(list(mask)+[False]):
        if v and s is None: s=i
        elif not v and s is not None: out.append((s,i)); s=None
    return out

def _longest(mask, close=0):
    m=np.asarray(mask,np.uint8)
    if close>1:
        m=cv2.morphologyEx(m.reshape(-1,1),cv2.MORPH_CLOSE,np.ones((close|1,1),np.uint8)).ravel()
    rs=_runs(m>0)
    return max(rs,key=lambda t:t[1]-t[0]) if rs else None

def fit_playfield(img):
    """在大致框选的区域里找出游戏区（两根柱子之间、计分栏下方），返回 (x, y, w, h)；失败返回 None"""
    b,g,r=[c.astype(np.int16) for c in cv2.split(img)]
    h,w=r.shape
    blue=(b>r+6)&(b>=g)                                  # 深蓝背景
    gold=(r>170)&(g>110)&(b<130)&(r-b>70)                # 砖块
    strong=(b>r+12)&(b>g+5)&(b>25)
    # 列：中间段深蓝最多的连续列
    rows=_longest(strong.mean(1)>0.25, close=int(0.06*h))
    if rows is None: return None
    cols=_longest(strong[rows[0]:rows[1]].mean(0)>0.12, close=int(0.05*w))
    if cols is None: return None
    x0,x1=cols
    # 行：蓝色多 或 砖块多 的行都算游戏区；计分栏是暗红色，两者都不是
    bf=blue[:,x0:x1].mean(1); gf=gold[:,x0:x1].mean(1)
    rows=_longest((bf>0.4)|(gf>0.3), close=max(3,int(0.015*h)))
    if rows is None: return None
    y0,y1=rows
    W,H=x1-x0,y1-y0
    if W<60 or H<60 or not (1.0<H/W<1.8): return None
    return int(x0),int(y0),int(W),int(H)


# ---------------------------------------------------------------------------
# 识别
# ---------------------------------------------------------------------------
class Detector:
    def __init__(self):
        self.prev_gray = None
        s = PROC_W / 825.0                      # 参数是在 825 像素宽的游戏区上测出来的
        self.amin, self.amax = 500 * s * s, 6000 * s * s
        self.k_close = np.ones((5, 5), np.uint8)
        self.k_dil = np.ones((7, 7), np.uint8)
        self.roofs = deque(maxlen=15)
        self.rejects = deque(maxlen=8)           # 马车顶部高度（自动识别，取中位数）

    @staticmethod
    def resize(frame):
        h, w = frame.shape[:2]
        return cv2.resize(frame, (PROC_W, int(round(h * PROC_W / w))),
                          interpolation=cv2.INTER_AREA)

    def analyze(self, pf):
        """pf: 已缩放的 BGR 游戏区。返回 dict(black, cart_x, roof, catch_y, cands)"""
        h, w = pf.shape[:2]
        gray = cv2.cvtColor(pf, cv2.COLOR_BGR2GRAY)
        center = gray[int(h * .15):int(h * .85):3, int(w * .15):int(w * .85):3]
        black = float(np.percentile(center, 95)) < BLACK_P95

        b, g, r = cv2.split(pf.astype(np.int16))

        # 马车：下半部分里的红色。先找车顶高度，再用车顶那几行算横坐标（避开两边的红箭头）
        e = max(2, int(0.03 * w))
        y0 = int(h * 0.74)                     # 马车只会在最下面这一段（实测车顶在 0.84~0.87h）
        red = (r[y0:, e:w - e] > 110) & (g[y0:, e:w - e] < 95) & (b[y0:, e:w - e] < 95) \
            & (r[y0:, e:w - e] - g[y0:, e:w - e] > 55)
        cart_x = None
        merged = cv2.dilate(red.astype(np.uint8), np.ones((1, 9), np.uint8))  # 车顶中间有白色花纹，横向连起来
        n, _, st, _ = cv2.connectedComponentsWithStats(merged, connectivity=8)
        best = None
        for i in range(1, n):
            cx, cy, cw, ch, ca = st[i]
            if cw >= 0.12 * w and ch >= 0.018 * w and ca >= 0.002 * w * w:
                if best is None or ca > best[4]:           # 下部最大的一块红色 = 马车车顶
                    best = (cx, cy, cw, ch, ca)
        if best is not None:
            cx, cy, cw, ch, _ = best
            top = cy + y0
            med = float(np.median(self.roofs)) if len(self.roofs) >= 5 else None
            if med is not None and abs(top - med) > 0.06 * h:
                # 高度和马车对不上：多半是砖块。但如果连续好几次都在同一个新高度，说明是之前记错了，改用新高度
                self.rejects.append(top)
                rj = list(self.rejects)[-5:]
                if len(rj) == 5 and max(rj) - min(rj) < 0.03 * h:
                    self.roofs.clear(); self.roofs.extend(rj); self.rejects.clear()
                else:
                    best = None
            else:
                self.roofs.append(top)
                self.rejects.clear()
        if best is not None:
            cart_x = float(cx + cw / 2.0) + e
        roof = float(np.median(self.roofs)) if self.roofs else 0.86 * h
        catch_y = roof - CATCH_ABOVE_ROOF * w

        # 靴子：棕色 + 正在移动
        m = ((r > 75) & (r < 185) & (g > 45) & (g < 125) & (b > 35) & (b < 110)
             & (r - g > 18) & (g - b > 2) & (r - b > 30)).astype(np.uint8)
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, self.k_close)
        if self.prev_gray is not None and self.prev_gray.shape == gray.shape:
            mot = cv2.dilate((cv2.absdiff(gray, self.prev_gray) > 28).astype(np.uint8),
                             self.k_dil)
            m &= mot
        else:
            m[:] = 0
        self.prev_gray = gray

        cands = []
        n, _, st, cen = cv2.connectedComponentsWithStats(m, connectivity=8)
        for i in range(1, n):
            a, bw, bh = st[i, 4], st[i, 2], st[i, 3]
            if not (self.amin < a < self.amax):
                continue
            if max(bw, bh) / max(1, min(bw, bh)) > 2.6:
                continue
            if cen[i][1] > roof + 0.01 * w:
                continue
            cands.append((float(a), float(cen[i][0]), float(cen[i][1])))
        return {"black": black, "cart_x": cart_x, "roof": roof, "catch_y": catch_y,
                "cands": cands, "w": w, "h": h}


# ---------------------------------------------------------------------------
# 跟踪靴子 + 估算速度
# ---------------------------------------------------------------------------
VALID_SPEED = (0.45, 2.5)   # 靴子的飞行速度范围（游戏区宽度/秒）；实测约 0.8~1.0
STATIC_EXTENT = 0.06        # 0.25 秒内活动范围小于这个（游戏区宽度比例）=> 不是靴子（爪子、碎屑等）


class Track:
    """一个候选目标的轨迹"""
    def __init__(self, t, x, y):
        self.hist = deque([(t, x, y)], maxlen=10)   # 当前这段直线（反弹后重新开始）
        self.trail = deque([(t, x, y)], maxlen=60)  # 全部位置（跨反弹），用来判断是不是在动
        self.outliers = []
        self.vx = self.vy = 0.0
        self.last_seen = t
        self.n = 1

    def speed(self):
        return float(np.hypot(self.vx, self.vy))

    def predict(self, t):
        pts = list(self.hist)[-6:]
        if len(pts) < 3:
            tm, xm, ym = pts[-1]
        else:
            tm = sum(q[0] for q in pts) / len(pts)
            xm = sum(q[1] for q in pts) / len(pts)
            ym = sum(q[2] for q in pts) / len(pts)
        return xm + self.vx * (t - tm), ym + self.vy * (t - tm)

    def _fit(self, t):
        pts = [q for q in self.hist if t - q[0] < 0.2]
        if len(pts) >= 2:
            ts = np.array([q[0] for q in pts]); ts -= ts.mean()
            den = float((ts ** 2).sum())
            if den > 1e-9:
                self.vx = float((ts * np.array([q[1] for q in pts])).sum() / den)
                self.vy = float((ts * np.array([q[2] for q in pts])).sum() / den)

    def add(self, t, x, y, w):
        self.trail.append((t, x, y))
        self.last_seen = t
        self.n += 1
        if len(self.hist) >= 3:
            px, py = self.predict(t)
            if (x - px) ** 2 + (y - py) ** 2 > (0.025 * w) ** 2:
                # 反弹 = 连续两帧都偏离直线（单帧偏离多半是识别抖动）
                self.outliers.append((t, x, y))
                if len(self.outliers) >= 2:
                    self.hist = deque(self.outliers, maxlen=10)
                    self.outliers = []
                    self._fit(t)
                return
        self.outliers = []
        self.hist.append((t, x, y))
        self._fit(t)

    def is_static(self, t, w):
        pts = [q for q in self.trail if t - q[0] <= 0.3]
        if len(pts) < 5 or pts[-1][0] - pts[0][0] < 0.22:
            return False
        xs = [q[1] for q in pts]; ys = [q[2] for q in pts]
        return np.hypot(max(xs) - min(xs), max(ys) - min(ys)) < STATIC_EXTENT * w

    def is_valid(self, w):
        if len(self.hist) < 3 or self.hist[-1][0] - self.hist[0][0] < 0.045:
            return False
        if not (VALID_SPEED[0] * w <= self.speed() <= VALID_SPEED[1] * w):
            return False
        (t0, x0, y0), (t1, x1, y1) = self.hist[0], self.hist[-1]
        return np.hypot(x1 - x0, y1 - y0) > 0.03 * w


class Tracker:
    """同时跟踪画面里所有会动的棕色目标，只把“像靴子一样匀速飞行”的那个交给马车"""
    def __init__(self):
        self.zones = []               # 判定为“不是靴子”的位置：(x, y, 过期时间)
        self.reset()

    def reset(self):
        self.tracks = []
        self.sel = None
        self.last_seen = -1e9

    # ---- 兼容旧接口：返回当前选中的靴子轨迹的属性
    @property
    def active(self):
        return self.sel is not None

    @property
    def hist(self):
        return self.sel.hist if self.sel else deque()

    @property
    def vx(self):
        return self.sel.vx if self.sel else 0.0

    @property
    def vy(self):
        return self.sel.vy if self.sel else 0.0

    def predict(self, t):
        return self.sel.predict(t) if self.sel else None

    def _in_zone(self, x, y, t, w):
        return any(z[2] > t and (x - z[0]) ** 2 + (y - z[1]) ** 2 < (0.07 * w) ** 2
                   for z in self.zones)

    def update(self, t, cands, w):
        self.tracks = [tr for tr in self.tracks if t - tr.last_seen < LOST_SEC]
        # 已确认是假目标的位置：直接丢掉这些识别结果（并刷新有效期）；靴子路过时按预测轨迹滑过去
        unused = []
        sp = self.sel.predict(t) if self.sel is not None else None
        sg = (0.08 * w + self.sel.speed() * (t - self.sel.last_seen)) if self.sel is not None else 0
        for c in cands:
            hit = False
            if sp is not None and (c[1] - sp[0]) ** 2 + (c[2] - sp[1]) ** 2 < sg ** 2:
                unused.append(c)                 # 靴子正好飞过屏蔽区：照样跟
                continue
            for i, z in enumerate(self.zones):
                if z[2] > t and (c[1] - z[0]) ** 2 + (c[2] - z[1]) ** 2 < (0.06 * w) ** 2:
                    self.zones[i] = (z[0], z[1], t + 30.0)
                    hit = True
                    break
            if not hit:
                unused.append(c)
        order = sorted(self.tracks, key=lambda tr: (tr is not self.sel, -tr.n))
        for tr in order:
            if not unused:
                break
            px, py = tr.predict(t)
            gate = 0.12 * w + tr.speed() * (t - tr.last_seen)
            best = min(unused, key=lambda c: (c[1] - px) ** 2 + (c[2] - py) ** 2)
            if (best[1] - px) ** 2 + (best[2] - py) ** 2 < gate ** 2:
                tr.add(t, best[1], best[2], w)
                unused.remove(best)
        for c in unused:
            if len(self.tracks) >= 8:
                break
            if 0.025 * w < c[1] < 0.975 * w and not self._in_zone(c[1], c[2], t, w):
                self.tracks.append(Track(t, c[1], c[2]))
        for tr in list(self.tracks):
            if tr.is_static(t, w):
                _, x, y = tr.trail[-1]
                self.zones = [z for z in self.zones if z[2] > t][-20:]
                self.zones.append((x, y, t + 30.0))
                self.tracks.remove(tr)
                if tr is self.sel:
                    self.sel = None
        if self.sel is not None and (self.sel not in self.tracks or t - self.sel.last_seen > 0.2):
            self.sel = None
        # 选中的目标变慢了（撞完砖后盯住了碎屑/爪子）=> 放弃
        if self.sel is not None and len(self.sel.hist) >= 4 \
                and self.sel.hist[-1][0] - self.sel.hist[0][0] > 0.07 \
                and self.sel.speed() < VALID_SPEED[0] * w:
            self.sel = None
        valid = [tr for tr in self.tracks if tr.is_valid(w) and tr.last_seen == t]
        if self.sel is None or (t - self.sel.last_seen > 0.06 and valid):
            if valid:
                self.sel = max(valid, key=lambda tr: (tr.n, tr.last_seen))
        if self.sel is not None and self.sel.last_seen == t:
            self.last_seen = t
            return True
        return False


def reflect(x, lo, hi):
    span = hi - lo
    if span <= 0:
        return (lo + hi) / 2
    x = (x - lo) % (2 * span)
    return lo + (x if x < span else 2 * span - x)


def target_x(tracker, t, w, catch_y):
    p = tracker.predict(t)
    if p is None:
        return None
    x, y = p
    r = 0.028 * w                           # 靴子半径
    if tracker.vy > 20:                     # 正在下落：算落点
        tt = (catch_y - y) / tracker.vy
        if tt < 0:
            return x
        return reflect(x + tracker.vx * tt, r, w - r)
    return min(max(x, r), w - r)            # 正在上升：先跟着它的横坐标


# ---------------------------------------------------------------------------
# 砖块地图 + 瞄准
# ---------------------------------------------------------------------------
EMPTY, BRICK, DEAD = 0, 1, 2
GOLD, ORANGE, TAN = 1, 2, 3          # 砖块种类（cls 数组里的值）；DEAD 用 4，GREEN 用 5
CLS_DEAD = 4
GREEN = 5                            # 绿色砖：打掉加一条命
BRICK_VALUE = {GOLD: 5.0, ORANGE: 1.5, TAN: 1.0, GREEN: GREEN_VALUE}


class BrickMap:
    """把马车上方的区域切成小格，按颜色分类：金色 / 橙色 / 米色(可打碎) / 淡蓝(死砖块)"""
    def __init__(self):
        self.occ = None
        self.n = 0

    def reset(self):
        self.occ = None

    def update(self, pf, catch_y, force=False):
        self.n += 1
        if self.occ is not None and self.n % 4 and not force:
            return
        h, w = pf.shape[:2]
        cs = max(4, int(round(CELL * w)))
        hsv = cv2.cvtColor(pf, cv2.COLOR_BGR2HSV)
        H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        b, g, r = cv2.split(pf.astype(np.int16))
        warm = (r > 140) & (r - b > 40)
        gold = warm & (H >= 17) & (V >= 215)
        orange = warm & (H < 17) & (S >= 170)
        dead = (b > r + 25) & (b > 130) & (g > 130)
        green = (g > r + 40) & (g > b + 30) & (g > 120)
        ymax = max(0, int(catch_y - 0.06 * w))
        gw, gh = w // cs, h // cs

        def frac(m):
            m = m.astype(np.float32)
            m[ymax:] = 0
            return cv2.resize(m[:gh * cs, :gw * cs], (gw, gh), interpolation=cv2.INTER_AREA)
        fw, fg, fo, fd, fgr = frac(warm), frac(gold), frac(orange), frac(dead), frac(green)
        cls = np.zeros((gh, gw), np.uint8)
        brick = fw > 0.3
        cls[brick] = TAN
        cls[brick & (fo > 0.45 * fw)] = ORANGE
        cls[brick & (fg > 0.35 * fw)] = GOLD
        cls[fd > 0.3] = CLS_DEAD
        cls[fgr > 0.25] = GREEN
        occ = np.zeros((gh, gw), np.uint8)
        occ[(cls == GOLD) | (cls == ORANGE) | (cls == TAN) | (cls == GREEN)] = BRICK
        occ[cls == CLS_DEAD] = DEAD
        self.cls, self.occ, self.cs, self.w, self.h = cls, occ, cs, w, h
        self.grid = cls.tolist()
        self.gw, self.gh = gw, gh

    def total_value(self):
        if self.occ is None:
            return 0.0, 0
        return (float((self.cls == GOLD).sum()) * BRICK_VALUE[GOLD]
                + float((self.cls == GREEN).sum()) * BRICK_VALUE[GREEN]
                + float((self.cls == ORANGE).sum()) * BRICK_VALUE[ORANGE]
                + float((self.cls == TAN).sum()) * BRICK_VALUE[TAN]), int((self.cls == GOLD).sum())

    def hit_edge(self, x, y, ex, ey, rad, removed):
        """靴子前沿（一条线段，沿 (ex,ey) 方向、长度 ±0.85 半径）碰到的砖。
        靴子有宽度：比靴子窄的缝钻不过去。中间的点优先（决定打碎哪块）。"""
        for f in (0.0, -0.85, 0.85):
            k, gx, gy = self.kind_at(x + ex * f * rad, y + ey * f * rad, removed)
            if k:
                return k, gx, gy
        return 0, 0, 0

    def kind_at(self, x, y, removed):
        gx, gy = int(x // self.cs), int(y // self.cs)
        if 0 <= gy < self.gh and 0 <= gx < self.gw:
            k = self.grid[gy][gx]
            if k and (gx, gy) not in removed:
                return k, gx, gy
        return 0, gx, gy

    # 兼容旧接口（画图、旧代码）
    def march(self, x, y, dx, dy, y_stop=None, max_len=None, skip=0.0):
        r = simulate(self, x, y, dx, dy, y_stop if y_stop is not None else 1e9, max_bounce=1)
        return r["kind"], r["x"], r["y"], -1, -1, r["path"]


def simulate(bm, x, y, dx, dy, catch_y, max_bounce=20, max_len=None, removed=None):
    """模拟靴子飞行：左右墙和砖块都会反弹（分别判断撞的是砖块侧面还是底面/顶面），
    打碎的砖从模拟里移除。一直算到落回马车高度、飞出顶部，或者达到反弹次数上限。"""
    w, cs = bm.w, bm.cs
    rad = BOOT_RAD * w
    n = float(np.hypot(dx, dy)) or 1.0
    dx, dy = dx / n, dy / n
    step = cs * 0.75
    limit = max_len or 7 * bm.h
    removed = set(removed) if removed else set()
    value = 0.0
    hits = gold = bounces = 0
    path = [(x, y)]
    length = 0.0
    first_kind = None
    while length < limit:
        # ---- 横向一步
        nx = x + dx * step
        if nx < rad:
            nx, dx = 2 * rad - nx, -dx
            path.append((nx, y))
        elif nx > w - rad:
            nx, dx = 2 * (w - rad) - nx, -dx
            path.append((nx, y))
        else:
            k, gx, gy = bm.hit_edge(nx + (rad if dx > 0 else -rad), y, 0, 1, rad, removed)
            if k:
                dx = -dx
                nx = x
                bounces += 1
                path.append((x, y))
                if first_kind is None:
                    first_kind = k
                if k != CLS_DEAD:
                    removed.add((gx, gy)); hits += 1; value += BRICK_VALUE[k]; gold += (k in (GOLD, GREEN))
        x = nx
        # ---- 纵向一步
        ny = y + dy * step
        if dy < 0 and ny - rad < 0:
            path.append((x, 0))
            return dict(kind="top", x=x, y=0, value=value, hits=hits, gold=gold,
                        bounces=bounces, path=path, length=length, first=first_kind, removed=removed)
        k, gx, gy = bm.hit_edge(x, ny + (rad if dy > 0 else -rad), 1, 0, rad, removed)
        if k:
            dy = -dy
            ny = y
            bounces += 1
            path.append((x, y))
            if first_kind is None:
                first_kind = k
            if k != CLS_DEAD:
                removed.add((gx, gy)); hits += 1; value += BRICK_VALUE[k]; gold += (k in (GOLD, GREEN))
        y = ny
        length += step
        if dy > 0 and y >= catch_y:
            path.append((x, catch_y))
            return dict(kind="land", x=x, y=catch_y, value=value, hits=hits, gold=gold,
                        bounces=bounces, path=path, length=length, first=first_kind, removed=removed)
        if bounces > max_bounce:
            break
    path.append((x, y))
    return dict(kind="none", x=x, y=y, value=value, hits=hits, gold=gold,
                bounces=bounces, path=path, length=length, first=first_kind, removed=removed)


REMAINING = [0.0]       # 这一关剩余砖的总价值（主循环里更新）


def shot_score(res, theta, exit_mode, w):
    """给一条模拟路线打分"""
    sc = -0.02 * abs(theta)
    if res["kind"] == "top":
        if exit_mode:
            return sc + 100.0 + res["value"]          # 想过关：飞出顶部最好
        sc -= max(EXIT_PENALTY, EXIT_PENALTY_FRAC * REMAINING[0])   # 还不想走：飞出去 = 剩下的砖都没了
    sc += res["value"] + 0.6 * res["gold"]            # 一路上打掉的砖（金色额外加分）
    if res["kind"] == "land":
        edge = min(res["x"], w - res["x"]) / w
        if edge < 0.08:
            sc -= 1.0                                 # 落点贴墙，接起来危险
    elif res["kind"] == "none":
        sc += 0.5                                     # 在砖块里弹很久没回来：通常是好事
    if exit_mode and res["kind"] != "top":
        sc *= 0.3
    return sc


def plan_shot(bm, x_land, catch_y, cart_x, t_land, speed, k_aim, prev_theta, exit_mode=False,
              sigma=AIM_SIGMA0):
    """选一个反弹角度：返回 (角度, 马车目标位置, 结果说明, 路径)
    每 1° 模拟一次；再按瞄准误差 sigma 求“期望得分”——小口子后面就算有金砖，
    打不准时期望值也会变低，就会改打更稳的目标。"""
    w = bm.w
    reach = speed * w * max(0.0, t_land - 0.08) + 0.01 * w
    th_max = int(min(AIM_MAX, k_aim * AIM_MAX_OFF))
    lim = th_max + int(2 * sigma) + 2                 # 误差范围外的角度也要模拟，用来算期望
    angles = list(range(-lim, lim + 1))
    scores, results = [], []
    for th in angles:
        rad = np.radians(th)
        res = simulate(bm, x_land, catch_y - 1, np.sin(rad), -np.cos(rad), catch_y)
        results.append(res)
        scores.append(shot_score(res, th, exit_mode, w))
    scores = np.array(scores)
    a = np.array(angles, float)
    cands = []
    for i, th in enumerate(angles):
        if abs(th) > th_max:
            continue
        desired = x_land - (th / k_aim) * w
        if not (0.09 * w <= desired <= 0.91 * w):
            continue
        if abs(desired - cart_x) > reach and th != 0:
            continue
        wts = np.exp(-0.5 * ((a - th) / max(sigma, 0.5)) ** 2)
        ev = float((wts * scores).sum() / wts.sum())
        cands.append([ev, th, desired, results[i], 0.0])
    if not cands:
        # 落点贴墙：马车只能停在边上，能打出的角度是固定的
        desired = min(max(x_land, 0.09 * w), 0.91 * w)
        th = int(round(k_aim * (x_land - desired) / w))
        i = angles.index(max(-lim, min(lim, th)))
        return th, desired, "edge", results[i]["path"]
    if LOOKAHEAD and not exit_mode:
        cands.sort(key=lambda c: -c[0])
        for c in cands[:LOOKAHEAD_TOP]:
            res = c[3]
            if res["kind"] == "land":
                c[4] = next_shot_ev(bm, res["x"], catch_y, sigma, k_aim, res["removed"])
            elif res["kind"] == "none":
                c[4] = 0.5 * res["value"]          # 还在砖堆里弹：后面大概率还会继续得分
        for c in cands[:LOOKAHEAD_TOP]:
            c[0] += LOOKAHEAD_W * c[4]
    best = max(cands, key=lambda c: c[0])
    ev, th, desired, res, nxt = best
    label = ("EXIT" if res["kind"] == "top" else f"{res['value']:.0f}pts g{res['gold']}") + f" ev{ev:.0f}"
    if nxt:
        label += f" +next{nxt:.0f}"
    return th, desired, label, res["path"]


def next_shot_ev(bm, x0, catch_y, sigma, k_aim, removed):
    """靴子落在 x0 之后，下一下最好能拿到的期望分（马车有时间走到任何位置）"""
    w = bm.w
    th_max = int(min(AIM_MAX, k_aim * AIM_MAX_OFF))
    angles = list(range(-th_max - 6, th_max + 7, 3))
    sc = []
    for th in angles:
        rad = np.radians(th)
        r = simulate(bm, x0, catch_y - 1, np.sin(rad), -np.cos(rad), catch_y, max_bounce=10, removed=removed)
        sc.append(shot_score(r, th, False, w))
    sc = np.array(sc); a = np.array(angles, float)
    best = 0.0
    for th in angles:
        if abs(th) > th_max:
            continue
        if not (0.09 * w <= x0 - (th / k_aim) * w <= 0.91 * w):
            continue
        wts = np.exp(-0.5 * ((a - th) / max(sigma, 0.5)) ** 2)
        best = max(best, float((wts * sc).sum() / wts.sum()))
    return best


def predict_landing(bm, x, y, vx, vy, catch_y):
    """模拟靴子撞砖/撞墙后落回的位置。返回 (落点, 反弹次数, 结果)；
    结果 'top' = 会飞出顶部，'none' = 在砖堆里乱弹、算不清"""
    res = simulate(bm, x, y, vx, vy, catch_y, max_bounce=10)
    if res["kind"] != "land":
        return None, res["bounces"], res["kind"]
    return res["x"], res["bounces"], "land"


class BounceLearner:
    """记录每次马车反弹：靴子打在车上的偏移 → 弹出角度，用来自动修正 AIM_K"""
    def __init__(self, log):
        self.samples = deque(maxlen=40)
        self.pending = None
        self.k = AIM_K
        self.log = log
        self.errs = deque(maxlen=20)
        self.sigma = AIM_SIGMA0

    def on_frame(self, t, trk, cart_x, catch_y, w, prev_vy, intended=None):
        if not trk.active or cart_x is None:
            return
        _, x, y = trk.hist[-1]
        if prev_vy > 60 and trk.vy < -60 and abs(y - catch_y) < 0.08 * w and self.pending is None:
            self.pending = (t, (x - cart_x) / w, intended)
        if self.pending and t - self.pending[0] > 0.12 and trk.vy < -60:
            off, intended = self.pending[1], self.pending[2]
            ang = float(np.degrees(np.arctan2(trk.vx, -trk.vy)))
            self.pending = None
            if abs(off) < 0.12:
                self.samples.append((off, ang))
                use = [a / o for o, a in self.samples if 0.012 < abs(o) < 0.07]
                if len(use) >= 4:
                    self.k = float(min(max(np.median(use), 350.0), 1100.0))
                msg = f"马车反弹：偏移 {off:+.3f}w → 角度 {ang:+.0f}°   (K={self.k:.0f}, 样本 {len(use)})"
                if intended is not None:
                    self.errs.append(ang - intended)
                    if len(self.errs) >= 5:
                        rms = float(np.sqrt(np.mean(np.square(self.errs))))
                        self.sigma = min(max(rms, 2.0), 12.0)
                    msg += f"  想打 {intended:+d}°，误差 {ang - intended:+.0f}°（平均误差 {self.sigma:.1f}°）"
                self.log(msg)
        if self.pending and t - self.pending[0] > 0.5:
            self.pending = None


# ---------------------------------------------------------------------------
# 画面来源
# ---------------------------------------------------------------------------
class ScreenSource:
    def __init__(self, region):
        import mss
        self.sct = (getattr(mss, "MSS", None) or mss.mss)()
        self.region = region

    def grab(self):
        return np.asarray(self.sct.grab(self.region))[:, :, :3], time.perf_counter()


class ReplaySource:
    """离线测试：读一个文件夹里的帧图片"""
    def __init__(self, folder, crop, fps=30):
        self.files = sorted(glob.glob(os.path.join(folder, "*.png")) +
                            glob.glob(os.path.join(folder, "*.jpg")))
        self.crop, self.i, self.fps = crop, 0, fps

    def grab(self):
        if self.i >= len(self.files):
            return None, None
        img = cv2.imread(self.files[self.i])
        x, y, w, h = self.crop
        t = self.i / self.fps
        self.i += 1
        return img[y:y + h, x:x + w], t


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
def draw_overlay(pf, info, tracker, tgt, state, bm=None, aim_path=None):
    vis = pf.copy()
    h, w = vis.shape[:2]
    if bm is not None and bm.occ is not None:
        cs = bm.cs
        for gy, gx in zip(*np.nonzero(bm.occ)):
            col = {GOLD: (0, 255, 255), ORANGE: (0, 128, 255), TAN: (200, 200, 200),
                   CLS_DEAD: (255, 255, 0), GREEN: (0, 255, 0)}.get(int(bm.cls[gy, gx]), (0, 200, 0))
            cv2.rectangle(vis, (gx * cs + 1, gy * cs + 1), (gx * cs + cs - 2, gy * cs + cs - 2), col, 1)
    if aim_path:
        pts = np.array([[int(px), int(py)] for px, py in aim_path], np.int32)
        cv2.polylines(vis, [pts], False, (0, 0, 255), 2)
    cy = int(info["catch_y"])
    cv2.line(vis, (0, cy), (w, cy), (80, 80, 80), 1)
    cv2.line(vis, (0, int(info["roof"])), (w, int(info["roof"])), (0, 120, 0), 1)
    if info["cart_x"] is not None:
        cv2.circle(vis, (int(info["cart_x"]), int(info["roof"]) + 6), 8, (0, 255, 0), 2)
    for _, x, y in info["cands"]:
        cv2.circle(vis, (int(x), int(y)), 4, (255, 0, 255), 1)
    if tracker.active:
        _, x, y = tracker.hist[-1]
        cv2.circle(vis, (int(x), int(y)), 14, (0, 255, 255), 2)
        cv2.arrowedLine(vis, (int(x), int(y)),
                        (int(x + tracker.vx * 0.15), int(y + tracker.vy * 0.15)), (0, 255, 255), 2)
    if tgt is not None:
        cv2.line(vis, (int(tgt), cy - 12), (int(tgt), cy + 12),
                 (0, 0, 255), 3)
    cv2.putText(vis, state, (6, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)
    return vis


class Snapshots:
    """保留最近约 1.5 秒的小图；出现事件时拼成一张图存到 debug 文件夹，方便排查"""
    def __init__(self, enabled):
        self.enabled = enabled
        self.buf = deque(maxlen=48)
        self.n = 0
        self.dir = os.path.join(HERE, "debug")

    def add(self, pf, t, state, mark=None):
        if not self.enabled:
            return
        self.n += 1
        if self.n % 2:
            return
        img = pf
        if mark is not None:
            img = pf.copy()
            boot, tgt, cx, cy = mark
            if boot is not None:
                cv2.circle(img, (int(boot[0]), int(boot[1])), 16, (0, 255, 255), 3)   # 黄圈：认为的靴子
            if tgt is not None:
                cv2.line(img, (int(tgt), int(cy) - 25), (int(tgt), int(cy) + 25), (0, 0, 255), 6)  # 红线：目标
            if cx is not None:
                cv2.line(img, (int(cx), int(cy) + 10), (int(cx), int(cy) + 40), (0, 255, 0), 6)    # 绿线：马车中心
        th = cv2.resize(img, (140, int(pf.shape[0] * 140 / pf.shape[1])), interpolation=cv2.INTER_AREA)
        self.buf.append((t, th, state))

    def save(self, tag, t_now):
        if not self.enabled or not self.buf:
            return None
        items = list(self.buf)[-24:]
        tiles = []
        for t, th, st in items:
            th = th.copy()
            cv2.putText(th, f"{t - t_now:+.2f}s", (3, 13), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)
            cv2.putText(th, st[:14], (3, th.shape[0] - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
            tiles.append(th)
        while len(tiles) % 8:
            tiles.append(np.zeros_like(tiles[0]))
        grid = np.vstack([np.hstack(tiles[i:i + 8]) for i in range(0, len(tiles), 8)])
        os.makedirs(self.dir, exist_ok=True)
        name = os.path.join(self.dir, f"{time.strftime('%H%M%S')}_{tag}.jpg")
        cv2.imwrite(name, grid)
        old = sorted(glob.glob(os.path.join(self.dir, "*.jpg")))
        for f in old[:-40]:
            try:
                os.remove(f)
            except OSError:
                pass
        return name


def run(args):
    set_dpi_aware()
    if args.replay:
        x, y, w, h = [int(v) for v in args.crop.split(",")]
        src = ReplaySource(args.replay, (x, y, w, h))
    else:
        if not os.path.exists(CONFIG):
            print("找不到 config.json，请先运行: python calibrate.py")
            sys.exit(1)
        with open(CONFIG, encoding="utf-8") as f:
            region = json.load(f)
        src = ScreenSource({k: int(region[k]) for k in ("left", "top", "width", "height")})
        print(f"游戏区域: {region}")

    logf = open(os.path.join(HERE, "bot_log.txt"), "a", encoding="utf-8") if not args.replay else None

    def log(msg, t=None):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line)
        if logf:
            logf.write(line + "\n"); logf.flush()

    dry = args.preview or bool(args.replay)
    keys = Keys(dry=dry)
    det, trk = Detector(), Tracker()
    snaps = Snapshots(enabled=not args.no_debug)
    bm = BrickMap()
    learner = BounceLearner(log)
    aim_theta, aim_path, aim_kind = None, None, ""
    level_start, level_v0, level_misses, exit_mode = None, 0.0, 0, False
    last_aim = None                  # 最近一次瞄准的角度（用来算瞄准误差）
    brake, brake_probe = BRAKE0, None
    exit_wait_until = -1e9           # 靴子刚从顶部飞出去：等过关黑屏，不要发射
    prev_vy = 0.0
    cart_speed = CART_SPEED
    last_cart = None                 # (t, x, 按键)

    running = bool(args.replay) or args.preview
    if not running:
        log("准备就绪。切到 Dota 2 窗口，按 F8 开始 / 暂停，F12 退出。")
    f8_prev = False
    last_black = -1e9
    was_black = False
    dark_since = None
    last_launch = -1e9
    failed_launches = 0
    presses, next_press = 0, 0.0
    last_miss = -1e9
    last_boot = None                 # 最近一次看到靴子时的 (y比例, vy, 靴子x, 马车x, 目标x)
    levels = 0
    frames, fps_t0, last_status = 0, time.perf_counter(), 0.0

    try:
        while True:
            # 热键
            if key_pressed(VK_F12):
                log("F12：退出")
                break
            f8 = key_pressed(VK_F8)
            if f8 and not f8_prev:
                running = not running
                keys.release_all()
                trk.reset()
                failed_launches = 0
                presses = 0
                log("开始运行" if running else "已暂停")
            f8_prev = f8

            frame, t = src.grab()
            if frame is None:
                break
            pf = det.resize(frame)
            info = det.analyze(pf)
            w, h = info["w"], info["h"]
            frames += 1

            state, tgt = "", None
            dark = info["black"] and info["cart_x"] is None
            if dark and dark_since is None:
                dark_since = t
            if not dark and dark_since is not None:
                if running and not was_black and t - dark_since > 0.05:
                    f = snaps.save("short_dark", t)
                    log(f"忽略了一次 {t - dark_since:.2f} 秒的短暂黑屏（不是过关）" + (f"，截图: {f}" if f else ""))
                dark_since = None
            transition = dark and t - dark_since >= BLACK_SUSTAIN

            if not running:
                state = "PAUSED (F8)"
            elif dark and not transition:
                state = "DARK?"                 # 先观望，按键和跟踪都保持不动
            elif transition:
                state = "LEVEL TRANSITION"
                if not was_black:
                    levels += 1
                    f = snaps.save("level", t)
                    log(f"检测到过关黑屏（本次运行第 {levels} 次）" + (f"，截图: {f}" if f else ""))
                keys.release_all()
                trk.reset()
                bm.reset()
                level_start, level_v0, level_misses, exit_mode = None, 0.0, 0, False
                exit_wait_until = -1e9
                presses = 0
                last_boot = None
                last_black = t
            else:
                cands = info["cands"]
                if t - last_black < AFTER_BLACK_SEC:
                    cands = []                      # 黑屏淡入期间画面在变，不跟踪
                elif not trk.active and info["cart_x"] is not None:
                    # 还没锁定目标时，忽略停在马车上的靴子（马车移动时它也在动）
                    cands = [c for c in cands
                             if not (c[2] > info["roof"] - 0.12 * w
                                     and abs(c[1] - info["cart_x"]) < 0.09 * w)]
                seen = trk.update(t, cands, w)
                if seen:
                    failed_launches = 0
                    presses = 0
                cx = info["cart_x"]
                if brake_probe is not None:
                    if keys.held is not None:
                        brake_probe = None
                    elif t - brake_probe[0] > 0.2 and cx is not None:
                        slide = (cx - brake_probe[1]) * brake_probe[2] / w
                        if -0.01 < slide < 0.05:
                            brake = min(max(0.85 * brake + 0.15 * slide, 0.0), 0.04)
                        brake_probe = None
                if cx is not None:
                    bm.update(pf, info["catch_y"])
                    if bm.occ is not None and t - last_black > AFTER_BLACK_SEC:
                        v_now, gold_now = bm.total_value()
                        REMAINING[0] = v_now
                        if level_start is None:
                            level_start = t
                        if t - level_start < 3.0:
                            level_v0 = max(level_v0, v_now)   # 开局几秒内取最大值作为这一关的总量
                        want_exit = (level_v0 > 0 and v_now <= EXIT_FRACTION * level_v0) \
                            or (t - level_start > LEVEL_TIME_CAP) or (level_misses >= EXIT_AFTER_MISSES)
                        if want_exit and not exit_mode:
                            log(f"开始找出口过关（剩余砖价值 {v_now:.0f}/{level_v0:.0f}，"
                                f"金砖 {gold_now}，本关 {t - level_start:.0f} 秒，掉命 {level_misses}）")
                        exit_mode = want_exit
                    # 测马车速度：按住同一个方向键时的移动速度
                    if last_cart and last_cart[2] and last_cart[2] == keys.held and t > last_cart[0]:
                        v = abs(cx - last_cart[1]) / (t - last_cart[0]) / w
                        if 0.2 < v < 2.0:
                            cart_speed = 0.95 * cart_speed + 0.05 * v
                    last_cart = (t, cx, keys.held)
                if trk.active:
                    state = "PLAY"
                    tgt = target_x(trk, t, w, info["catch_y"])
                    dz = DEADZONE * w
                    p = trk.predict(t)
                    stale = t - trk.sel.last_seen
                    if stale > 0.08:
                        # 跟丢了一小会儿：不要再按旧速度往前推（会以为它飞出顶部了），守在中间偏它最后出现的那一侧
                        lx = trk.hist[-1][1]
                        tgt = 0.5 * lx + 0.25 * w
                        state = "PLAY (lost)"
                        p = None
                    if AIM and bm.occ is not None and cx is not None and p is not None:
                        if trk.vy > 20:
                            t_land = (info["catch_y"] - p[1]) / trk.vy
                            sim_land, _, _ = predict_landing(bm, p[0], p[1], trk.vx, trk.vy, info["catch_y"])
                            if sim_land is not None:
                                tgt = sim_land            # 下落途中还会撞砖时，用模拟的落点
                            if t_land > 0 and tgt is not None:
                                x_land = tgt
                                reach = cart_speed * w * max(0.0, t_land - 0.08) + 0.01 * w
                                if aim_theta is not None:
                                    # 已锁定角度：按最新落点更新马车目标
                                    off = max(-AIM_MAX_OFF, min(AIM_MAX_OFF, aim_theta / learner.k))
                                    want = min(max(x_land - off * w, 0.09 * w), 0.91 * w)
                                    if abs(want - cx) <= reach or aim_theta == 0:
                                        tgt = want
                                    else:
                                        aim_theta = None            # 来不及了，重新选
                                if aim_theta is None and len(trk.hist) >= 4:
                                    aim_theta, tgt, aim_kind, aim_path = plan_shot(
                                        bm, x_land, info["catch_y"], cx, t_land, cart_speed, learner.k, None,
                                        exit_mode, learner.sigma)
                                if aim_theta is not None:
                                    last_aim = aim_theta
                                    dz = AIM_DEADZONE * w
                                    state = f"AIM {aim_theta:+d} {aim_kind}"
                        else:
                            aim_theta, aim_path = None, None
                            land, nb, kind = predict_landing(bm, p[0], p[1], trk.vx, trk.vy, info["catch_y"])
                            if land is not None:
                                if nb >= 3:
                                    land = 0.5 * land + 0.25 * w   # 要撞好几次才落下：不确定，往中间靠
                                tgt = land
                                state = "PLAY (pre)"
                            elif kind == "top" and nb <= 1 and p[1] < 0.25 * h:
                                tgt = cx                            # 一路畅通、快飞出顶部了：原地等
                                state = "PLAY (exit?)"
                            else:
                                tgt = 0.5 * p[0] + 0.25 * w         # 在砖堆里乱弹、算不清：守在中间偏它那一侧
                                state = "PLAY (chaos)"
                    if tgt is not None:
                        tgt = min(max(tgt, 0.05 * w), 0.95 * w)
                    if tgt is not None and cx is not None:
                        d = tgt - cx
                        stop = brake * w                 # 松键后还会滑这么远：提前松
                        if keys.held == "right":
                            want_key = "right" if d > dz + stop else None
                        elif keys.held == "left":
                            want_key = "left" if d < -(dz + stop) else None
                        else:
                            want_key = "right" if d > dz + 0.5 * stop else "left" if d < -(dz + 0.5 * stop) else None
                        if keys.held in ("left", "right") and want_key != keys.held:
                            brake_probe = (t, cx, 1 if keys.held == "right" else -1)
                        keys.hold(want_key)
                    else:
                        keys.hold(None)
                    learner.on_frame(t, trk, cx, info["catch_y"], w, prev_vy, last_aim)
                    prev_vy = trk.vy
                else:
                    keys.hold(None)
                    aim_theta, aim_path = None, None
                    last_aim = None
                    idle = t - max(trk.last_seen, last_launch,
                                   last_black + AFTER_BLACK_SEC - NO_BOOT_SEC)
                    state = f"WAIT {idle:.1f}s"
                    if presses > 0:
                        state = f"LAUNCH x{presses}"
                        if presses >= LAUNCH_PRESSES:
                            presses = 0
                            last_launch = t
                        elif t >= next_press:
                            if not dry:
                                keys.tap("space", 0.08)
                            presses += 1
                            next_press = t + 0.45
                    elif t < exit_wait_until:
                        state = "EXITED? wait"
                    elif t - last_miss < LOST_LIFE_DELAY:
                        state = f"LOST LIFE, wait {LOST_LIFE_DELAY - (t - last_miss):.1f}s"
                    elif (idle > NO_BOOT_SEC and t - last_black > AFTER_BLACK_SEC
                            and t - last_launch > LAUNCH_COOLDOWN):
                        if failed_launches >= MAX_FAILED_LAUNCH:
                            running = False
                            log(f"连续 {failed_launches} 轮发射都没看到靴子，自动暂停。"
                                "可能游戏结束/弹出菜单了，处理后按 F8 继续。")
                        else:
                            state = "LAUNCH x1"
                            f = snaps.save("launch", t)
                            log("发射：按空格（之后每 0.45 秒补按一次，直到看到靴子飞出）"
                                + (f"，截图: {f}" if f else ""))
                            if not dry:
                                keys.tap("space", 0.08)
                            presses, next_press = 1, t + 0.45
                            last_launch = t
                            failed_launches += 1
            was_black = transition if running else False
            if running and trk.active:
                _, bx, by = trk.hist[-1]
                last_boot = (by / h, trk.vy, bx, info["cart_x"], tgt)
            elif last_boot is not None:
                by_r, bvy, bx, bcx, btgt = last_boot
                if running and not transition and by_r < 0.15 and bvy < 0:
                    exit_wait_until = t + 2.5         # 从顶部飞出去了，马上就会过关黑屏
                if running and not transition and by_r > 0.55 and bvy > 0:
                    last_miss = t
                    level_misses += 1
                    presses = 0
                    f = snaps.save("miss", t)
                    log(f"漏接？靴子最后在 x={bx / w:.2f} (高度 {by_r:.2f})，马车 x="
                        f"{(bcx or 0) / w:.2f}，目标 x={(btgt or 0) / w:.2f}" + (f"，截图: {f}" if f else ""))
                last_boot = None
            mark = None
            if snaps.enabled:
                mark = (trk.hist[-1][1:] if trk.active else None, tgt, info["cart_x"], info["catch_y"])
            snaps.add(pf, t, state, mark)

            if args.preview or args.show:
                vis = draw_overlay(pf, info, trk, tgt, state, bm, aim_path)
                cv2.imshow("brick bot preview", vis)
                if cv2.waitKey(1) & 0xFF == 27:
                    break
            if args.replay and args.trace and frames % 3 == 0:
                b = trk.hist[-1] if trk.active else None
                print(f"t={t:5.2f} {state:18s} cart={info['cart_x'] and round(info['cart_x'])} "
                      f"boot={b and (round(b[1]), round(b[2]))} v=({trk.vx:.0f},{trk.vy:.0f}) "
                      f"tgt={tgt and round(tgt)} key={keys.held}")

            now = time.perf_counter()
            if not args.replay and now - last_status > 2.0:
                fps = frames / (now - fps_t0)
                frames, fps_t0, last_status = 0, now, now
                print(f"  fps={fps:5.1f}  状态={state}  按键={keys.held}  "
                      f"瞄准误差≈{learner.sigma:.1f}°  滑行≈{brake:.3f}w")
    finally:
        keys.release_all()
        if logf:
            logf.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Dota 2 打砖块机器人")
    ap.add_argument("--preview", action="store_true", help="只显示识别画面，不按键")
    ap.add_argument("--show", action="store_true", help="运行时也显示识别画面（可能抢焦点，不推荐）")
    ap.add_argument("--no-debug", action="store_true", help="不保存 debug 截图")
    ap.add_argument("--replay", help=argparse.SUPPRESS)
    ap.add_argument("--crop", default="240,150,825,1090", help=argparse.SUPPRESS)
    ap.add_argument("--trace", action="store_true", help=argparse.SUPPRESS)
    run(ap.parse_args())
