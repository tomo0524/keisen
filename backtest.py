#!/usr/bin/env python3
"""自作チャート条件「75日線下の戻り(仮称)」のバックテスト。東証全銘柄・過去5年。

GitHub Actions で手動実行する想定。結果は backtest/ フォルダに保存する。
  - 銘柄一覧: 日本取引所グループの上場銘柄一覧(プライム・スタンダード・グロースの内国株式)
  - 株価: Yahoo Finance(yfinance)。個人の検証目的で利用すること

条件(すべて当日の終値時点で判定)
  流動性: 売買代金(終値×出来高)の5日平均が 1億円以上 30億円未満
  1. 終値 < 200日線
  2. 75日線が下向き(25営業日前の75日線 > 当日の75日線)
  3. 直近60営業日(当日を除く)に、日中の値幅が75日線にかかった日がある
  4. 25日線または40日線が上向き(25営業日前 < 当日)
  5. 75日線 > 終値 > 25日線
  6. 直近63営業日(約3か月)に、出来高 >= 前日までの5日平均出来高×2 の日がある
シグナル: 条件が初めて揃った日(直前20営業日に同じシグナルがない)。翌日の始値で買う想定。
"""
import datetime as dt
import io
import json
import math
import os
import sys
import time
import urllib.request

import numpy as np
import pandas as pd

CFG = {
    "turnover_min": 1e8, "turnover_max": 3e9, "turnover_days": 5,
    "touch_lookback": 60, "spike_lookback": 63, "spike_mult": 2.0, "spike_avg_days": 5,
    "slope_lag": 25, "cooldown": 20,
    "horizons": [5, 10, 20, 40, 60], "years": 5, "split_years_first": 3,
    "cost_roundtrip": 0.003,
}
JPX_URLS = [
    "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx",
    "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls",
]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Referer": "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"}


# ---------- 判定(ネットに依存しない。テスト対象) ----------

def roll_mean(a, n):
    return pd.Series(a).rolling(n, min_periods=n).mean().to_numpy()


def roll_any(mask, n):
    return pd.Series(mask.astype(float)).rolling(n, min_periods=1).max().to_numpy() > 0


def shift(a, k):
    out = np.full(len(a), np.nan)
    if k < len(a):
        out[k:] = a[:-k] if k else a
    return out


def condition(o, h, l, c, v, cfg=CFG):
    """各日について条件が揃っているか(bool配列)と、途中の指標を返す。"""
    ma25, ma40, ma75, ma200 = (roll_mean(c, n) for n in (25, 40, 75, 200))
    lag = cfg["slope_lag"]
    turn = roll_mean(c * v, cfg["turnover_days"])
    vavg_prev = shift(roll_mean(v, cfg["spike_avg_days"]), 1)
    with np.errstate(invalid="ignore"):
        spike = (v >= cfg["spike_mult"] * vavg_prev) & (vavg_prev > 0)
        touch = (l <= ma75) & (h >= ma75)
        touch_recent = shift(roll_any(touch, cfg["touch_lookback"]).astype(float), 1) > 0
        spike_recent = roll_any(spike, cfg["spike_lookback"])
        cond = (
            (turn >= cfg["turnover_min"]) & (turn < cfg["turnover_max"])
            & (c < ma200)
            & (ma75 < shift(ma75, lag))
            & touch_recent
            & ((ma25 > shift(ma25, lag)) | (ma40 > shift(ma40, lag)))
            & (ma75 > c) & (c > ma25)
            & spike_recent
        )
    cond &= ~np.isnan(ma200)
    return cond, {"ma25": ma25, "ma75": ma75, "ma200": ma200, "turn": turn, "spike": spike}


def events(cond, cooldown):
    """条件が初めて揃った日のインデックス(直前cooldown日に同じシグナルがないもの)。"""
    out, last = [], -10 ** 9
    for i in range(1, len(cond)):
        if cond[i] and not cond[i - 1] and i - last > cooldown:
            out.append(i)
            last = i
    return out


def fwd_returns(o, c, i, horizons):
    """シグナル日iの翌日始値で買い、i+H日目の終値で売った場合の騰落率。"""
    if i + 1 >= len(o) or not o[i + 1] > 0:
        return None
    e = o[i + 1]
    return {H: (c[i + H] / e - 1) if i + H < len(c) else None for H in horizons}


