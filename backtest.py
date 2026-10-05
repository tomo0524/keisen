#!/usr/bin/env python3
"""自作チャート条件「75日線下の戻り(仮称)」のバックテスト。東証全銘柄・過去5年。

GitHub Actions で手動実行する想定。結果は backtest/ フォルダに保存する。
  - 銘柄一覧: 日本取引所グループの上場銘柄一覧(プライム・スタンダード・グロースの内国株式)
  - 株価: Yahoo Finance(yfinance)。個人の検証目的で利用すること

条件(すべて当日の終値時点で判定)
  流動性: 売買代金(終値×出来高)の5日平均が 5,000万円以上 10億円以下
  1. 終値 < 200日線
  2. 75日線が下向き(25営業日前の75日線 > 当日の75日線)
  3. 直近60営業日(当日を除く)に、日中の値幅が75日線にかかった日がある
  4. 25日線または40日線が上向き(25営業日前 < 当日)
  5. 75日線 > 終値 > 25日線
  6. 直近63営業日(約3か月)に、出来高 >= 前日までの5日平均出来高×2 の日がある
シグナル: 条件が初めて揃った日(直前20営業日に同じシグナルがない)。
買い方: 翌日の始値では買わない。シグナル日の終値の3%下に指値を出し、10営業日以内に安値が届いたら約定
       (寄り付きが指値より下なら始値で約定)。売りは約定日の前日から数えて H 営業日目の終値。
損切り: 約定日以降、終値が25日線×0.97を下回った日が出たら翌日の始値で売る。
利確: 約定の翌日以降、高値が買値×(1+利確幅)に届いたらその価格で売る(寄りが上なら始値)。
      利確幅は なし/5%/10%/15%/20% を比較。同じ日に両方なら利確を優先。
最長保有: どちらにもかからなければ、約定日から数えて H 営業日目(約定日=1日目)の終値で売る。
比較対象: 流動性条件を満たす全銘柄・全日に、まったく同じ売買ルール(指値→損切り→利確)を当てはめたもの。
比較: D = Aの後20営業日以内に、出来高2倍以上で終値が75日線を上抜けた日をシグナルとする。
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
    "turnover_min": 5e7, "turnover_max": 1e9, "turnover_days": 5,
    "touch_lookback": 60, "spike_lookback": 63, "spike_mult": 2.0, "spike_avg_days": 5,
    "slope_lag": 25, "cooldown": 20,
    "horizons": [5, 10, 20, 40, 60], "years": 5, "split_years_first": 3,
    "cost_roundtrip": 0.003,
    "limit_drop": 0.03, "limit_wait": 10, "stop_below_ma25": 0.03,
    "take_profits": [None, 0.05, 0.10, 0.15, 0.20],
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
            (turn >= cfg["turnover_min"]) & (turn <= cfg["turnover_max"])
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
    "A": "元の条件。条件が揃った日の終値の3%下に指値、10営業日以内に約定したら買う",
    "D": "Aの後20営業日以内に、出来高2倍以上(前日までの5日平均比)で終値が75日線を上抜けた日。その日の終値の3%下に指値、10営業日以内に約定したら買う",
}


def variant_signals(cond, c, ma75, spike, cfg):
    """A: 条件が初めて揃った日。D: Aの後20営業日以内に、出来高2倍で終値が75日線を上抜けた日。"""
    n = len(c)
    A = events(cond, cfg["cooldown"])
    D, last = [], -10 ** 9
    for i in A:
        for k in range(i + 1, min(n, i + 21)):
            if c[k] > ma75[k] and c[k - 1] <= ma75[k - 1]:
                if spike[k] and k - last > cfg["cooldown"]:
                    D.append(k)
                    last = k
                break
    return {"A": A, "D": D}


TP_KEY = lambda tp: "none" if tp is None else f"{int(round(tp * 100))}"


def simulate(o, h, l, c, stopf, cfg):
    """各日iを「シグナル日」とみなして売買をまとめて計算する(シグナル側も比較対象も同じこの計算を使う)。
    買い: i+1〜i+limit_wait日目に安値が c[i]×(1-limit_drop) 以下になった最初の日fに、min(始値, 指値)で約定。
    売り(保有期間H・利確幅tp): 約定日fを1日目として、
      - 利確: f+1日目以降、高値 >= 買値×(1+tp) の日に max(始値, 目標) で売る
      - 損切り: f日目以降、終値 < 25日線×(1-stop) の日の翌日の始値で売る(翌日が最終日以前の場合)
      - どちらもなし: f+H-1日目の終値で売る
    戻り値: f(約定日, 未約定は-1), entry, tried(待ち期間を最後まで観測できたか), res[(H, tpkey)] = (騰落率, 保有日数, 決済の種類)"""
    n = len(c)
    I = np.arange(n)
    lim = c * (1 - cfg["limit_drop"])
    f = np.full(n, -1)
    entry = np.full(n, np.nan)
    for w in range(1, cfg["limit_wait"] + 1):
        k = I + w
        kk = np.minimum(k, n - 1)
        hit = (k < n) & (f < 0) & (l[kk] <= lim)
        f[hit] = k[hit]
        entry[hit] = np.minimum(o[kk[hit]], lim[hit])
    tried = I + cfg["limit_wait"] < n
    res = {}
    for hz in cfg["horizons"]:
        end = f + hz - 1
        valid = (f >= 0) & (end < n)
        for tp in cfg["take_profits"]:
            alive = valid.copy()
            px = np.full(n, np.nan)
            days = np.zeros(n, dtype=int)
            kind = np.zeros(n, dtype=int)  # 0=期間満了 1=損切り 2=利確
            tgt = entry * (1 + tp) if tp is not None else None
            for t in range(hz):
                kk = np.clip(f + t, 0, n - 1)
                if tp is not None and t >= 1:
                    m = alive & (h[kk] >= tgt)
                    px[m] = np.maximum(o[kk[m]], tgt[m])
                    days[m], kind[m] = t + 1, 2
                    alive &= ~m
                if t <= hz - 2:
                    m = alive & stopf[kk]
                    px[m] = o[np.minimum(kk[m] + 1, n - 1)]
                    days[m], kind[m] = t + 2, 1
                    alive &= ~m
            px[alive] = c[np.clip(end, 0, n - 1)[alive]]
            days[alive] = hz
            with np.errstate(invalid="ignore", divide="ignore"):
                r = px / entry - 1
            r[~valid] = np.nan
            res[(hz, TP_KEY(tp))] = (r, days, kind)
    return f, entry, tried, res


def run(frames, names, today=None, cfg=CFG):
    """frames: {code: DataFrame(Open,High,Low,Close,Volume, index=日付)}"""
    H = cfg["horizons"]
    TPK = [TP_KEY(tp) for tp in cfg["take_profits"]]
    last_date = max(df.index[-1] for df in frames.values())
    eval_start = last_date - pd.DateOffset(years=cfg["years"])
    split = eval_start + pd.DateOffset(years=cfg["split_years_first"])
    base = {(hz, tk): [] for hz in H for tk in TPK}
    base_fill = {"tried": 0, "filled": 0}
    sigs, current = [], []
    fills = {v: {"tried": 0, "filled": 0} for v in VARIANTS}
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
        with np.errstate(invalid="ignore"):
            stopf = c < ind["ma25"] * (1 - cfg["stop_below_ma25"])
        f, entry, tried, sim = simulate(o, h, l, c, stopf, cfg)
        in_period = np.asarray(d >= eval_start)

        def clean(i, hz):  # シグナル日i〜最終日に異常値がない
            if f[i] < 0:
                return False
            end = f[i] + hz - 1
            return end < n and jc[end] - jc[i] == 0

        # 比較対象: 流動性条件(同じ売買代金の範囲)を満たす全日に、同じ売買ルールを当てはめる
        liquid = (ind["turn"] >= cfg["turnover_min"]) & (ind["turn"] <= cfg["turnover_max"])
        bt_ = liquid & tried & in_period
        base_fill["tried"] += int(bt_.sum())
        base_fill["filled"] += int((bt_ & (f >= 0)).sum())
        I = np.arange(n)
        for hz in H:
            end = np.clip(f + hz - 1, 0, n - 1)
            okj = (f >= 0) & (f + hz - 1 < n) & ((jc[end] - jc) == 0)
            dropped["jump_windows"] += int((liquid & (f >= 0) & (f + hz - 1 < n) & ~okj).sum())
            for tk in TPK:
                r = sim[(hz, tk)][0]
                ok = liquid & in_period & okj & np.isfinite(r)
                if ok.any():
                    base[(hz, tk)].append(pd.Series(r[ok], index=d[ok]))

        vev = variant_signals(cond, c, ind["ma75"], ind["spike"], cfg)
        for var, sig in vev.items():
            for i in sig:
                if not in_period[i]:
                    continue
                if tried[i]:
                    fills[var]["tried"] += 1
                    fills[var]["filled"] += int(f[i] >= 0)
                if f[i] < 0:
                    continue
                fi, e = int(f[i]), float(entry[i])
                j1, j2 = fi + 1, min(n, fi + 21)  # 最大上昇・下落は約定の翌日から20日
                if j1 >= j2:
                    continue
                rec = {"variant": var, "code": code, "name": names.get(code, ""), "date": d[i].strftime("%Y-%m-%d"),
                       "fill_date": d[fi].strftime("%Y-%m-%d"), "entry": e, "signal_close": float(c[i]),
                       "ma75_gap": float(c[i] / ind["ma75"][i] - 1),
                       "mfe20": float(h[j1:j2].max() / e - 1), "mae20": float(l[j1:j2].min() / e - 1)}
                for hz in H:
                    cl = clean(i, hz)
                    for tk in TPK:
                        r, days, kind = sim[(hz, tk)]
                        ok = cl and np.isfinite(r[i])
                        rec[f"r{hz}_{tk}"] = float(r[i]) if ok else None
                        rec[f"days{hz}_{tk}"] = int(days[i]) if ok else None
                        rec[f"kind{hz}_{tk}"] = int(kind[i]) if ok else None
                sigs.append(rec)
        if cond[-1] and d[-1] == last_date:
            k = n - 1
            while k > 0 and cond[k - 1]:
                k -= 1
            sp = np.where(ind["spike"][max(0, n - cfg["spike_lookback"]):])[0]
            current.append({"code": code, "name": names.get(code, ""), "close": float(c[-1]),
                            "limit_price": round(float(c[-1]) * (1 - cfg["limit_drop"]), 1),
                            "ma25": round(float(ind["ma25"][-1]), 1), "ma75": round(float(ind["ma75"][-1]), 1),
                            "ma200": round(float(ind["ma200"][-1]), 1), "to_ma75": round(float(ind["ma75"][-1] / c[-1] - 1), 4),
                            "stop_line": round(float(ind["ma25"][-1]) * (1 - cfg["stop_below_ma25"]), 1),
                            "turnover5_oku": round(float(ind["turn"][-1]) / 1e8, 2), "since": d[k].strftime("%Y-%m-%d"),
                            "last_spike_days_ago": int(len(ind["spike"][max(0, n - cfg["spike_lookback"]):]) - 1 - sp[-1]) if len(sp) else None})

    # 比較対象を日付ごとに集計し、シグナルとの差を計算
    bmean, bmed, ball = {}, {}, {}
    for key, parts in base.items():
        sr = pd.concat(parts) if parts else pd.Series(dtype=float)
        g = sr.groupby(level=0)
        bmean[key], bmed[key], ball[key] = g.mean(), g.median(), sr
    for s in sigs:
        dd = pd.Timestamp(s["date"])
        for hz in H:
            for tk in TPK:
                r = s[f"r{hz}_{tk}"]
                m1, m2 = bmean[(hz, tk)].get(dd, np.nan), bmed[(hz, tk)].get(dd, np.nan)
                s[f"xm{hz}_{tk}"] = (r - m1) if r is not None and not math.isnan(m1) else None
                s[f"x{hz}_{tk}"] = (r - m2) if r is not None and not math.isnan(m2) else None

    def block(rows):
        out = {}
        for hz in H:
            for tk in TPK:
                vals = [r for r in rows if r[f"r{hz}_{tk}"] is not None]
                kinds = [r[f"kind{hz}_{tk}"] for r in vals]
                out[f"{hz}_{tk}"] = {
                    "ret": stats([r[f"r{hz}_{tk}"] for r in vals]),
                    "excess_vs_mean": stats([r[f"xm{hz}_{tk}"] for r in vals]),
                    "beat_median_rate": float(np.mean([r[f"x{hz}_{tk}"] > 0 for r in vals if r[f"x{hz}_{tk}"] is not None])) if vals else None,
                    "stop_rate": float(np.mean([k == 1 for k in kinds])) if vals else None,
                    "tp_rate": float(np.mean([k == 2 for k in kinds])) if vals else None,
                    "avg_days": float(np.mean([r[f"days{hz}_{tk}"] for r in vals])) if vals else None,
                }
        return out

    def base_block(lo, hi):
        out = {}
        for (hz, tk), sr in ball.items():
            b = sr[(sr.index >= lo) & (sr.index < hi)]
            out[f"{hz}_{tk}"] = {"n": int(len(b)), "mean": float(b.mean()) if len(b) else None,
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
            "path20": {"mfe": stats([s["mfe20"] for s in vs]), "mae": stats([s["mae20"] for s in vs])},
        }
    result = {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config": cfg, "period": [eval_start.strftime("%Y-%m-%d"), last_date.strftime("%Y-%m-%d")],
        "split": split.strftime("%Y-%m-%d"), "stocks_tested": len(frames), "cleaning": dropped,
        "signals": res_var["A"]["signals"], "variants": res_var,
        "limit_fill": {v: dict(x, rate=(x["filled"] / x["tried"]) if x["tried"] else None) for v, x in fills.items()},
        "baseline_fill": dict(base_fill, rate=(base_fill["filled"] / base_fill["tried"]) if base_fill["tried"] else None),
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


def _fetch_batch(batch, start):
    import yfinance as yf
    return yf.download(batch, start=start, interval="1d", auto_adjust=False, group_by="ticker",
                       threads=True, progress=False, timeout=30)


def download(codes, start, batch_limit=180, total_limit=90 * 60):
    """80銘柄ずつ取得。1回180秒・全体90分を超えたら打ち切って、取れた分で進む。"""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout
    frames, failed, skipped = {}, [], 0
    B, t0 = 80, time.time()
    for i in range(0, len(codes), B):
        batch = [c + ".T" for c in codes[i:i + B]]
        if time.time() - t0 > total_limit:
            print("全体の時間上限に達したため、取得を打ち切ります", flush=True)
            failed += [t[:-2] for t in batch]
            continue
        data = None
        for attempt in range(2):
            # 毎回新しい作業枠を使う(固まった取得が残っても、次の取得を妨げない)
            pool = ThreadPoolExecutor(max_workers=1)
            fut = pool.submit(_fetch_batch, batch, start)
            try:
                data = fut.result(timeout=batch_limit)
                break
            except FTimeout:
                print(f"{i}: {batch_limit}秒を超えたため、この分を飛ばします", flush=True)
                skipped += 1
                break
            except Exception as e:
                print("retry", i, e, flush=True)
                time.sleep(15)
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
        for t in batch:
            try:
                if data is None:
                    raise KeyError(t)
                # 列が「銘柄→項目」の2段構造なら銘柄で取り出す(1銘柄だけの回も同じ)
                if isinstance(data.columns, pd.MultiIndex):
                    sub = data[t] if t in data.columns.get_level_values(0) else data.droplevel(1, axis=1)
                else:
                    sub = data
                sub = sub[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Open", "High", "Low", "Close"])
                sub = sub[sub["Close"] > 0]
                if len(sub) >= 260:
                    frames[t[:-2]] = sub.astype(float)
                else:
                    failed.append(t[:-2])
            except Exception:
                failed.append(t[:-2])
        print(f"{min(i + B, len(codes))}/{len(codes)} 取得済み {len(frames)} ({int(time.time() - t0)}秒)", flush=True)
        time.sleep(2)
    print(f"取得完了: {len(frames)}銘柄 / 失敗 {len(failed)} / 時間切れで飛ばした回数 {skipped}", flush=True)
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
    code = main(*sys.argv[1:])
    sys.stdout.flush()
    os._exit(code)  # 固まった取得処理が残っていても、必ずここで終了する
