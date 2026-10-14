"""短線「發動」機率研究：用收盤前就知道的條件，估計一檔股票隔天開盤買進後，5 日內先漲 7% 還是先跌破停損。

目的不是找到「必漲」的股票（做不到），而是回答兩件事：
1. 哪些條件真的讓發動機率比基準高（單一條件分組、合成分數）？
2. 合成分數最高的那一群，在「沒看過的時期」發動機率比基準高多少倍？

定義（全部以 t 日收盤的資訊，隔天 t+1 開盤買進）：
- 發動（label A）：t+1 起 5 個交易日內，最高價先碰到「開盤價 +7%」，而且在那之前沒有碰到停損（開盤價 − 1.5×ATR）。
  同一天既碰到目標又碰到停損，保守算「沒發動」。只算大盤多頭（和進場規則一致）。
- 發動（label B）：改成先碰到 +2R（R＝開盤價到停損的距離＝1.5×ATR）。固定 +7% 對波動大的股票比較容易達成，B 把波動大小扣掉，
  看的是真正的方向優勢，也更貼近實際策略（+2R 先出一半）。
- 平均 R（rr）：同樣的停損與 +2R 目標，先到目標＝+2、先停損＝−1、5 日內都沒到＝依收盤價折算（限制在 −1～+2），代表這群的期望值。
- 特徵：價量、均線、相對強度、離高點距離、量能、題材熱度（同題材其他股票剛突破幾檔）、美股領先。
- 模型：純 numpy 的邏輯迴歸（透明、不易過度擬合）。「線性」用標準化後的特徵；「分箱」把每個特徵切成 5 等分、各自一個加分，能抓非線性。
- 檢驗：時間切半、互換訓練與測試（先 A 訓練 B 測試，再反過來），訓練與測試之間空 5 天避免標籤重疊。
注意：同一天、同題材的樣本彼此相關，樣本數看起來大但有效獨立樣本少；題材成分用現在的清單回頭套，有後見之明。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import early

HORIZON, UP, ATR_MULT = 5, 0.07, 1.5
FEATURES = ["ret1", "ret5", "ret20", "ret60", "gap", "dist52", "rs120", "rs20", "vr", "vr5", "atr_pct", "tr_ratio",
            "c_ma20", "c_ma60", "ma20_slope", "clv", "up_days5", "breakout", "th_brk", "th_ret5", "us_lead", "mkt_strong"]
LABELS = {
    "ret1": "今日漲幅", "ret5": "近5日漲幅", "ret20": "近20日漲幅", "ret60": "近60日漲幅", "gap": "離20日高(差%)",
    "dist52": "離52週高", "rs120": "120日相對強度", "rs20": "20日相對強度", "vr": "今日量比", "vr5": "近5日均量比",
    "atr_pct": "ATR占股價", "tr_ratio": "波動收斂(10日/50日振幅)", "c_ma20": "股價離20日線", "c_ma60": "股價離60日線",
    "ma20_slope": "20日線斜率(5日)", "clv": "收盤在當日區間位置", "up_days5": "近5日上漲天數", "breakout": "今日放量突破",
    "th_brk": "同題材近3日突破檔數", "th_ret5": "題材近5日平均漲幅", "us_lead": "美股領先強度", "mkt_strong": "大盤強勢"}


# ---------------------------------------------------------------- 特徵與標籤

def label_frame(fr: dict[str, pd.DataFrame], horizon: int = HORIZON, up: float = UP, atr_mult: float = ATR_MULT,
                r_target: float = 2.0) -> dict:
    """標籤。hit＝label A（先碰 +up）、hit_r＝label B（先碰 +r_target R）、rr＝期望值（以 R 計）、
    fwd＝持有 horizon 日收盤報酬、mfe＝期間最大漲幅。NaN＝未來資料不足。"""
    entry = fr["open"].shift(-1)
    risk = atr_mult * fr["atr"]
    stop = entry - risk
    target = entry * (1 + up)
    target_r = entry + r_target * risk
    idx, cols = entry.index, entry.columns
    alive_a = pd.DataFrame(True, index=idx, columns=cols)
    alive_b = alive_a.copy()
    hit_a = pd.DataFrame(False, index=idx, columns=cols)
    hit_b, lose_b = hit_a.copy(), hit_a.copy()
    hi_max = None
    for h in range(1, horizon + 1):
        hi, lo = fr["high"].shift(-h), fr["low"].shift(-h)
        stopped = lo <= stop
        reached = hi >= target
        hit_a |= alive_a & reached & ~stopped      # 先到目標（同一天兩者都碰到＝算停損）
        alive_a &= ~reached & ~stopped
        reached_b = hi >= target_r
        hit_b |= alive_b & reached_b & ~stopped
        lose_b |= alive_b & stopped
        alive_b &= ~reached_b & ~stopped
        hi_max = hi if hi_max is None else np.maximum(hi_max, hi)
    last = fr["close"].shift(-horizon)
    valid = last.notna() & entry.notna() & fr["atr"].notna()
    rest = ((last - entry) / risk).clip(-1.0, r_target)
    rr = pd.DataFrame(np.where(hit_b, r_target, np.where(lose_b, -1.0, rest)), index=idx, columns=cols)
    return {"hit": hit_a.astype(float).where(valid), "hit_r": hit_b.astype(float).where(valid), "rr": rr.where(valid),
            "fwd": (last / entry - 1).where(valid), "mfe": (hi_max / entry - 1).where(valid)}


def feature_frames(fr: dict[str, pd.DataFrame], index_df: pd.DataFrame, themes_tw: dict | None = None,
                   lead: dict | None = None) -> dict[str, pd.DataFrame]:
    """每個特徵一張「日期 × 代號」寬表（t 日收盤前就知道）。"""
    c, h, l, v = fr["close"], fr["high"], fr["low"], fr["volume"]
    pc = c.shift(1)
    tr = np.maximum(np.maximum(h - l, (h - pc).abs()), (l - pc).abs())
    avg_v = v.shift(1).rolling(20).mean()
    f = {
        "ret1": c / pc - 1, "ret5": fr["ret5"], "ret20": fr["ret20"], "ret60": c / c.shift(60) - 1,
        "gap": fr["gap"], "dist52": c / h.rolling(252, min_periods=120).max() - 1,
        "rs120": fr["rs"], "rs20": fr["ret20"].rank(axis=1, pct=True),
        "vr": fr["vr"], "vr5": v.rolling(5).mean() / avg_v, "atr_pct": fr["atr"] / c,
        "tr_ratio": tr.rolling(10).mean() / tr.rolling(50).mean(),
        "c_ma20": c / fr["ma20"] - 1, "c_ma60": c / fr["ma60"] - 1, "ma20_slope": fr["ma20"] / fr["ma20"].shift(5) - 1,
        "clv": ((c - l) / (h - l).replace(0, np.nan)), "up_days5": (c.diff() > 0).astype(float).rolling(5).sum(),
        "breakout": fr["sig_a"].astype(float),
    }
    # 題材熱度：同題材其他股票近 3 日放量突破的檔數（取該股所屬題材中最大）、題材成分近 5 日平均漲幅
    brk3 = early.leaders(fr).astype(float)
    th_brk = pd.DataFrame(0.0, index=c.index, columns=c.columns)
    th_ret5 = pd.DataFrame(np.nan, index=c.index, columns=c.columns)
    cols = set(c.columns)
    for members in (themes_tw or {}).values():
        ms = list(dict.fromkeys(str(x) for x in members if str(x) in cols))
        if len(ms) < 3:
            continue
        nb = brk3[ms].sum(axis=1)
        mr = fr["ret5"][ms].mean(axis=1)
        for code in ms:
            th_brk[code] = np.maximum(th_brk[code], nb - brk3[code])
            th_ret5[code] = np.fmax(th_ret5[code], mr)
    f["th_brk"], f["th_ret5"] = th_brk, th_ret5.fillna(0.0)
    us = pd.DataFrame(0.0, index=c.index, columns=c.columns)
    for ts, items in (lead or {}).items():
        if ts in us.index:
            for sc, code, _ in items:
                if code in us.columns:
                    us.at[ts, code] = max(us.at[ts, code], float(sc))
    f["us_lead"] = us
    ic = index_df["close"].reindex(c.index).ffill()
    ma20 = ic.rolling(20).mean()
    strong = ((ic > ma20) & (ma20 > ma20.shift(10))).astype(float)
    f["mkt_strong"] = pd.DataFrame({code: strong for code in c.columns})
    return f


def dataset(fr: dict[str, pd.DataFrame], feats: dict[str, pd.DataFrame], labels: dict, mkt: pd.Series) -> pd.DataFrame:
    """攤成長表：每列是一個（日期, 代號），只留大盤多頭、特徵齊全、標籤有值的。（不用 stack，避免 pandas 版本差異）"""
    idx = fr["close"].index
    cols = fr["close"].columns
    mi = pd.MultiIndex.from_product([idx, cols], names=["date", "code"])

    def flat(frame: pd.DataFrame) -> np.ndarray:
        return frame.reindex(index=idx, columns=cols).to_numpy(dtype=float).ravel()

    data = {k: flat(feats[k]) for k in FEATURES}
    for k in ("hit", "hit_r", "rr", "fwd", "mfe"):
        data[k] = flat(labels[k])
    data["sig_a"], data["sig_b"] = flat(fr["sig_a"].astype(float)), flat(fr["sig_b"].astype(float))
    df = pd.DataFrame(data, index=mi)
    ok = mkt.reindex(idx).fillna(False).to_numpy(dtype=bool).repeat(len(cols))
    df = df[ok].dropna(subset=FEATURES + ["hit"])
    return df.reset_index()


# ---------------------------------------------------------------- 邏輯迴歸（純 numpy）

def fit_logit(X: np.ndarray, y: np.ndarray, l2: float = 5.0, iters: int = 30) -> np.ndarray:
    """牛頓法（IRLS）的 L2 邏輯迴歸。X 已含截距欄。"""
    w = np.zeros(X.shape[1])
    pen = np.eye(X.shape[1]) * l2
    pen[0, 0] = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(X @ w, -30, 30)))
        g = X.T @ (p - y) + pen @ w
        H = (X.T * (p * (1 - p))) @ X + pen + 1e-6 * np.eye(X.shape[1])
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-6:
            break
    return w


class Model:
    """kind: "linear"＝標準化特徵；"binned"＝每個特徵切 5 等分（訓練集分位數）各一個加分。"""

    def __init__(self, kind: str = "linear", l2: float = 5.0, features: list[str] | None = None):
        self.kind, self.l2, self.features = kind, l2, features or FEATURES

    def _design(self, df: pd.DataFrame, fit: bool) -> np.ndarray:
        A = df[self.features].to_numpy(float)
        if fit:
            self.lo, self.hi = np.nanpercentile(A, 1, axis=0), np.nanpercentile(A, 99, axis=0)
            A = np.clip(A, self.lo, self.hi)
            if self.kind == "linear":
                self.mu, self.sd = A.mean(axis=0), A.std(axis=0) + 1e-9
            else:
                self.edges = [np.unique(np.nanpercentile(A[:, j], [20, 40, 60, 80])) for j in range(A.shape[1])]
        else:
            A = np.clip(A, self.lo, self.hi)
        if self.kind == "linear":
            return np.c_[np.ones(len(A)), (A - self.mu) / self.sd]
        parts = [np.ones((len(A), 1))]
        for j, e in enumerate(self.edges):
            k = np.searchsorted(e, A[:, j], side="right")        # 0..len(e)
            onehot = np.zeros((len(A), len(e) + 1))
            onehot[np.arange(len(A)), k] = 1.0
            parts.append(onehot[:, 1:])                           # 以第一箱為基準
        return np.hstack(parts)

    def fit(self, df: pd.DataFrame, col: str = "hit") -> "Model":
        self.w = fit_logit(self._design(df, True), df[col].to_numpy(float), self.l2)
        return self

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        return 1 / (1 + np.exp(-np.clip(self._design(df, False) @ self.w, -30, 30)))


# ---------------------------------------------------------------- 評估

def lift_table(df: pd.DataFrame, score: np.ndarray, tops=(0.10, 0.05, 0.01), col: str = "hit") -> list[dict]:
    """分數最高的前 x% 的發動率、倍數、平均 R、平均5日報酬。"""
    base = float(df[col].mean())
    order = np.argsort(-score)
    rows = [{"組別": "全部（基準）", "筆數": int(len(df)), "發動率%": round(base * 100, 1), "倍數": 1.0,
             "平均R": round(float(df["rr"].mean()), 2), "平均5日%": round(float(df["fwd"].mean()) * 100, 2),
             "平均最大漲幅%": round(float(df["mfe"].mean()) * 100, 2)}]
    for q in tops:
        k = max(1, int(len(df) * q))
        sub = df.iloc[order[:k]]
        rows.append({"組別": f"分數最高前 {q:.0%}", "筆數": int(k), "發動率%": round(float(sub[col].mean()) * 100, 1),
                     "倍數": round(float(sub[col].mean()) / base, 2) if base else None,
                     "平均R": round(float(sub["rr"].mean()), 2), "平均5日%": round(float(sub["fwd"].mean()) * 100, 2),
                     "平均最大漲幅%": round(float(sub["mfe"].mean()) * 100, 2)})
    return rows


def calibration(df: pd.DataFrame, score: np.ndarray, bins: int = 10, col: str = "hit") -> list[dict]:
    """依分數切 10 等分：預測平均 vs 實際發動率、平均 R（看預測的機率準不準、分數高是不是真的比較賺）。"""
    q = pd.qcut(pd.Series(score).rank(method="first"), bins, labels=False)
    out = []
    for b in range(bins):
        m = (q == b).to_numpy()
        out.append({"十分位(10=最高)": b + 1, "筆數": int(m.sum()), "預測%": round(float(score[m].mean()) * 100, 1),
                    "實際發動率%": round(float(df[col].to_numpy()[m].mean()) * 100, 1),
                    "平均R": round(float(df["rr"].to_numpy()[m].mean()), 2)})
    return out


def univariate(df: pd.DataFrame, features: list[str] = FEATURES, bins: int = 5, col: str = "hit") -> list[dict]:
    """單一特徵分 5 等分的發動率與平均 R（看哪些條件真的有差；描述用，不是預測）。"""
    base = float(df[col].mean())
    rows = []
    for f in features:
        s = df[f]
        if s.nunique() < 3:
            continue
        try:
            q = pd.qcut(s.rank(method="first"), bins, labels=False)
        except ValueError:
            continue
        rates = [float(df[col][q == b].mean()) for b in range(bins)]
        rr = [float(df["rr"][q == b].mean()) for b in range(bins)]
        lo = [float(s[q == b].min()) for b in range(bins)]
        rows.append({"特徵": LABELS.get(f, f), "最低組發動率%": round(rates[0] * 100, 1), "最高組發動率%": round(rates[-1] * 100, 1),
                     "最高組÷基準": round(rates[-1] / base, 2) if base else None, "各組發動率%": [round(r * 100, 1) for r in rates],
                     "各組平均R": [round(x, 2) for x in rr], "各組下限": [round(x, 4) for x in lo]})
    return sorted(rows, key=lambda r: -abs(r["最高組發動率%"] - r["最低組發動率%"]))


def signal_rates(df: pd.DataFrame, extra: dict[str, pd.Series] | None = None, col: str = "hit") -> list[dict]:
    """既有訊號的發動率與平均 R：突破、拉回、美股領先、同題材有龍頭突破…，當成對照。"""
    base = float(df[col].mean())
    flags = {"收盤放量突破（現行）": df["sig_a"] > 0, "拉回（現行）": (df["sig_b"] > 0) & (df["sig_a"] == 0),
             "美股領先": df["us_lead"] > 0, "同題材近3日有突破": df["th_brk"] >= 1,
             "120日相對強度前20%": df["rs120"] >= 0.8, "離20日高3%內未突破": (df["gap"] > 0) & (df["gap"] <= 0.03)}
    flags.update(extra or {})
    rows = []
    for name, m in flags.items():
        m = m.to_numpy() if hasattr(m, "to_numpy") else m
        if m.sum() < 30:
            continue
        sub = df[m]
        rows.append({"訊號": name, "筆數": int(m.sum()), "發動率%": round(float(sub[col].mean()) * 100, 1),
                     "倍數": round(float(sub[col].mean()) / base, 2), "平均R": round(float(sub["rr"].mean()), 2),
                     "平均5日%": round(float(sub["fwd"].mean()) * 100, 2)})
    return rows


def evaluate(df: pd.DataFrame, embargo_days: int = 5, col: str = "hit") -> dict:
    """時間切半、互換訓練與測試。回傳每種模型在「沒看過的那一半」的表現。"""
    dates = np.sort(df["date"].unique())
    cut = dates[len(dates) // 2]
    emb = pd.Timedelta(days=embargo_days * 2)
    first = df[df["date"] < cut - emb]
    second = df[df["date"] >= cut]
    out = {"切點": str(pd.Timestamp(cut).date()), "前半段筆數": int(len(first)), "後半段筆數": int(len(second)),
           "前半段基準發動率%": round(float(first[col].mean()) * 100, 1),
           "後半段基準發動率%": round(float(second[col].mean()) * 100, 1),
           "前半段基準平均R": round(float(first["rr"].mean()), 2), "後半段基準平均R": round(float(second["rr"].mean()), 2),
           "模型": {}}
    for kind, l2 in (("linear", 5.0), ("binned", 20.0)):
        res = {}
        for name, (tr, te) in {"前半訓練→後半測試": (first, second), "後半訓練→前半測試": (second, first)}.items():
            m = Model(kind, l2).fit(tr, col)
            p = m.predict(te)
            res[name] = {"提升表": lift_table(te, p, col=col), "校準": calibration(te, p, col=col)}
            brk = (te["sig_a"] > 0).to_numpy()                      # 只看「現行放量突破」那群：分數能不能把其中的好壞分開
            if brk.sum() >= 100:
                res[name]["現行突破群的提升表"] = lift_table(te[brk], p[brk], col=col)
        out["模型"][kind] = res
    return out


# ---------------------------------------------------------------- 把分數放進策略（逐季向前訓練，沒有偷看未來）

def walk_forward_scores(df: pd.DataFrame, col: str = "hit_r", kind: str = "linear", l2: float = 5.0,
                        min_train_days: int = 250, step_days: int = 63, embargo_days: int = 10) -> pd.DataFrame:
    """逐季向前：每一季只用「那一季開始前（再空 embargo_days 天）」的資料訓練，預測那一季。
    回傳每列（date, code）的分數；前 min_train_days 天沒有分數。"""
    dates = pd.DatetimeIndex(df["date"].drop_duplicates().sort_values())
    start = dates[0] + pd.Timedelta(days=min_train_days)
    parts = []
    t0 = start
    while t0 <= dates[-1]:
        t1 = t0 + pd.Timedelta(days=step_days)
        train = df[df["date"] < t0 - pd.Timedelta(days=embargo_days)]
        test = df[(df["date"] >= t0) & (df["date"] < t1)]
        if len(train) > 2000 and len(test):
            m = Model(kind, l2).fit(train, col)
            parts.append(pd.DataFrame({"date": test["date"].to_numpy(), "code": test["code"].to_numpy(),
                                       "score": m.predict(test)}))
        t0 = t1
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["date", "code", "score"])


def top_signals(scores: pd.DataFrame, n: int = 3, name: str = "高分") -> dict:
    """每天分數最高的前 n 檔 → 回測引擎的額外訊號 {訊號日: [(分數, 代號, 名稱)]}。"""
    out: dict = {}
    for ts, g in scores.groupby("date"):
        top = g.nlargest(n, "score")
        out[ts] = [(float(r.score), str(r.code), name) for r in top.itertuples()]
    return out
