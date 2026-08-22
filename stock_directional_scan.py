# -*- coding: utf-8 -*-
"""
主线板块干净多头定向扫描（stock-directional-scan 的纯 Python 实现）

流程（与 SKILL.md 一致）：
  1. 8 主线板块拉成分股（东方财富，多镜像主机 + 重试退避）
  2. 去重 -> 主板非 ST 过滤（sh60/sz000/001/002/003，剔 688/300/301/ST）
  3. 腾讯批量行情预筛（按换手率降序取前 N 只进 K 线阶段）
  4. 腾讯 K 线（300 根）本地计算：均线结构判定"干净多头(ma_long 等价)"
  5. 对干净多头标的按《购买指数六维体系》精算（趋势25/位置20/动能20/盈亏比15/题材10/估值10）
  6. 输出 Markdown 汇总表 + 评级 + 90+ 稀缺根因 + 免责声明

与 MCP 版的差异（无 westock 时的等价替代，已在代码内注明）：
  - ma_long 策略 -> 本地自算：MA5>MA10>MA20>MA60 多头发散 + MA60 上行 + 价>MA20
  - 题材分 -> 板块归属数代理（无新闻接口）：>=3 板块 10 / 2 板块 8 / 1 板块 6
  - 估值分 -> 候选池 PE 中位数作"行业中枢"代理
用法：
  python stock_directional_scan.py                 # 全量扫描（默认 K 线阶段取前 80 只）
  python stock_directional_scan.py --limit 40      # 只精算换手率前 40
  python stock_directional_scan.py --min-score 60  # 只输出 >=60 分
  python stock_directional_scan.py --save          # 同时保存 md 文件
"""
import argparse
import json
import gzip
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

# ---------------- 常量 ----------------

BOARDS = {  # 8 主线板块（东财板块码；失效时可用 --relookup 自动重查）
    "半导体": "BK1036",
    "光学光电子": "BK1038",
    "通信设备": "BK0448",
    "人形机器人": "BK1184",
    "元件": "BK0459",
    "存储芯片": "BK1137",
    "汽车零部件": "BK0481",
    "通用设备": "BK0545",
}

EM_HOSTS = [  # 东财镜像轮换
    "https://push2.eastmoney.com",
    "https://82.push2.eastmoney.com",
    "https://33.push2.eastmoney.com",
    "https://17.push2.eastmoney.com",
]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"

DISCLAIMER = ("\n> **免责声明**：以上内容基于公开数据和量化分析，仅供参考，不构成投资建议。"
              "市场有风险，投资需谨慎。任何投资决策应结合个人风险承受能力、资金状况和投资目标独立判断，"
              "必要时咨询持牌专业机构。过往表现不预示未来收益。")


# ---------------- HTTP 基础 ----------------

