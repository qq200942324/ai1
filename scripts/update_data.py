#!/usr/bin/env python3
"""
A股 20日滚动数据管线
=====================
每次运行：补齐近20日的行情K线 + 新闻消息面，删除20日前的旧数据。

行情：东方财富 K线 API（8大指数日线）
新闻：新浪财经滚动新闻 API（5大分类）
输出：JSON 数据文件 + Markdown 可读摘要

用法：python scripts/update_data.py
"""

import json
import os
import sys
import time
import requests
from datetime import datetime, timedelta, date
from collections import defaultdict

# ============================================================
# 配置
# ============================================================

VAULT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(VAULT_ROOT, "2-wiki", "data")
MARKET_DIR = os.path.join(DATA_DIR, "market")
NEWS_DIR = os.path.join(DATA_DIR, "news")
META_FILE = os.path.join(DATA_DIR, "meta.json")

WINDOW_DAYS = 20
TODAY = date.today()
CUTOFF_DATE = TODAY - timedelta(days=WINDOW_DAYS)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.eastmoney.com/",
}

# 全局 session——绕过系统代理（避免 ProxyError）
SESSION = requests.Session()
SESSION.trust_env = False

# 8大核心指数 (腾讯接口代码)
INDICES = {
    "上证指数": "sh000001",
    "深证成指": "sz399001",
    "创业板指": "sz399006",
    "科创50":   "sh000688",
    "上证50":   "sh000016",
    "沪深300":  "sh000300",
    "中证500":  "sh000905",
    "中证1000": "sh000852",
}

# 新浪新闻分类 (lid)
NEWS_LIDS = {
    "2516": "A股沪深",
    "2512": "宏观政策",
    "2511": "公司要闻",
    "2515": "国际财经",
    "2509": "行业板块",
}

# ============================================================
# 工具函数
# ============================================================

def ensure_dirs():
    os.makedirs(MARKET_DIR, exist_ok=True)
    os.makedirs(NEWS_DIR, exist_ok=True)