# ---------- 集計 ----------

def stats(x):
    x = np.asarray([v for v in x if v is not None and not math.isnan(v)])
    if len(x) == 0:
        return {"n": 0}
    sd = x.std(ddof=1) if len(x) > 1 else float("nan")
    return {
        "n": int(len(x)), "mean": float(x.mean()), "median": float(np.median(x)),
        "win": float((x > 0).mean()), "t": float(x.mean() / (sd / math.sqrt(len(x)))) if len(x) > 1 and sd > 0 else None,
        "p10": float(np.percentile(x, 10)), "p90": float(np.percentile(x, 90)),
    }


VARIANTS = {
    "A": "元の条件(条件が揃った翌日の始値で買う)",
    "B": "Aの後20営業日以内に、終値が75日線を上抜けた翌日の始値で買う",
    "C": "Aのうち、75日線までの距離が3%以内のものだけ",
}


def variant_events(cond, c, ma75, cfg):
    A = events(cond, cfg["cooldown"])
    B, last = [], -10 ** 9
    for i in A:
        for k in range(i + 1, min(len(c), i + 21)):
            if c[k] > ma75[k] and c[k - 1] <= ma75[k - 1]:
                if k - last > cfg["cooldown"]:
                    B.append(k)
                    last = k
                break
    C = [i for i in A if ma75[i] / c[i] - 1 <= 0.03]
    return {"A": A, "B": B, "C": C}