def _open(url, referer, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return data


def fetch_em(path, retries=4, politeness=2.0):
    """东财接口：多镜像 + 指数退避。path 以 / 开头。"""
    last = None
    for i in range(retries):
        host = EM_HOSTS[i % len(EM_HOSTS)]
        try:
            return json.loads(_open(host + path, "https://quote.eastmoney.com/").decode("utf-8"))
        except Exception as e:
            last = e
            time.sleep(politeness * (i + 1))
    raise last


def fetch_qt_quotes(codes):
    """腾讯批量行情（GBK 文本），一次最多 ~60 只。返回 {code: dict}"""
    out = {}
    for i in range(0, len(codes), 50):
        batch = codes[i:i + 50]
        url = "https://qt.gtimg.cn/q=" + ",".join(batch)
        text = _open(url, "https://gu.qq.com/").decode("gbk", "ignore")
        for line in text.strip().split(";"):
            line = line.strip()
            if not line or "=" not in line:
                continue
            m = re.match(r"v_(\w+)=", line)
            if not m:
                continue
            code = m.group(1)
            f = line.split("~")
            if len(f) < 46:
                continue
            try:
                out[code] = {
                    "name": f[1],
                    "price": float(f[3]),
                    "pct": float(f[32]),
                    "turnover": float(f[38]) if f[38] not in ("", "-") else None,
                    "pe": float(f[39]) if f[39] not in ("", "-") else None,
                    "pb": float(f[46]) if f[46] not in ("", "-") else None,
                }
            except (ValueError, IndexError):
                continue
        time.sleep(0.35)
    return out


def fetch_kline(code, limit=300):
    """腾讯前复权日 K。返回 bars（升序）：[{date,open,close,high,low,vol}]"""
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={code},day,,,{limit},qfq")
    d = json.loads(_open(url, "https://gu.qq.com/").decode("utf-8"))
    node = (d.get("data") or {}).get(code) or {}
    rows = node.get("qfqday") or node.get("day") or []
    bars = []
    for r in rows:
        try:
            bars.append({"date": r[0], "open": float(r[1]), "close": float(r[2]),
                         "high": float(r[3]), "low": float(r[4]), "vol": float(r[5])})
        except (ValueError, IndexError):
            continue
    return bars


# ---------------- 指标计算（纯本地） ----------------

def ma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def ma_series(vals, n):
    out = [None] * len(vals)
    acc = 0.0
    for i, v in enumerate(vals):
        acc += v
        if i >= n:
            acc -= vals[i - n]
        if i >= n - 1:
            out[i] = acc / n
    return out


def ema_series(vals, n):
    k = 2.0 / (n + 1)
    out = [None] * len(vals)
    e = vals[0]
    out[0] = e
    for i in range(1, len(vals)):
        e = vals[i] * k + e * (1 - k)
        out[i] = e
    return out


def macd(vals):
    if len(vals) < 35:
        return None
    e12 = ema_series(vals, 12)
    e26 = ema_series(vals, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = ema_series(dif, 9)
    m = dif[-1] - dea[-1]
    m_prev = dif[-2] - dea[-2]
    golden = m > 0
    expanding = abs(m) > abs(m_prev)
    return {"dif": dif[-1], "dea": dea[-1], "macd": m, "golden": golden, "expanding": expanding}


def kdj(bars, n=9):
    if len(bars) < n + 3:
        return None
    k, d = 50.0, 50.0
    for i in range(len(bars) - n - 2, len(bars)):
        win = bars[i - n + 1:i + 1]
        hh = max(b["high"] for b in win)
        ll = min(b["low"] for b in win)
        rsv = 50.0 if hh == ll else (bars[i]["close"] - ll) / (hh - ll) * 100
        k = 2 / 3 * k + 1 / 3 * rsv
        d = 2 / 3 * d + 1 / 3 * k
    j = 3 * k - 2 * d
    return {"k": k, "d": d, "j": j}


def rsi(vals, n=6):
    if len(vals) < n + 1:
        return None
    gains, losses = [], []
    for i in range(len(vals) - n, len(vals)):
        ch = vals[i] - vals[i - 1]
        gains.append(max(ch, 0))
        losses.append(max(-ch, 0))
    ag = sum(gains) / n
    al = sum(losses) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def swing_points(bars, span=2):
    """分形摆动点：返回 (swing_highs, swing_lows) 各为 (idx, price)"""
    highs, lows = [], []
    for i in range(span, len(bars) - span):
        w = bars[i - span:i + span + 1]
        if bars[i]["high"] == max(b["high"] for b in w):
            highs.append((i, bars[i]["high"]))
        if bars[i]["low"] == min(b["low"] for b in w):
            lows.append((i, bars[i]["low"]))
    return highs, lows


def higher_structure(bars, lookback=60):
    """更高高点 + 更高低点确认（近 lookback 根内取最近两个摆动高点/低点比较）"""
    seg = bars[-lookback:]
    sh, sl = swing_points(seg)
    if len(sh) >= 2 and len(sl) >= 2:
        return sh[-1][1] > sh[-2][1] and sl[-1][1] > sl[-2][1]
    return False


def is_clean_bullish(closes, price):
    """ma_long 等价判定：MA5>MA10>MA20>MA60 多头发散 + MA60 上行 + 价在 MA20 上"""
    if len(closes) < 65:
        return False
    s5, s10, s20, s60 = (ma_series(closes, n) for n in (5, 10, 20, 60))
    m5, m10, m20, m60 = s5[-1], s10[-1], s20[-1], s60[-1]
    if None in (m5, m10, m20, m60):
        return False
    aligned = m5 > m10 > m20 > m60
    ma60_rising = s60[-1] > s60[-6]
    return aligned and ma60_rising and price > m20


def is_reversal_day_break(bars, quote):
    """反转日击穿修正判定（scoring.md ②：四条同时满足才扣分）"""
    if len(bars) < 25:
        return False
    last = bars[-1]
    prev = bars[-2]
    chg = (last["close"] - prev["close"]) / prev["close"]
    a = chg <= -0.03 and last["close"] < last["open"]
    if not a:
        return False
    vol_avg20 = sum(b["vol"] for b in bars[-21:-1]) / 20
    vol_ratio = last["vol"] / vol_avg20 if vol_avg20 else 0
    t = quote.get("turnover")
    b_cond = (t is not None and t > 10) or vol_ratio > 2.0
    if not b_cond:
        return False
    c_cond = last["low"] > 0 and last["close"] / last["low"] <= 1.02
    if not c_cond:
        return False
    closes = [b["close"] for b in bars]
    m5 = ma(closes, 5)
    m20 = ma(closes, 20)
    m60 = ma(closes, 60)
    broke_support = (m20 and last["low"] < m20) or (m60 and last["low"] < m60)
    d = last["close"] < m5 and broke_support
    return bool(d)


# ---------------- 六维评分 ----------------

def score_trend(bars, closes, price):
    """① 趋势 25：干净多头（价>上行 MA60 + 更高低点结构）25；反弹/震荡 12-18；空头 0-5"""
    if len(closes) < 65:
        return 0
    s20 = ma(closes, 20)
    s60_series = ma_series(closes, 60)
    m60 = s60_series[-1]
    ma60_up = s60_series[-1] > s60_series[-6]
    if price > m60 and ma60_up and higher_structure(bars):
        return 25
    if price > s20 and not ma60_up:
        return 15
    if price > s20:
        return 18
    if price < m60 and not ma60_up:
        return 5
    return 8


def score_position(price, support, bars, quote):
    """② 位置 20 + 反转日击穿修正"""
    if support is None or support <= 0:
        return 3
    dist = (support - price) / price
    if dist > 0.08:
        base = 3
    elif dist > 0.05:
        base = 8
    elif dist > 0.02:
        base = 14
    elif dist >= -0.005:  # 距支撑 <=2%（含轻微贴上）
        base = 20
    else:  # 已跌破最近支撑
        base = 2
    if is_reversal_day_break(bars, quote):
        chg_deep = min(0.0, dist)
        base = max(0, base - (8 if chg_deep < -0.02 else 5))
    return base


def score_momentum(bars, closes):
    """③ 动能 20：基础 10 + KDJ/MACD/RSI 加减，封顶 20 保底 0"""
    k = kdj(bars)
    m = macd(closes)
    r = rsi(closes, 6)
    s = 10
    if k:
        if k["j"] < 20:
            s += 6
        elif k["j"] <= 50:
            s += 3
        elif k["j"] > 90:
            s -= 6
    if m:
        if m["golden"] and m["expanding"]:
            s += 4
        elif not m["golden"]:
            s -= 4
    if r is not None:
        if 30 <= r <= 60:
            s += 3
        elif r > 80:
            s -= 4
        elif r < 30:
            s += 2
    return max(0, min(20, s))


def score_rr(price, support, target):
    """④ 盈亏比 15：R:R = (目标-买点)/(买点-止损)，买点=支撑回踩位，止损=支撑-1.5%"""
    if not support or not target or support <= 0:
        return 0
    entry = support
    stop = support * 0.985
    if entry <= stop:
        return 0
    rr = (target - entry) / (entry - stop)
    if rr >= 3:
        return 15
    if rr >= 2:
        return 11
    if rr >= 1.5:
        return 7
    return 3


def score_theme(n_boards):
    """⑤ 题材 10（板块归属数代理，无新闻接口）"""
    if n_boards >= 3:
        return 10
    if n_boards == 2:
        return 8
    return 6


def score_valuation(pe, pe_median, gain10):
    """⑥ 估值 10：候选池 PE 中位数作行业中枢代理"""
    if pe is None or pe <= 0 or pe_median is None:
        return 0
    if gain10 > 0.40 and pe > 1.5 * pe_median:
        return 0
    if pe <= 0.75 * pe_median:
        return 10
    if pe <= 1.15 * pe_median:
        return 7
    if pe <= 2.0 * pe_median:
        return 4
    return 0


def rating(total):
    if total >= 80:
        return "强烈建议买入"
    if total >= 65:
        return "建议买入"
    if total >= 50:
        return "轻仓试探/等回踩"
    if total >= 35:
        return "观望"
    return "不建议买入"


# ---------------- 关键价位 ----------------

def nearest_support(closes, price, bars):
    """最近有效支撑：MA5/MA10/MA20 中价格上方且最贴近的一条；都在上方则取摆动低点"""
    cands = []
    for n in (5, 10, 20):
        v = ma(closes, n)
        if v and v < price:
            cands.append(v)
    if cands:
        return max(cands)
    _, sl = swing_points(bars[-40:])
    below = [p for _, p in sl if p < price]
    return max(below) if below else None


def first_target(price, bars):
    """第一目标：20 日高点；已贴近则 60 日高点；再贴近则全程高点"""
    for lookback in (20, 60, len(bars)):
        h = max(b["high"] for b in bars[-lookback:])
        if h > price * 1.01:
            return h
    return price * 1.05


# ---------------- 主流程 ----------------

def get_board_constituents(name, bk):
    stocks, pn = [], 1
    while True:
        path = (f"/api/qt/clist/get?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2"
                f"&fid=f3&fs=b:{bk}&fields=f12,f13,f14")
        d = fetch_em(path)
        diff = (d.get("data") or {}).get("diff") or []
        if not diff:
            break
        for x in diff:
            mkt = {1: "sh", 0: "sz"}.get(x.get("f13"))
            if mkt:
                stocks.append((mkt + str(x["f12"]), x.get("f14", "")))
        if len(diff) < 100:
            break
        pn += 1
        time.sleep(2.5)
    return stocks


def mainboard_nosth_filter(pool):
    """主板非 ST：sh60xxxx / sz000|001|002|003xxx，剔 688/300/301/ST"""
    out = {}
    for code, v in pool.items():
        num = code[2:]
        ok = (code.startswith("sh60") or num[:3] in ("000", "001", "002", "003"))
        if ok and "ST" not in v["name"].upper():
            out[code] = v
    return out


def analyze_one(code, info, quote, bars, pe_median):
    closes = [b["close"] for b in bars]
    price = quote["price"]
    info_out = {"code": code, "name": quote.get("name") or info["name"],
                "boards": info["boards"], "price": price, "pe": quote.get("pe")}
    if len(bars) < 70:
        return None

    support = nearest_support(closes, price, bars)
    target = first_target(price, bars)
    gain10 = (closes[-1] / closes[-11] - 1) if len(closes) > 11 else 0.0

    dims = {
        "趋势": score_trend(bars, closes, price),
        "位置": score_position(price, support, bars, quote),
        "动能": score_momentum(bars, closes),
        "盈亏比": score_rr(price, support, target),
        "题材": score_theme(len(info["boards"])),
        "估值": score_valuation(quote.get("pe"), pe_median, gain10),
    }
    total = sum(dims.values())
    info_out.update({
        "dims": dims, "total": total, "rating": rating(total),
        "support": support, "stop": support * 0.985 if support else None,
        "target": target, "gain10": gain10,
    })
    return info_out


def main():
    ap = argparse.ArgumentParser(description="主线板块干净多头定向扫描（纯 Python）")
    ap.add_argument("--limit", type=int, default=80, help="K 线精算阶段取换手率前 N 只（默认 80）")
    ap.add_argument("--min-score", type=int, default=0, help="只输出总分 >= N 的标的")
    ap.add_argument("--save", action="store_true", help="同时保存 Markdown 文件")
    args = ap.parse_args()

    t0 = time.time()
    print("[1/5] 拉取 8 主线板块成分股（东财）...")
    pool = {}
    for bname, bk in BOARDS.items():
        try:
            stocks = get_board_constituents(bname, bk)
            print(f"    {bname}({bk}): {len(stocks)} 只")
            for code, name in stocks:
                pool.setdefault(code, {"name": name, "boards": []})
                if bname not in pool[code]["boards"]:
                    pool[code]["boards"].append(bname)
        except Exception as e:
            print(f"    {bname}({bk}) 拉取失败：{e}（跳过）")
        time.sleep(2.0)
    if not pool:
        sys.exit("全部板块拉取失败，退出（东财限流？稍后重试）")
    print(f"    候选池去重：{len(pool)} 只")

    print("[2/5] 主板非 ST 过滤...")
    mb = mainboard_nosth_filter(pool)
    print(f"    剩 {len(mb)} 只（剔除科创/创业/ST）")

    print("[3/5] 腾讯批量行情预筛...")
    quotes = fetch_qt_quotes(list(mb.keys()))
    valid = {c: q for c, q in quotes.items() if q["price"] > 0}
    ranked = sorted(valid.items(), key=lambda kv: kv[1].get("turnover") or 0, reverse=True)
    picked = ranked[: args.limit]
    print(f"    有效行情 {len(valid)} 只，按换手率取前 {len(picked)} 只进精算")

    print("[4/5] 拉 K 线 + 干净多头判定 + 六维精算...")
    bullish, results = [], []
    for i, (code, quote) in enumerate(picked, 1):
        try:
            bars = fetch_kline(code, 300)
        except Exception:
            time.sleep(0.5)
            continue
        closes = [b["close"] for b in bars]
        if len(bars) < 70 or is_clean_bullish(closes, quote["price"]):
            if len(bars) >= 70:
                bullish.append((code, mb[code], quote, bars))
        time.sleep(0.35)
        if i % 20 == 0:
            print(f"    ... {i}/{len(picked)}")
    pe_list = [q["pe"] for _, _, q, _ in bullish if q.get("pe") and q["pe"] > 0]
    pe_median = statistics.median(pe_list) if pe_list else None
    print(f"    干净多头：{len(bullish)} 只；候选池 PE 中位数 = {pe_median and round(pe_median, 1)}")

    for code, info, quote, bars in bullish:
        r = analyze_one(code, info, quote, bars, pe_median)
        if r and r["total"] >= args.min_score:
            results.append(r)
    results.sort(key=lambda x: -x["total"])

    print("[5/5] 生成汇总...")
    data_date = bullish[0][3][-1]["date"] if bullish else datetime.now().strftime("%Y-%m-%d")
    lines = []
    lines.append(f"# 主线板块干净多头定向扫描 · {data_date}")
    lines.append("")
    lines.append(f"**扫描范围**：{'/'.join(BOARDS.keys())}（{len(pool)} 只去重 -> 主板非ST {len(mb)} 只 -> "
                 f"换手前 {len(picked)} 精算 -> 干净多头 {len(bullish)} 只）")
    lines.append(f"**干净多头判定**（本地自算，等价 ma_long）：MA5>MA10>MA20>MA60 多头发散 + MA60 上行 + 价>MA20")
    lines.append("")
    header = ("| # | 名称 | 代码 | 指数 | 评级 | 趋势/25 | 位置/20 | 动能/20 | 盈亏比/15 | 题材/10 | 估值/10 |"
              " 关键支撑 | 止损 | 第一目标 |")
    sep = "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
    lines.append(header)
    lines.append(sep)
    for i, r in enumerate(results, 1):
        d = r["dims"]
        fmt = lambda v: f"{v:.2f}" if isinstance(v, float) and v > 20 else f"{v}"
        lines.append(
            f"| {i} | {r['name']} | {r['code']} | **{r['total']}** | {r['rating']} |"
            f" {d['趋势']} | {d['位置']} | {d['动能']} | {d['盈亏比']} | {d['题材']} | {d['估值']} |"
            f" {r['support'] and round(r['support'], 2)} | {r['stop'] and round(r['stop'], 2)} |"
            f" {r['target'] and round(r['target'], 2)} |")
    if not results:
        lines.append("| — | 无达标标的（--min-score 过滤后为空） | | | | | | | | | | | | |")
    lines.append("")

    top = [r for r in results if r["total"] >= 80]
    if top:
        lines.append("## ≥80 分点评")
        for r in top:
            d = r["dims"]
            why = []
            if d["趋势"] >= 23:
                why.append("多头结构干净（MA 发散+MA60 上行+高低点抬升）")
            if d["位置"] >= 14:
                why.append(f"贴近支撑（{r['support'] and round(r['support'], 2)}）")
            if d["估值"] >= 7:
                why.append(f"估值合理（PE {r['pe'] and round(r['pe'], 1)} vs 池中位 {pe_median and round(pe_median, 1)}）")
            if d["题材"] >= 8:
                why.append("多板块主线交叉题材")
            lines.append(f"- **{r['name']}（{r['total']}）**：" + "；".join(why))
        lines.append("")

    if not any(r["total"] >= 90 for r in results):
        lines.append("## 90+ 稀缺根因")
        lines.append("购买指数中「估值10+位置20+盈亏比15=45 分」直接绑定**买得便宜**；而主线干净多头股普遍 "
                     "PE 畸高（估值失分）、多头发散形态价格远离均线（位置失分）、高位追入盈亏比差（盈亏比失分）——"
                     "三者同具极少见。真 90+ 多存在于「上升通道中回踩 MA10/MA20 缩量企稳」的短暂窗口（通常 1-3 个交易日）。")
        lines.append("")

    lines.append(f"数据截止：{data_date} 收盘/最新 | 来源：东方财富（板块成分）+ 腾讯（行情/前复权K线，本地计算指标）")
    lines.append(f"耗时 {time.time() - t0:.0f}s | 生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(DISCLAIMER)

    report = "\n".join(lines)
    print("\n" + report + "\n")

    if args.save:
        fn = f"定向扫描_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
        with open(fn, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"已保存：{fn}")


if __name__ == "__main__":
    main()
