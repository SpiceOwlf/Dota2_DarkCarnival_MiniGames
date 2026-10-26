"""
校准：框选小游戏画面，结果存到 config.json（只需做一次；换分辨率或挪窗口后重做）

用法：
    python calibrate.py              # 默认主显示器
    python calibrate.py --monitor 2  # 第二块显示器

运行后有 5 秒倒计时，请切到 Dota 2，打开「机关人攻击」小游戏（开始界面或游戏中都行）。
截图后会弹出窗口：用鼠标拖一个框，把整个小游戏画面框进去（从左上角的分数到右下角的边框，
和录像里的范围一样）。按 回车/空格 确认（C 取消）。
"""
import argparse
import json
import os
import sys
import time

import cv2
import numpy as np

import bot

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--monitor", type=int, default=1)
    ap.add_argument("--delay", type=int, default=5)
    args = ap.parse_args()

    bot.set_dpi_aware()
    import mss

    MSS = getattr(mss, "MSS", None) or mss.mss
    with MSS() as sct:
        if args.monitor >= len(sct.monitors):
            print(f"没有第 {args.monitor} 块显示器，可用：1..{len(sct.monitors) - 1}")
            sys.exit(1)
        mon = sct.monitors[args.monitor]
        for i in range(args.delay, 0, -1):
            print(f"{i} 秒后截图，请切到 Dota 2 小游戏画面…")
            time.sleep(1)
        shot = np.asarray(sct.grab(mon))[:, :, :3].copy()

    if shot.mean() < 3:
        print("截图几乎全黑：Dota 2 可能是“全屏独占”模式，请改成“窗口”或“无边框窗口”后重试。")
        sys.exit(1)

    H, W = shot.shape[:2]
    scale = min(1.0, 1500 / W, 850 / H)
    view = cv2.resize(shot, (int(W * scale), int(H * scale)), interpolation=cv2.INTER_AREA)
    print("请在弹出窗口里框选整个小游戏画面，然后按 回车 确认。")
    print("（如果没看到窗口：按 Alt+Tab，或点任务栏里的 Python 图标，把它切到最前面）")
    win = "drag minigame area, then ENTER"
    cv2.namedWindow(win, cv2.WINDOW_AUTOSIZE)
    cv2.imshow(win, view)
    cv2.waitKey(1)
    try:
        cv2.setWindowProperty(win, cv2.WND_PROP_TOPMOST, 1)   # 置顶，避免被 Dota 挡住
    except Exception:
        pass
    if bot.IS_WIN:
        try:
            hwnd = bot.user32.FindWindowW(None, win)
            if hwnd:
                bot.user32.ShowWindow(hwnd, 5)
                bot.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
    x, y, w, h = cv2.selectROI(win, view, showCrosshair=False)
    cv2.destroyAllWindows()
    if w < 20 or h < 20:
        print("没有框选，已取消。")
        sys.exit(1)

    x0, y0 = int(round(x / scale)), int(round(y / scale))
    ww, hh = int(round(w / scale)), int(round(h / scale))
    region = {"left": mon["left"] + x0, "top": mon["top"] + y0, "width": ww, "height": hh}
    box = shot[y0:y0 + hh, x0:x0 + ww]

    # 检查图：把识别到的词画出来
    img = bot.to_proc(box)
    words = bot.find_words(img)
    check = img.copy()
    for wd in words:
        cv2.rectangle(check, (wd.x0 - 3, wd.y0 - 3), (wd.x1 + 3, wd.y1 + 3), (0, 255, 0), 2)
    cv2.imwrite(os.path.join(HERE, "calib_check.png"), check)

    print(f"\n游戏区域：{region}  （宽/高 = {ww / max(1, hh):.2f}，录像里是 0.98）")
    if not 0.85 < ww / max(1, hh) < 1.15:
        print("⚠ 框的形状和录像里差得比较多，确认一下是不是框住了整个小游戏画面（不要框整个 Dota 窗口）。")
    if words:
        hs = sorted(wd.h for wd in words)
        print(f"✓ 画面里找到 {len(words)} 个词，字高约 {hs[len(hs) // 2]} 像素（正常 26~36）")
    else:
        print("（截图时画面里没有单词，没法检查字的大小；开始游戏后用 python bot.py --preview 看一下）")

    with open(os.path.join(HERE, "config.json"), "w", encoding="utf-8") as f:
        json.dump(region, f, indent=2)
    print("已保存 config.json。可以打开 calib_check.png 看看框得对不对。")
    print("下一步：python bot.py --preview  （只看识别效果）  或  python bot.py  （正式运行）")


if __name__ == "__main__":
    main()
