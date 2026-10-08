"""
校准：框选游戏区域，结果存到 config.json（只需做一次；换分辨率或挪窗口后重做）

用法：
    python calibrate.py              # 默认主显示器
    python calibrate.py --monitor 2  # 第二块显示器

运行后有 5 秒倒计时，请切到 Dota 2，让打砖块画面完整显示（不要暂停）。
截图后会弹出窗口：用鼠标拖一个框，把整个打砖块画面（包括两根金色柱子）框进去即可，
不用很精确，脚本会自动收紧到两根柱子之间的游戏区。按 回车/空格 确认（C 取消）。
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
            print(f"{i} 秒后截图，请切到 Dota 2 打砖块画面…")
            time.sleep(1)
        shot = np.asarray(sct.grab(mon))[:, :, :3].copy()

    if shot.mean() < 3:
        print("截图几乎全黑：Dota 2 可能是“全屏独占”模式，请改成“无边框窗口”后重试。")
        sys.exit(1)

    H, W = shot.shape[:2]
    scale = min(1.0, 1500 / W, 850 / H)
    view = cv2.resize(shot, (int(W * scale), int(H * scale)), interpolation=cv2.INTER_AREA)
    print("请在弹出窗口里框选游戏区域，然后按 回车 确认。")
    print("（如果没看到窗口：按 Alt+Tab，或点任务栏里的 Python 图标，把它切到最前面）")
    win = "drag playfield, then ENTER"
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
    box = shot[y0:y0 + hh, x0:x0 + ww]

    # 在你框的范围里自动找游戏区（两根柱子之间、计分栏下方）
    fit = bot.fit_playfield(box)
    if fit is not None:
        fx, fy, fw, fh = fit
        print(f"\n✓ 自动找到游戏区（已从你框的范围收紧到两根柱子之间）")
    else:
        fx, fy, fw, fh = 0, 0, ww, hh
        print("\n⚠ 没能自动找到游戏区，直接使用你框的范围。"
              "请确认只框了两根金色柱子之间、计分栏下方的蓝色区域。")
    region = {"left": mon["left"] + x0 + fx, "top": mon["top"] + y0 + fy,
              "width": fw, "height": fh}

    # 检查图：你框的范围 + 绿框标出最终使用的游戏区
    check = box.copy()
    cv2.rectangle(check, (fx, fy), (fx + fw - 1, fy + fh - 1), (0, 255, 0), 3)
    cv2.imwrite(os.path.join(HERE, "calib_check.png"), check)

    det = bot.Detector()
    info = det.analyze(det.resize(box[fy:fy + fh, fx:fx + fw]))
    print(f"游戏区域：{region}  （高/宽 = {fh / max(1, fw):.2f}）")
    if info["cart_x"] is None:
        print("⚠ 在游戏区下部没找到红色马车。请确认画面里有马车、没有被暂停，然后重新校准。")
    else:
        print(f"✓ 找到马车，横坐标约在游戏区的 {info['cart_x'] / info['w'] * 100:.0f}% 处")

    with open(os.path.join(HERE, "config.json"), "w", encoding="utf-8") as f:
        json.dump(region, f, indent=2)
    print("已保存 config.json。打开 calib_check.png 看一眼：绿框应该正好框住两根柱子之间的游戏区。")
    print("下一步：python bot.py --preview  （只看识别效果）  或  python bot.py  （正式运行）")


if __name__ == "__main__":
    main()
