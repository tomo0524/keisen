#!/usr/bin/env python3
"""罫線スクリーナー「地合い」タブ用の市場データを集めて market.json に書き出す。

GitHub Actions で毎日実行する想定。標準ライブラリだけで動き、APIキーは不要。
取得元:
  - 米セントルイス連銀 FRED(CSV): 日経平均, S&P500, NASDAQ, VIX, 米10年債, FRB政策金利, WTI, ドル円, 米CPI, 米失業率
  - 財務省 国債金利情報(CSV): 日本10年債
  - Stooq(CSV): 日経平均, TOPIX(FREDより早く更新されるため優先)
取れなかった項目は前回の market.json の値を残す。日銀政策金利と東京都区部コアCPIはアプリで手入力。
"""
import csv
import datetime as dt
import io
import json
import re
import sys
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (keisen-screener market fetch)"}
TODAY = dt.date.today()


def get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------- 解析(ネットに依存しない部分。テスト対象) ----------

def parse_fred(text):
    """FREDのCSV(1列目=日付, 2列目=値)を [(YYYY-MM-DD, float)] にする。欠損('.'や空)は除く。"""
    rows = []
    for r in csv.reader(io.StringIO(text)):
        if len(r) < 2 or not re.match(r"\d{4}-\d{2}-\d{2}", r[0]):
            continue
        try:
            rows.append((r[0][:10], float(r[1])))
        except ValueError:
            continue
    return sorted(rows)


def parse_stooq(text):
    """StooqのCSV(Date,Open,High,Low,Close,Volume)を [(日付, 終値)] にする。"""
    rows = []
    rd = csv.reader(io.StringIO(text))
    head = next(rd, None)
    if not head:
        return rows
    h = [x.strip().lower() for x in head]
    if "date" not in h or "close" not in h:
        return rows
    di, ci = h.index("date"), h.index("close")
    for r in rd:
        try:
            rows.append((r[di][:10], float(r[ci])))
        except (ValueError, IndexError):
            continue
    return sorted(rows)


ERA = {"R": 2018, "H": 1988, "S": 1925}


def mof_date(s):
    s = s.strip()
    m = re.match(r"([RHS])(\d+)\.(\d+)\.(\d+)$", s)
    if m:
        return dt.date(ERA[m.group(1)] + int(m.group(2)), int(m.group(3)), int(m.group(4))).isoformat()
    m = re.match(r"(\d{4})[/.-](\d{1,2})[/.-](\d{1,2})$", s)
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    return None


def parse_mof(text, col="10年"):
    """財務省 jgbcm.csv(基準日, 1年, 2年, ... 10年 ...)から10年債の [(日付, 利回り)] を取り出す。"""
    rows, idx = [], None
    for r in csv.reader(io.StringIO(text)):
        cells = [c.strip() for c in r]
        if idx is None:
            if col in cells:
                idx = cells.index(col)
            continue
        if len(cells) <= idx:
            continue
        d = mof_date(cells[0])
        try:
            v = float(cells[idx])
        except ValueError:
            continue
        if d:
            rows.append((d, v))
    return sorted(rows)


def last_and_week_ago(rows):
    """日次データの最新値と、その7日以上前の直近値(1週前)を返す。"""
    if not rows:
        return None
    d, v = rows[-1]
    ref = (dt.date.fromisoformat(d) - dt.timedelta(days=7)).isoformat()
    prev = [x for x in rows if x[0] <= ref]
    return {"v": round(v, 4), "d": d, "p": round(prev[-1][1], 4) if prev else None}


def last_and_prev_month(rows, ndp=1):
    """月次データの最新値と前月値。日付は YYYY-MM で返す。"""
    if not rows:
        return None
    d, v = rows[-1]
    p = rows[-2][1] if len(rows) > 1 else None
    return {"v": round(v, ndp), "d": d[:7], "p": round(p, ndp) if p is not None else None}


def yoy(rows):
    """月次の指数(CPIなど)を前年同月比(%)の系列にする。"""
    by = {d[:7]: v for d, v in rows}
    out = []
    for d, v in rows:
        y, m = int(d[:4]), d[5:7]
        base = by.get(f"{y - 1}-{m}")
        if base:
            out.append((d, (v / base - 1) * 100))
    return out


# ---------- 取得 ----------

def fred(sid, days=800):
    cosd = (TODAY - dt.timedelta(days=days)).isoformat()
    return parse_fred(get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={cosd}").decode("utf-8"))


def stooq(sym):
    return parse_stooq(get(f"https://stooq.com/q/d/l/?s={sym}&i=d").decode("utf-8", "replace"))


def mof_jgb10():
    base = "https://www.mof.go.jp/jgbs/reference/interest_rate/"
    rows = []
    try:
        rows = parse_mof(get(base + "jgbcm.csv").decode("shift_jis", "replace"))
    except Exception:
        pass
    # 月初は当月ファイルに1週前の値がないため、全期間ファイルで補う
    if len(rows) < 6:
        rows = sorted(dict(parse_mof(get(base + "data/jgbcm_all.csv", 60).decode("shift_jis", "replace")) + rows).items())
    return rows


def main(path):
    try:
        with open(path, encoding="utf-8") as f:
            old = json.load(f).get("items", {})
    except Exception:
        old = {}

    jobs = {
        # 日経平均・TOPIXはStooqを優先し、日経平均はFREDで補う
        "n225": [lambda: last_and_week_ago(stooq("^nkx")), lambda: last_and_week_ago(fred("NIKKEI225"))],
        "topix": [lambda: last_and_week_ago(stooq("^tpx"))],
        "spx": [lambda: last_and_week_ago(fred("SP500"))],
        "ndx": [lambda: last_and_week_ago(fred("NASDAQCOM"))],
        "vix": [lambda: last_and_week_ago(fred("VIXCLS"))],
        "us10y": [lambda: last_and_week_ago(fred("DGS10"))],
        "fed": [lambda: last_and_week_ago(fred("DFEDTARU"))],
        "wti": [lambda: last_and_week_ago(fred("DCOILWTICO"))],
        "usdjpy": [lambda: last_and_week_ago(fred("DEXJPUS"))],
        "jp10y": [lambda: last_and_week_ago(mof_jgb10())],
        "uscpi": [lambda: last_and_prev_month(yoy(fred("CPIAUCNS", 900)))],
        "unemp": [lambda: last_and_prev_month(fred("UNRATE", 400))],
    }

    items, errors = {}, []
    for key, tries in jobs.items():
        val = None
        for fn in tries:
            try:
                val = fn()
                if val:
                    break
            except Exception as e:  # 1つの失敗で全体を止めない
                errors.append(f"{key}: {type(e).__name__}: {e}")
        if val:
            items[key] = {k: v for k, v in val.items() if v is not None}
        elif key in old:
            items[key] = old[key]  # 取れなかったら前回値を残す

    out = {
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "items": items,
        "errors": errors,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"{len(items)} items, {len(errors)} errors")
    for e in errors:
        print("  ", e)
    return 0 if items else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "market.json"))