def load_json(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def date_range_str():
    return f"{CUTOFF_DATE.strftime('%Y-%m-%d')} ~ {TODAY.strftime('%Y-%m-%d')}"


# ============================================================
# 行情数据：腾讯 K线 API（比东财更稳定，无代理问题）
# ============================================================

def fetch_index_kline(code, name, days=30):
    """获取单只指数的日K线数据 (腾讯接口) + qt实时数据补充"""
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {
        "param": f"{code},day,,,{days},qfq",
    }
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = SESSION.get(url, params=params, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            print(f"  [WARN] {name} ({code}) API返回code={data.get('code')}")
            return []
        inner = data.get("data", {}).get(code, {})
        klines = inner.get("day", []) or inner.get("qfqday", [])
        qt = inner.get("qt", {}).get(code, [])

        # 从qt提取今日成交额: 索引37是成交额(万元), 索引6是成交量(手)
        today_amount = 0
        if len(qt) > 37 and qt[37]:
            try:
                today_amount = float(qt[37]) * 10000  # 万元→元
            except ValueError:
                pass

        result = []
        for i, line in enumerate(klines):
            if len(line) >= 6:
                bar = {
                    "date": line[0],
                    "open": float(line[1]),
                    "close": float(line[2]),
                    "high": float(line[3]),
                    "low": float(line[4]),
                    "volume": int(float(line[5])),
                    "amount": 0,
                    "amplitude": 0,
                    "change_pct": 0,
                }
                # 计算振幅
                if bar["open"] != 0:
                    bar["amplitude"] = round(
                        (bar["high"] - bar["low"]) / bar["open"] * 100, 2
                    )
                # 计算涨跌幅 (相对前日收盘)
                if i > 0 and result[i-1]["close"] != 0:
                    bar["change_pct"] = round(
                        (bar["close"] - result[i-1]["close"]) / result[i-1]["close"] * 100, 2
                    )
                result.append(bar)

        # 补充今日成交额
        if result and today_amount > 0:
            result[-1]["amount"] = today_amount

        return result
    except Exception as e:
        print(f"  [WARN] {name} ({code}) 获取失败: {e}")
        return []


def fetch_all_indices():
    """获取所有指数的K线数据"""
    print("\n[MARKET] 行情数据：获取 8 大指数 K 线...")
    all_data = {}
    for i, (name, secid) in enumerate(INDICES.items()):
        if i > 0:
            time.sleep(1.2)  # 避免限流
        bars = fetch_index_kline(secid, name)
        # 只保留窗口期内的数据
        bars = [b for b in bars if b["date"] >= CUTOFF_DATE.strftime("%Y-%m-%d")]
        all_data[name] = bars
        print(f"  [OK] {name}: {len(bars)} 条日线")
    return all_data


def save_market_data(all_data):
    """保存行情数据并生成 Markdown 摘要"""
    meta = {
        "updated": datetime.now().isoformat(),
        "window_days": WINDOW_DAYS,
        "date_range": date_range_str(),
        "indices": list(INDICES.keys()),
    }

    # 保存聚合 JSON
    master = {"meta": meta, "data": all_data}
    save_json(os.path.join(MARKET_DIR, "indices_master.json"), master)

    # 保存按日期组织的 JSON
    by_date = defaultdict(dict)
    for name, bars in all_data.items():
        for bar in bars:
            by_date[bar["date"]][name] = bar
    save_json(os.path.join(MARKET_DIR, "indices_by_date.json"), dict(by_date))

    # 生成 Markdown 摘要
    lines = [
        "---",
        "tags: [数据, 行情]",
        f"created: {datetime.now().strftime('%Y-%m-%d')}",
        f"updated: {datetime.now().strftime('%Y-%m-%d')}",
        "---",
        "",
        "# A股行情数据摘要",
        f"> 更新时间：{datetime.now().strftime('%Y-%m-%d %H:%M')} | 覆盖：{date_range_str()} | 指数：{len(INDICES)} 个",
        "",
        "## 今日指数概览",
        "",
        "| 指数 | 收盘 | 涨跌幅 | 成交额(亿) | 振幅 |",
        "|------|------|--------|-----------|------|",
    ]

    for name in INDICES:
        bars = all_data.get(name, [])
        if bars:
            today_bar = bars[-1]
            amount_yi = today_bar["amount"] / 1e8
            lines.append(
                f"| {name} | {today_bar['close']:.2f} | "
                f"{today_bar['change_pct']:+.2f}% | {amount_yi:.0f} | "
                f"{today_bar['amplitude']:.2f}% |"
            )

    lines += [
        "",
        "## 近5日涨跌幅矩阵",
        "",
    ]

    # 列头
    recent_dates = sorted(set(
        b["date"] for bars in all_data.values() for b in bars
    ))[-5:]
    header = "| 指数 | " + " | ".join(d[-5:] for d in recent_dates) + " |"
    sep = "|------|" + "|".join(["------"] * len(recent_dates)) + "|"
    lines.append(header)
    lines.append(sep)

    for name in INDICES:
        bars = all_data.get(name, [])
        bar_map = {b["date"]: b for b in bars}
        row = f"| {name} |"
        for d in recent_dates:
            b = bar_map.get(d)
            if b:
                row += f" {b['change_pct']:+.2f}% |"
            else:
                row += " — |"
        lines.append(row)

    lines += [
        "",
        "## 数据文件",
        f"- [[2-wiki/data/market/indices_master.json]] — 聚合数据",
        f"- [[2-wiki/data/market/indices_by_date.json]] — 按日期查询",
    ]

    md_path = os.path.join(MARKET_DIR, "market_summary.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"  [FILE] 行情摘要已保存: market_summary.md")
    return master


# ============================================================
# 新闻数据：新浪财经滚动新闻 API
# ============================================================

def fetch_sina_news_page(lid, page=1, num=50):
    """获取新浪新闻单页"""
    url = "https://feed.mix.sina.com.cn/api/roll/get"
    params = {
        "pageid": 153,
        "lid": lid,
        "k": "",
        "num": num,
        "page": page,
    }
    try:
        r = SESSION.get(url, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        items = data.get("result", {}).get("data", [])
        news_list = []
        for item in items:
            ctime = item.get("ctime", "")
            if ctime:
                dt = datetime.fromtimestamp(int(ctime))
                date_str = dt.strftime("%Y-%m-%d")
                time_str = dt.strftime("%H:%M")
            else:
                date_str = time_str = ""

            news_list.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "date": date_str,
                "time": time_str,
                "keywords": item.get("keywords", ""),
                "lid": lid,
                "category": NEWS_LIDS.get(lid, ""),
            })
        return news_list
    except Exception as e:
        print(f"  [WARN] 新闻 lid={lid} page={page} 失败: {e}")
        return []


def fetch_news_for_date_range():
    """
    智能获取20日窗口内的新闻。
    策略：从第1页开始向后翻，直到遇到早于窗口日期的新闻为止。
    首次运行会翻较深，后续每日更新只需前几页。
    """
    print("\n[NEWS] 新闻数据：从新浪财经获取...")

    all_news = []
    seen_urls = set()

    for lid, cat_name in NEWS_LIDS.items():
        print(f"    [CAT] {cat_name} (lid={lid})...")
        cat_news = []
        consecutive_empty = 0
        page = 1

        while True:
            items = fetch_sina_news_page(lid, page=page, num=30)
            if not items:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    break
                page += 1
                time.sleep(0.5)
                continue

            consecutive_empty = 0
            in_window = 0
            too_old = 0

            for item in items:
                if item["date"] and item["date"] >= CUTOFF_DATE.strftime("%Y-%m-%d"):
                    if item["url"] not in seen_urls:
                        seen_urls.add(item["url"])
                        cat_news.append(item)
                        in_window += 1
                elif item["date"] and item["date"] < CUTOFF_DATE.strftime("%Y-%m-%d"):
                    too_old += 1

            if too_old > 20:  # 本页大部分超出窗口，停止翻页
                break

            page += 1
            time.sleep(0.6)

            # 安全上限：每个分类最多翻 200 页
            if page > 200:
                break

            if page % 20 == 0:
                print(f"    已翻 {page} 页，收集 {len(cat_news)} 条...")

        all_news.extend(cat_news)
        print(f"    [OK] 收集 {len(cat_news)} 条新闻")

    # 去重 & 按日期排序 (最新在前)
    all_news.sort(key=lambda x: (x["date"], x["time"]), reverse=True)
    print(f"  [STATS] 总计: {len(all_news)} 条新闻（{date_range_str()}）")
    return all_news


def save_news_data(all_news):
    """保存新闻数据并生成 Markdown 摘要"""
    # 按日期分组
    by_date = defaultdict(list)
    for item in all_news:
        by_date[item["date"]].append(item)

    dates_sorted = sorted(by_date.keys(), reverse=True)

    meta = {
        "updated": datetime.now().isoformat(),
        "window_days": WINDOW_DAYS,
        "date_range": date_range_str(),
        "total_articles": len(all_news),
        "categories": list(NEWS_LIDS.values()),
    }

    # 保存聚合 JSON
    master = {"meta": meta, "by_date": {d: by_date[d] for d in dates_sorted}}
    save_json(os.path.join(NEWS_DIR, "news_master.json"), master)

    # 生成 Markdown 摘要
    lines = [
        "---",
        "tags: [数据, 新闻]",
        f"created: {datetime.now().strftime('%Y-%m-%d')}",
        f"updated: {datetime.now().strftime('%Y-%m-%d')}",
        "---",
        "",
        "# A股新闻消息面摘要",
        f"> 更新时间：{datetime.now().strftime('%Y-%m-%d %H:%M')} | 覆盖：{date_range_str()} | 总条数：{len(all_news)}",
        "",
        "## 每日新闻量",
        "",
        "| 日期 | 条数 |",
        "|------|------|",
    ]
    for d in dates_sorted[:WINDOW_DAYS]:
        lines.append(f"| {d} | {len(by_date[d])} |")

    # 最近7天的详细新闻
    lines += [
        "",
        "## 近7日要闻",
    ]

    for d in dates_sorted[:7]:
        day_news = by_date[d]
        lines += [
            "",
            f"### {d}（{len(day_news)} 条）",
        ]

        # 按分类分组
        by_cat = defaultdict(list)
        for item in day_news:
            by_cat[item["category"]].append(item)

        for cat, items in by_cat.items():
            lines.append(f"\n**{cat}** ({len(items)} 条)：")
            for item in items[:10]:  # 每个分类最多显示10条
                lines.append(f"- [{item['title']}]({item['url']}) `{item['time']}`")
            if len(items) > 10:
                lines.append(f"  > ... 还有 {len(items) - 10} 条")

    lines += [
        "",
        "## 数据文件",
        f"- [[2-wiki/data/news/news_master.json]] — 完整新闻数据（{len(all_news)} 条）",
    ]

    md_path = os.path.join(NEWS_DIR, "news_summary.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"  [FILE] 新闻摘要已保存: news_summary.md")
    return master


# ============================================================
# 清理旧数据
# ============================================================

def cleanup_old_files():
    """删除超过窗口期的旧数据文件"""
    removed = 0
    for directory in [MARKET_DIR, NEWS_DIR]:
        for fname in os.listdir(directory):
            if fname.endswith(".json") and fname not in [
                "indices_master.json", "indices_by_date.json", "news_master.json"
            ]:
                fpath = os.path.join(directory, fname)
                try:
                    os.remove(fpath)
                    removed += 1
                except Exception:
                    pass
    if removed:
        print(f"\n[CLEAN]  清理了 {removed} 个旧数据文件")


# ============================================================
# 主流程
# ============================================================

def main():
    print("=" * 60)
    print("[Update] A股 20日滚动数据管线")
    print(f"   运行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   数据窗口：{date_range_str()}")
    print("=" * 60)

    ensure_dirs()

    # 1. 行情数据
    indices_data = fetch_all_indices()
    save_market_data(indices_data)

    # 2. 新闻数据
    news_data = fetch_news_for_date_range()
    save_news_data(news_data)

    # 3. 清理旧文件
    cleanup_old_files()

    # 4. 保存元数据（保留已有的 last_analysis）
    existing_meta = load_json(META_FILE) or {}
    meta = {
        "last_update": datetime.now().isoformat(),
        "window_days": WINDOW_DAYS,
        "date_range": date_range_str(),
        "market_indices": len(indices_data),
        "news_total": len(news_data),
    }
    # 保留上次分析/选股时间戳（如果存在，避免被管线覆盖）
    for key in ["last_analysis", "last_stock_pool"]:
        if existing_meta.get(key):
            meta[key] = existing_meta[key]
    save_json(META_FILE, meta)

    print(f"\n[OK] 完成！行情覆盖 {len(indices_data)} 个指数，新闻共 {len(news_data)} 条")
    print(f"   [DIR] 数据目录: {DATA_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