def run(frames, names, today=None, cfg=CFG):
    """frames: {code: DataFrame(Open,High,Low,Close,Volume, index=日付)}"""
    H = cfg["horizons"]
    last_date = max(df.index[-1] for df in frames.values())
    eval_start = last_date - pd.DateOffset(years=cfg["years"])
    split = eval_start + pd.DateOffset(years=cfg["split_years_first"])
    base_parts = {h: [] for h in H}
    sigs, current = [], []
    dropped = {"jump_windows": 0, "stocks_with_jump": 0}

    for code, df in frames.items():
        df = df.dropna()
        df = df[(df["Open"] > 0) & (df["High"] >= df["Low"])]
        if len(df) < 260:
            continue
        o, h, l, c, v = (df[k].to_numpy(dtype=float) for k in ("Open", "High", "Low", "Close", "Volume"))
        d = df.index
        n = len(c)
        # ありえない値動き(前日終値比で3倍超・1/3未満)を含む期間は集計から除く
        prev = np.append(np.nan, c[:-1])
        with np.errstate(divide="ignore", invalid="ignore"):
            jump = (np.maximum(o, c) / prev > 3) | (np.minimum(o, c) / prev < 1 / 3)
        jump[0] = False
        if jump.any():
            dropped["stocks_with_jump"] += 1
        jc = np.cumsum(jump)
        cond, ind = condition(o, h, l, c, v, cfg)

        def clean(i, hz):  # i+1 .. i+hz の間に異常値がない
            return i + hz < n and jc[i + hz] - jc[i] == 0

        liquid = (ind["turn"] >= cfg["turnover_min"]) & (ind["turn"] < cfg["turnover_max"])
        o_next = np.append(o[1:], np.nan)
        for hz in H:
            exitc = np.append(c[hz:], [np.nan] * hz)
            with np.errstate(divide="ignore", invalid="ignore"):
                r = exitc / o_next - 1
            jc_end = np.append(jc[hz:], [jc[-1]] * hz)
            okj = (jc_end - jc) == 0
            dropped["jump_windows"] += int((liquid & ~okj).sum())
            ok = liquid & np.isfinite(r) & okj & (d >= eval_start)
            if ok.any():
                base_parts[hz].append(pd.Series(r[ok], index=d[ok]))
        for var, idxs in variant_events(cond, c, ind["ma75"], cfg).items():
            for i in idxs:
                if d[i] < eval_start or i + 1 >= n or not o[i + 1] > 0:
                    continue
                e = o[i + 1]
                j1, j2 = i + 1, min(n, i + 21)
                rec = {"variant": var, "code": code, "name": names.get(code, ""), "date": d[i].strftime("%Y-%m-%d"),
                       "entry": float(e), "close": float(c[i]),
                       "ma75_gap": float(c[i] / ind["ma75"][i] - 1),
                       "mfe20": float(h[j1:j2].max() / e - 1), "mae20": float(l[j1:j2].min() / e - 1),
                       "cross75_20": bool(np.any(c[j1:j2] > ind["ma75"][j1:j2]))}
                for hz in H:
                    rec[f"r{hz}"] = float(c[i + hz] / e - 1) if clean(i, hz) else None
                sigs.append(rec)
        if cond[-1] and d[-1] == last_date:
            k = n - 1
            while k > 0 and cond[k - 1]:
                k -= 1
            sp = np.where(ind["spike"][max(0, n - cfg["spike_lookback"]):])[0]
            current.append({"code": code, "name": names.get(code, ""), "close": float(c[-1]),
                            "ma25": round(float(ind["ma25"][-1]), 1), "ma75": round(float(ind["ma75"][-1]), 1),
                            "ma200": round(float(ind["ma200"][-1]), 1), "to_ma75": round(float(ind["ma75"][-1] / c[-1] - 1), 4),
                            "turnover5_oku": round(float(ind["turn"][-1]) / 1e8, 1), "since": d[k].strftime("%Y-%m-%d"),
                            "last_spike_days_ago": int(len(ind["spike"][max(0, n - cfg["spike_lookback"]):]) - 1 - sp[-1]) if len(sp) else None})

    # 比較対象: 同じ日に流動性条件を満たした全銘柄の「中央値」と「平均」
    base_all = {hz: (pd.concat(base_parts[hz]) if base_parts[hz] else pd.Series(dtype=float)) for hz in H}
    base_med = {hz: base_all[hz].groupby(level=0).median() for hz in H}
    base_mean = {hz: base_all[hz].groupby(level=0).mean() for hz in H}
    for s in sigs:
        dd = pd.Timestamp(s["date"])
        for hz in H:
            r = s[f"r{hz}"]
            bm, bmn = base_med[hz].get(dd, np.nan), base_mean[hz].get(dd, np.nan)
            s[f"x{hz}"] = (r - bm) if r is not None and not math.isnan(bm) else None
            s[f"xm{hz}"] = (r - bmn) if r is not None and not math.isnan(bmn) else None

    def block(rows):
        out = {}
        for hz in H:
            out[str(hz)] = {"ret": stats([r[f"r{hz}"] for r in rows]),
                            "excess_vs_median": stats([r[f"x{hz}"] for r in rows]),
                            "excess_vs_mean": stats([r[f"xm{hz}"] for r in rows]),
                            "beat_median_rate": (float(np.mean([r[f"x{hz}"] > 0 for r in rows if r[f"x{hz}"] is not None]))
                                                 if any(r[f"x{hz}"] is not None for r in rows) else None)}
        return out

    def base_block(lo, hi):
        out = {}
        for hz in H:
            b = base_all[hz]
            b = b[(b.index >= lo) & (b.index < hi)]
            out[str(hz)] = {"n": int(len(b)), "mean": float(b.mean()) if len(b) else None,
                            "median": float(b.median()) if len(b) else None, "win": float((b > 0).mean()) if len(b) else None}
        return out

    far = last_date + pd.Timedelta(days=1)
    res_var = {}
    for var in VARIANTS:
        vs = [s for s in sigs if s["variant"] == var]
        first = [s for s in vs if pd.Timestamp(s["date"]) < split]
        second = [s for s in vs if pd.Timestamp(s["date"]) >= split]
        years = sorted({s["date"][:4] for s in vs})
        res_var[var] = {
            "desc": VARIANTS[var], "signals": len(vs),
            "all": block(vs), "first": block(first), "second": block(second),
            "by_year": {y: block([s for s in vs if s["date"][:4] == y]) for y in years},
            "path20": {"mfe": stats([s["mfe20"] for s in vs]), "mae": stats([s["mae20"] for s in vs]),
                       "cross75_rate": float(np.mean([s["cross75_20"] for s in vs])) if vs else None},
        }
    result = {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config": cfg, "period": [eval_start.strftime("%Y-%m-%d"), last_date.strftime("%Y-%m-%d")],
        "split": split.strftime("%Y-%m-%d"), "stocks_tested": len(frames), "cleaning": dropped,
        "signals": res_var["A"]["signals"], "variants": res_var,
        "baseline": {"all": base_block(eval_start, far), "first": base_block(eval_start, split), "second": base_block(split, far)},
        "current": sorted(current, key=lambda x: x["to_ma75"]),
    }
    return result, sigs


