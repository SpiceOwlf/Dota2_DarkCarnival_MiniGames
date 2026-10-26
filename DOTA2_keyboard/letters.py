"""
字母模板识别：游戏字体固定，每个字母是一个连通块。把字母缩放到统一大小，和存下来的样本比相似度（k 近邻投票）。
比 Tesseract 快得多（< 1ms），也不会把 J 认丢、把 I 认成 L。
样本存在 letters.npz；运行中每个“一次打掉”的词都会确认它的字母，自动加进样本库。
"""
import os

import cv2
import numpy as np

W, H = 40, 32           # 字母统一缩放到 H 高，放在 W 宽的画布中间
PER_LETTER = 60         # 每个字母最多存多少个样本
K = 5                   # k 近邻
MIN_SCORE = 0.85        # 最像的样本相似度要到这个值，才算认出来
MIN_MARGIN = 0.0        # 第一名比第二名（另一个字母）至少高这么多


def feature(crop):
    """crop: 一个字母的布尔掩码（字母高度 = crop 高度）。返回归一化的特征向量"""
    img = crop.astype(np.uint8) * 255
    h, w = img.shape
    if h < 4 or w < 1:
        return None
    nw = max(1, min(W, int(round(w * H / h))))
    img = cv2.resize(img, (nw, H), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((H, W), np.uint8)
    x0 = (W - nw) // 2
    canvas[:, x0:x0 + nw] = img
    f = cv2.GaussianBlur(canvas, (5, 5), 1.2).astype(np.float32).ravel()
    n = np.linalg.norm(f)
    return f / n if n > 0 else None


class Letters:
    def __init__(self, path):
        self.path = path
        self.X = np.zeros((0, W * H), np.float32)
        self.y = np.zeros((0,), "<U1")
        self.added = 0
        if os.path.exists(path):
            d = np.load(path)
            self.X, self.y = d["X"].astype(np.float32), d["y"]

    def __len__(self):
        return len(self.y)

    def letters_known(self):
        return "".join(sorted(set(self.y.tolist())))

    def read(self, feats):
        """返回 (字符串, 最低置信度)；有字母认不出时置信度为 0"""
        if len(self.y) == 0 or not feats or any(f is None for f in feats):
            return "", 0.0
        S = np.stack(feats) @ self.X.T                     # (n字母, n样本) 余弦相似度
        out, conf = [], 1.0
        for row in S:
            idx = np.argpartition(-row, min(K, len(row) - 1))[:K]
            votes = {}
            for i in idx:
                votes[self.y[i]] = votes.get(self.y[i], 0.0) + row[i]
            best = max(votes, key=votes.get)
            top = row[self.y == best].max()
            others = row[self.y != best]
            second = others.max() if len(others) else 0.0
            c = top if (top >= MIN_SCORE and top - second >= MIN_MARGIN) else 0.0
            out.append(best)
            conf = min(conf, c)
        return "".join(out), conf

    def add(self, feats, text):
        if len(feats) != len(text) or any(f is None for f in feats):
            return False
        X = np.stack(feats).astype(np.float32)
        y = np.array(list(text), "<U1")
        self.X = np.vstack([self.X, X])
        self.y = np.concatenate([self.y, y])
        # 每个字母只留最新的 PER_LETTER 个
        keep = np.zeros(len(self.y), bool)
        for ch in set(text):
            idx = np.flatnonzero(self.y == ch)
            keep[idx[-PER_LETTER:]] = True
        for ch in set(self.y.tolist()) - set(text):
            keep[self.y == ch] = True
        self.X, self.y = self.X[keep], self.y[keep]
        self.added += 1
        return True

    def save(self):
        np.savez_compressed(self.path, X=self.X.astype(np.float16), y=self.y)