# ---------- 取得 ----------

def ensure(pkg):
    try:
        __import__(pkg)
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", pkg])


def norm_code(x):
    t = str(x).strip()
    if t.endswith(".0"):
        t = t[:-2]
    return t


def parse_universe(df):
    """JPXの上場銘柄一覧(表)から、プライム・スタンダード・グロースの内国株式を取り出す。"""
    col = lambda *keys: next(c for c in df.columns if all(k in str(c) for k in keys))
    c_code, c_name, c_mk = col("コード"), col("銘柄名"), col("市場")
    mk = df[c_mk].fillna("").astype(str)
    sel = df[mk.str.contains("内国株式") & mk.str.contains("プライム|スタンダード|グロース")]
    names = {norm_code(r[c_code]): str(r[c_name]).strip() for _, r in sel.iterrows()}
    markets = {norm_code(r[c_code]): str(r[c_mk]).replace("（内国株式）", "").strip() for _, r in sel.iterrows()}
    return names, markets


def load_universe():
    errs = []
    for url in JPX_URLS:
        try:
            ensure("openpyxl" if url.endswith("x") else "xlrd")
            raw = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read()
            names, markets = parse_universe(pd.read_excel(io.BytesIO(raw), dtype=str))
            if len(names) > 1000:
                print("銘柄一覧:", url)
                return names, markets
            errs.append(f"{url}: 銘柄数が少ない({len(names)})")
        except Exception as e:
            errs.append(f"{url}: {type(e).__name__}: {e}")
    raise RuntimeError("上場銘柄一覧を読み込めませんでした / " + " | ".join(errs))


def download(codes, start):
    import yfinance as yf
    frames, failed = {}, []
    B = 80
    for i in range(0, len(codes), B):
        batch = [c + ".T" for c in codes[i:i + B]]
        data = None
        for attempt in range(3):
            try:
                data = yf.download(batch, start=start, interval="1d", auto_adjust=False, group_by="ticker",
                                   threads=True, progress=False)
                break
            except Exception as e:
                print("retry", i, e)
                time.sleep(15 * (attempt + 1))
        for t in batch:
            try:
                sub = data[t] if data is not None and len(batch) > 1 else data
                sub = sub[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Open", "High", "Low", "Close"])
                sub = sub[sub["Close"] > 0]
                if len(sub) >= 260:
                    frames[t[:-2]] = sub.astype(float)
                else:
                    failed.append(t[:-2])
            except Exception:
                failed.append(t[:-2])
        print(f"{min(i + B, len(codes))}/{len(codes)} 取得済み {len(frames)}", flush=True)
        time.sleep(2)
    return frames, failed


def main(outdir="backtest"):
    os.makedirs(outdir, exist_ok=True)
    names, markets = load_universe()
    print("対象", len(names), "銘柄")
    start = (dt.date.today() - dt.timedelta(days=365 * CFG["years"] + 420)).isoformat()
    frames, failed = download(sorted(names), start)
    result, sigs = run(frames, names)
    result["universe"] = len(names)
    result["download_failed"] = len(failed)
    for c in result["current"]:
        c["market"] = markets.get(c["code"], "")
    with open(os.path.join(outdir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1, default=lambda x: None)
    pd.DataFrame(sigs).to_csv(os.path.join(outdir, "signals.csv"), index=False, encoding="utf-8-sig")
    pd.DataFrame(result["current"]).to_csv(os.path.join(outdir, "current.csv"), index=False, encoding="utf-8-sig")
    print("シグナル(A)", result["signals"], "件 / 現在該当", len(result["current"]), "銘柄", result["cleaning"])
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
