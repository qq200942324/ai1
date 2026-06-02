#!/usr/bin/env python3
"""
A股选股池筛选脚本
==================
基于 Wiki 交易框架，筛选流通市值 > 80亿的个股，分为观察股和交易股。

筛选维度（来自 Wiki 概念）：
  1. 流通市值 > 80亿（硬性门槛）
  2. 龙头属性 [[龙头认知]]：行业地位、累积涨幅
  3. 赚钱效应 [[赚钱效应]]：可持续盈利模式
  4. 核心矛盾 [[主要矛盾与核心论]]：AI/大宗/防御方向对齐
  5. 弱转强 [[弱转强]]：近期走势转折信号
  6. 情绪周期 [[情绪周期]]：退潮期偏好防御+结构性主线

用法：python scripts/screen_stocks.py
输出：2-wiki/data/stock_pool.json
"""

import json
import os
import sys
import time
import requests
from datetime import datetime, date, timedelta
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# 配置
# ============================================================

# 修复 Windows GBK 终端编码问题
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

VAULT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(VAULT_ROOT, "2-wiki", "data")
OUTPUT_FILE = os.path.join(DATA_DIR, "stock_pool.json")
OUTPUT_PERIOD_FILE = os.path.join(DATA_DIR, "market", "period_returns.json")  # 多周期涨幅排名

MIN_CAP_YI = 80  # 最低流通市值（亿）— 交易池门槛
DATA_COLLECT_MIN_CAP_YI = 50  # 数据收集门槛（亿）— 低于此的不入库
DATA_COLLECT_MIN_CAP = DATA_COLLECT_MIN_CAP_YI * 1e8  # 转换为元
MIN_CAP = MIN_CAP_YI * 1e8  # 转换为元

SESSION = requests.Session()
SESSION.trust_env = False

# ============================================================
# 东财行业分类 → Wiki 主题映射
# ============================================================

# 当前市场核心方向（来自每日分析 + Wiki 框架）
CORE_THEMES = {
    "AI硬件": ["半导体", "通信设备", "光学光电子", "电子元件", "消费电子", "计算机设备"],
    "AI应用": ["软件开发", "互联网服务", "文化传媒", "游戏"],
    "大宗商品": ["有色金属", "煤炭", "石油", "贵金属", "钢铁"],
    "新能源": ["电池", "光伏设备", "风电设备", "电网设备", "电源设备"],
    "防御消费": ["食品饮料", "医药制造", "中药", "医疗器械", "农牧饲渔"],
    "金融": ["银行", "证券", "保险", "多元金融"],
    "高端制造": ["汽车整车", "汽车零部件", "专用设备", "通用设备", "航天航空", "船舶制造"],
}

# 防御/权重板块（退潮期偏好）
DEFENSIVE_SECTORS = ["银行", "保险", "食品饮料", "医药制造", "中药", "公用事业", "电力行业", "煤炭"]

# AI/科技进攻板块
TECH_SECTORS = ["半导体", "通信设备", "软件开发", "计算机设备", "光学光电子", "电子元件", "互联网服务"]

# 大宗商品/周期板块
COMMODITY_SECTORS = ["有色金属", "煤炭", "石油", "贵金属", "钢铁", "化工"]

def classify_theme(sector):
    """将东财行业分类映射到 Wiki 主题"""
    for theme, industries in CORE_THEMES.items():
        if sector in industries:
            return theme
    return "其他"


# ============================================================
# Step 1: 东财 API 拉取全 A 股列表（按流通市值排序）
# ============================================================

def _normalize_vol_ratio(raw_val):
    """将东财API的原始量比值归一化。

    东财 push2delay API 的 f10/f17 量比字段返回原始值（×100），
    如实际量比 1.64 → 返回 164。正常量比范围 0.1~20，超过则需÷100。
    """
    if not raw_val or raw_val == 0:
        return 0
    if raw_val > 50:  # 合理量比不会超过20，>50说明未归一化
        return round(raw_val / 100, 2)
    return raw_val


def fetch_stock_universe():
    """拉取流通市值 > MIN_CAP 的所有 A 股"""
    print(f"\n[STOCK] Step 1: 拉取流通市值 > {DATA_COLLECT_MIN_CAP_YI}亿 的股票（交易池≥{MIN_CAP_YI}亿）...")

    all_stocks = []
    page = 1

    while True:
        url = "https://push2delay.eastmoney.com/api/qt/clist/get"
        params = {
            "fid": "f20",   # 按流通市值排序
            "po": 1,        # 1 = 从大到小
            "pz": 100,
            "pn": page,
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f2,f3,f8,f9,f10,f12,f14,f15,f17,f18,f20,f21,f100,f102,f104,f105,f106"
        }
        try:
            r = SESSION.get(url, params=params, timeout=20)
            data = r.json()
            items = data.get("data", {}).get("diff", {})

            if not items:
                break

            below_threshold = 0
            for _, item in items.items():
                cap = item.get("f20", 0)  # 流通市值（元）
                name = item.get("f14", "")

                # 跳过退市/ST/科创板(688)
                code = item.get("f12", "")
                if "PT" in name or "*ST" in name or cap == 0 or code.startswith("688"):
                    continue

                if cap >= DATA_COLLECT_MIN_CAP:
                    price = item.get("f2", 0) / 100 if item.get("f2") else 0  # f2=分→元
                    prev_close = item.get("f18", 0) / 100 if item.get("f18") else 0  # f18=分→元
                    change_amt = item.get("f3", 0)  # f3=涨跌额(可能是分)

                    # 计算涨跌幅
                    # 东财 clist 的 f3 可能是涨跌额也可能是百分比，需智能判断
                    change_amt = item.get("f3", 0)
                    if prev_close > 0:
                        # 如果 f3 / prev_close 在合理范围(±20%)→ f3 是涨跌额
                        ratio = change_amt / prev_close
                        if abs(ratio) <= 0.20:
                            change_pct = round(ratio * 100, 2)
                        elif abs(change_amt) <= 20:
                            # f3 本身就是百分比
                            change_pct = round(change_amt, 2)
                        else:
                            change_pct = 0
                    else:
                        change_pct = 0

                    stock = {
                        "code": item.get("f12", ""),
                        "name": name,
                        "price": price,
                        "change_pct": change_pct,
                        "turnover": item.get("f8", 0) or item.get("f15", 0),  # 换手率
                        "pe": item.get("f9", 0),  # 市盈率
                        "vol_ratio": _normalize_vol_ratio(item.get("f10", 0) or item.get("f17", 0)),  # 量比（原始需÷100）
                        "circ_mcap": cap,  # 流通市值
                        "total_mcap": item.get("f21", 0),  # 总市值
                        "sector": item.get("f100", ""),  # 行业
                        "prev_close": prev_close,
                    }
                    # 计算流通市值（亿）
                    stock["circ_mcap_yi"] = round(cap / 1e8, 1)
                    # 主题分类
                    stock["theme"] = classify_theme(stock["sector"])

                    all_stocks.append(stock)
                else:
                    below_threshold += 1

            if page % 5 == 0:
                print(f"  已翻 {page} 页，收集 {len(all_stocks)} 只（流通值 >= {DATA_COLLECT_MIN_CAP_YI}亿）...")

            # 如果本页大部分低于阈值，停止
            if below_threshold > 80:
                break

            page += 1
            time.sleep(0.5)

            if page > 50:  # 安全上限
                break

        except Exception as e:
            print(f"  [WARN] 第 {page} 页获取失败: {e}")
            break

    print(f"  [OK] 共收集 {len(all_stocks)} 只股票（流通值 >= {DATA_COLLECT_MIN_CAP_YI}亿）")
    return all_stocks


# ============================================================
# Step 2: 腾讯 API 批量获取近期 K 线（识别弱转强信号）
# ============================================================

def fetch_recent_klines(stocks, days=10):
    """批量获取近期 K 线数据，识别技术形态"""
    print(f"\n[STOCK] Step 2: 批量获取近 {days} 日 K 线...")

    # 腾讯 API 支持批量（以逗号分隔）
    codes = [f"{'sh' if s['code'].startswith(('6','9')) else 'sz'}{s['code']}" for s in stocks]

    batch_size = 20  # 每批 20 只
    results = {}

    for i in range(0, len(codes), batch_size):
        batch_codes = codes[i:i+batch_size]
        code_str = ",".join(batch_codes)

        try:
            url = "https://web.sqt.gtimg.cn/q=" + code_str
            headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
            r = SESSION.get(url, headers=headers, timeout=15)
            r.encoding = "gbk"

            for line in r.text.split(";"):
                if "~" not in line or "=" not in line:
                    continue
                # 解析 qt 数据
                parts = line.split("~")
                if len(parts) < 40:
                    continue

                try:
                    code_full = parts[2]  # 6-digit code, no prefix
                    code = code_full  # matches East Money 6-digit format

                    info = {
                        "name": parts[1],
                        "price": float(parts[3]) if parts[3] else 0,
                        "change_pct": float(parts[32]) if parts[32] else 0,
                        "high": float(parts[33]) if parts[33] else 0,
                        "low": float(parts[34]) if parts[34] else 0,
                        "volume": int(parts[6]) if parts[6] else 0,
                        "amount_yi": float(parts[37]) * 10000 / 1e8 if parts[37] else 0,  # 万元→亿
                        "amplitude": float(parts[43]) if parts[43] else 0,  # 振幅
                        "pe": float(parts[39]) if parts[39] else 0,
                        "turnover": float(parts[38]) if parts[38] else 0,
                        # 多周期区间涨跌幅（前复权，百分比）
                        # 注意：腾讯 qt API 字段位置因股票市场（沪/深/科创）略有差异
                        # [62] 在所有市场均可靠 = 5日涨跌幅
                        # [63] 近似 10~20 日区间涨幅，沪市可靠，深市可能有偏差
                        # [64] 沪市 = 20日涨跌幅，深市可能为 0（字段位置不同）
                        "ret_5d": float(parts[62]) if parts[62] else 0,
                        "ret_10d": float(parts[63]) if parts[63] else 0,
                        "ret_20d": float(parts[64]) if parts[64] else 0,
                    }
                    results[code] = info
                except (ValueError, IndexError):
                    continue

            if (i // batch_size) % 5 == 0:
                print(f"  已处理 {min(i+batch_size, len(codes))}/{len(codes)} 只...")
            time.sleep(0.8)

        except Exception as e:
            print(f"  [WARN] 批量 {i}-{i+batch_size} 获取失败: {e}")

    print(f"  [OK] 获取到 {len(results)} 只股票的实时数据")
    return results


# ============================================================
# Step 3: 计算技术指标 & 应用 Wiki 筛选
# ============================================================

def compute_wiki_scores(stocks, qt_data, market_phase=None, identities=None):
    """基于 Wiki 交易框架计算每只股票的得分"""
    print(f"\n[STOCK] Step 3: 应用 Wiki 框架计算得分...")
    is_bear = market_phase and market_phase.get("phase") in ("退潮", "偏弱震荡")
    identities = identities or {}

    scored = []

    for s in stocks:
        code = s["code"]
        qt = qt_data.get(code, {})

        score = 0
        reasons = []

        # ---- 维度 1：龙头属性 [[龙头认知]] ----
        # 大市值 → 可能是行业龙头/中军
        if s["circ_mcap_yi"] >= 500:
            score += 3
            reasons.append(f"超大流通盘{s['circ_mcap_yi']}亿(行业龙头/中军)")
        elif s["circ_mcap_yi"] >= 200:
            score += 2
            reasons.append(f"大流通盘{s['circ_mcap_yi']}亿")
        elif s["circ_mcap_yi"] >= 100:
            score += 1

        # ---- 维度 2：主题对齐 [[主要矛盾与核心论]] ----
        theme = s["theme"]
        sector = s["sector"]

        if theme == "AI硬件":
            score += 3
            reasons.append(f"核心主线:AI硬件({sector})")
        elif theme == "AI应用":
            score += 2
            reasons.append(f"核心主线:AI应用({sector})")
        elif theme == "大宗商品":
            score += 2
            reasons.append(f"核心主线:大宗商品({sector})")
        elif theme == "防御消费":
            score += 1  # 退潮期有防御价值但进攻性弱
            reasons.append(f"防御板块({sector})-退潮期避险")
        elif theme == "金融":
            score += 1
            reasons.append(f"权重金融({sector})")
        elif theme == "高端制造":
            score += 1
            reasons.append(f"高端制造({sector})")

        # ---- 维度 3：赚钱效应 [[赚钱效应]] ----
        # 优先使用腾讯 qt 数据（更可靠），回退到东财数据
        change = qt.get("change_pct", 0) or s.get("change_pct", 0) or 0
        # 清理异常值
        if abs(change) > 20:
            change = s.get("change_pct", 0) or 0  # 回退东财
        if abs(change) > 20:
            change = 0  # 放弃异常值

        if change > 5:
            score += 3
            reasons.append(f"今日强势+{change:.1f}%")
        elif change > 2:
            score += 2
            reasons.append(f"今日走强+{change:.1f}%")
        elif change > 0:
            score += 1
        elif change < -5:
            score -= 2
            reasons.append(f"今日大跌{change:.1f}%")
        elif change < -2:
            score -= 1

        # ---- 维度 4：弱转强信号 [[弱转强]] ----
        # 优先使用腾讯 qt 数据
        turnover = qt.get("turnover", 0) or s.get("turnover", 0) or 0
        vol_ratio = s.get("vol_ratio", 0) or 0  # 量比只有东财有
        amplitude = qt.get("amplitude", 0) or 0

        # 清理异常 turnover（>100% 不可能）
        if turnover > 100:
            turnover = 0

        # 放量+大振幅 → 可能弱转强（分歧转一致过程）
        if vol_ratio > 2 and amplitude > 5 and change > 0:
            score += 3
            reasons.append(f"放量弱转强(量比{vol_ratio:.1f},振幅{amplitude:.1f}%)")
        elif vol_ratio > 1.5 and change > 0:
            score += 2
            reasons.append(f"放量走强(量比{vol_ratio:.1f})")
        elif vol_ratio > 1.2 and change > 0:
            score += 1

        # ---- 维度 5：分歧与一致 [[分歧与一致]] ----
        # 高换手+温和上涨 → 分歧中走强（分歧转一致过程）
        if turnover and 3 < turnover < 25 and 0 < change < 5:
            score += 2
            reasons.append(f"分歧转一致(换手{turnover:.1f}%)")
        elif turnover and turnover > 15 and change > 0:
            score += 1  # 高换手但上涨，分歧大但多方胜

        # ---- 维度 6：情绪周期适配 [[情绪周期]] ----
        # 退潮期：偏好防御+大盘+低波动
        # 结构性：偏好 AI 硬件+大宗（有基本面支撑）
        if theme == "防御消费" and change < 0 and abs(change) < 2:
            score += 1
            reasons.append("退潮期抗跌(防御属性)")

        # ---- 维度 7：共振/跷跷板 [[共振与跷跷板]] ----
        if theme in ["AI硬件", "AI应用"] and change > 1:
            score += 1  # 科技主线有板块共振

        # ---- 维度 8：流动性溢价 ----
        # 日成交额 > 20亿 → 流动性好，大资金可进出
        amount_yi = qt.get("amount_yi", 0) or 0
        if amount_yi > 50:
            score += 2
            reasons.append(f"超高流动性(日成交{amount_yi:.0f}亿)")
        elif amount_yi > 20:
            score += 1
            reasons.append(f"流动性好(日成交{amount_yi:.0f}亿)")

        # ---- 维度 9：退潮期超跌反弹识别 ----
        # 退潮/偏弱震荡时：20日跌 > 15% + 今日涨 > 2%（近似 3 日反弹）→ +2分
        if is_bear:
            ret_20d_qt = qt.get("ret_20d", 0) or 0
            if ret_20d_qt < -15 and change > 2:
                score += 2
                reasons.append(f"退潮超跌反弹(20日{ret_20d_qt:+.0f}% 今日{change:+.1f}%)")

        # ---- 维度 10：前视镜市场身份 ----
        # 行业市值龙头 / 涨停板人气 / 量能异动 → 不是后视镜看历史，而是看"当下正在发生什么"
        ident = identities.get(code, {})
        id_score = ident.get("identity_score", 0)
        if id_score >= 3:
            score += 2
            reasons.append("市场身份突出(行业龙头+涨停+量能异动)")
        elif id_score >= 2:
            score += 1
            id_parts = []
            if ident.get("is_industry_leader"):
                id_parts.append("行业市值第一")
            if ident.get("is_limit_up"):
                id_parts.append("涨停")
            if ident.get("vol_anomaly"):
                id_parts.append("量能异动")
            reasons.append(f"市场身份: {'+'.join(id_parts)}")

        # 将身份信息附加到股票数据
        s["market_identity"] = ident

        scored.append({
            **s,
            "qt": qt,
            "score": score,
            "reasons": reasons,
            # 用腾讯 qt 数据覆写关键字段（更可靠）
            "change_pct": change,
            "turnover": turnover,
            "amplitude": amplitude if amplitude else s.get("amplitude", 0),
        })

    # 按得分排序
    scored.sort(key=lambda x: x["score"], reverse=True)

    # 统计
    high = [s for s in scored if s["score"] >= 8]
    mid = [s for s in scored if 5 <= s["score"] < 8]
    low = [s for s in scored if s["score"] < 5]
    print(f"  [STATS] 高得分(>=8): {len(high)} | 中得分(5-7): {len(mid)} | 低得分(<5): {len(low)}")

    return scored


# ============================================================
# Step 4: 生成选股池 JSON
# ============================================================

def build_stock_pool(scored, market_phase=None):
    """从得分列表中按 Wiki 逻辑构建 30 只选股池

    Args:
        scored: 打分后的股票列表
        market_phase: _detect_market_phase() 的返回值（可选，用于动态配额）
    """
    print(f"\n[STOCK] Step 4: 构建选股池...")
    phase_label = market_phase["phase"] if market_phase else "未知"

    # === 交易池硬门槛：流通市值 ≥ 80亿 ===
    # 数据收集阶段放宽到 50 亿（用于 period_returns 分析），
    # 但最终选股池仍只纳入 ≥ 80 亿的标的
    scored = [s for s in scored if s["circ_mcap_yi"] >= MIN_CAP_YI]
    print(f"  过滤至 ≥{MIN_CAP_YI}亿: {len(scored)} 只")

    # 分类统计
    by_theme = defaultdict(list)
    for s in scored:
        by_theme[s["theme"]].append(s)

    # === 观察股 (20只) ===
    # 策略：覆盖主要方向的代表性标的，配额按市场阶段动态调整
    phase = market_phase["phase"] if market_phase else "震荡"

    # 动态配额表：{phase: {theme: quota}}
    QUOTAS = {
        "主升":     {"AI硬件": 10, "防御消费": 1, "金融": 1, "大宗商品": 2, "高端制造": 2, "AI应用": 2, "新能源": 1, "其他": 1},
        "偏强震荡":  {"AI硬件": 7,  "防御消费": 2, "金融": 1, "大宗商品": 3, "高端制造": 3, "AI应用": 2, "新能源": 1, "其他": 1},
        "震荡":     {"AI硬件": 5,  "防御消费": 3, "金融": 2, "大宗商品": 3, "高端制造": 3, "AI应用": 2, "新能源": 1, "其他": 1},
        "偏弱震荡":  {"AI硬件": 3,  "防御消费": 4, "金融": 3, "大宗商品": 3, "高端制造": 3, "AI应用": 2, "新能源": 1, "其他": 1},
        "退潮":     {"AI硬件": 2,  "防御消费": 5, "金融": 4, "大宗商品": 3, "高端制造": 3, "AI应用": 1, "新能源": 1, "其他": 1},
    }
    quotas = QUOTAS.get(phase, QUOTAS["震荡"])
    theme_pool = {theme: [s for s in scored if s["theme"] == theme] for theme in quotas}
    observe = []
    for theme, n in quotas.items():
        observe.extend(theme_pool.get(theme, [])[:n])
    observe = observe[:20]

    # === 重点交易个股（退潮期减少到 5 只，优先选超跌反弹） ===
    trade_target = 5 if phase in ("退潮", "偏弱震荡") else 10
    trade = []

    # 精选标准：
    # 1. 得分 >= 4（退潮期标准放宽）
    # 2. 不在观察股列表中（避免重复）
    observe_codes = {s["code"] for s in observe}
    candidates = [
        s for s in scored
        if s["score"] >= 4
        and s["code"] not in observe_codes
    ]

    # 退潮期优先选超跌反弹
    if phase in ("退潮", "偏弱震荡"):
        with_signal = [s for s in candidates if any(
            "超跌反弹" in r or "退潮超跌" in r for r in s.get("reasons", [])
        )]
        others = [s for s in candidates if s not in with_signal]
        trade.extend(with_signal[:trade_target])
        trade.extend(others[:max(0, trade_target - len(trade))])
    else:
        # 优先选有弱转强/分歧转一致信号的
        with_signal = [s for s in candidates if any(
            "弱转强" in r or "分歧转一致" in r for r in s["reasons"]
        )]
        without_signal = [s for s in candidates if s not in with_signal]
        trade.extend(with_signal[:max(6, trade_target - 4)])
        trade.extend(without_signal[:max(0, trade_target - len(trade))])

    trade = trade[:trade_target]

    # === 输出格式 ===
    def format_stock(s, category):
        qt = s.get("qt", {})
        ident = s.get("market_identity", {})
        return {
            "code": s["code"],
            "name": s["name"],
            "price": s["price"],
            "change_pct": s["change_pct"],
            "circ_mcap_yi": s["circ_mcap_yi"],
            "pe": s.get("pe", 0),
            "turnover": s.get("turnover", 0),
            "vol_ratio": s.get("vol_ratio", 0),
            "sector": s["sector"],
            "theme": s["theme"],
            "amount_yi": qt.get("amount_yi", 0) or 0,
            "amplitude": qt.get("amplitude", 0) or 0,
            "score": s["score"],
            "category": category,
            "wiki_reasons": s["reasons"],
            "market_identity": {
                "is_industry_leader": ident.get("is_industry_leader", False),
                "is_limit_up": ident.get("is_limit_up", False),
                "vol_anomaly": ident.get("vol_anomaly", False),
                "amp_anomaly": ident.get("amp_anomaly", False),
                "industry_rank": ident.get("industry_rank", 99),
                "identity_score": ident.get("identity_score", 0),
            },
        }

    # === 持仓交叉引用（从 positions.json 读取） ===
    position_cross_ref = []
    positions_path = os.path.join(DATA_DIR, "positions.json")
    if os.path.exists(positions_path):
        try:
            with open(positions_path, "r", encoding="utf-8") as f:
                pos_data = json.load(f)
            all_codes = {s["code"] for s in observe + trade}
            for pos in pos_data.get("positions", []):
                if pos.get("code") in all_codes:
                    position_cross_ref.append({
                        "code": pos["code"],
                        "name": pos.get("name", ""),
                        "entry_date": pos.get("entry_date", ""),
                        "floating_pnl_pct": pos.get("floating_pnl_pct", 0),
                        "days_held": pos.get("days_held", 0),
                        "divergence_stage": pos.get("divergence_stage", "?"),
                    })
        except Exception:
            pass

    result = {
        "meta": {
            "updated": datetime.now().isoformat(),
            "min_cap_yi": MIN_CAP_YI,
            "data_collect_min_cap_yi": DATA_COLLECT_MIN_CAP_YI,
            "total_screened": len(scored),
            "market_phase": market_phase if market_phase else "退潮期",
            "positions_file": "2-wiki/data/positions.json",
            "wiki_frameworks": [
                "[[龙头认知]]", "[[主要矛盾与核心论]]", "[[赚钱效应]]",
                "[[弱转强]]", "[[分歧与一致]]", "[[情绪周期]]",
                "[[共振与跷跷板]]"
            ],
        },
        "observe": [format_stock(s, "观察股") for s in observe],
        "trade": [format_stock(s, "重点交易") for s in trade],
        "position_cross_ref": position_cross_ref,
        "theme_summary": {
            theme: len([s for s in observe + trade if s["theme"] == theme])
            for theme in CORE_THEMES
        },
    }

    save_json(OUTPUT_FILE, result)

    print(f"\n[OK] 选股池已保存: {OUTPUT_FILE}")
    print(f"   [观察股] {len(observe)} 只 | [重点交易] {len(trade)} 只")

    # 打印摘要
    print(f"\n{'='*60}")
    print(f"选股池摘要")
    print(f"{'='*60}")
    print(f"\n--- 观察股 ({len(observe)}只) ---")
    for i, s in enumerate(observe):
        print(f"  {i+1:2d}. {s['name']:6s} {s['code']:6s} "
              f"市值{s['circ_mcap_yi']:6.0f}亿 {s['theme']:6s} "
              f"{s['change_pct']:+.1f}% 得分{s['score']}")

    print(f"\n--- 重点交易 ({len(trade)}只) ---")
    for i, s in enumerate(trade):
        signals = [r for r in s.get("reasons", []) if any(
            k in r for k in ["弱转强", "分歧转一致", "放量走强", "今日强势"]
        )]
        signal_str = "; ".join(signals[:2]) if signals else "等待信号"
        print(f"  {i+1:2d}. {s['name']:6s} {s['code']:6s} "
              f"市值{s['circ_mcap_yi']:6.0f}亿 {s['theme']:6s} "
              f"{s['change_pct']:+.1f}% | {signal_str}")

    return result


# ============================================================
# Step 4.1: 前视镜维度（市场身份）
# ============================================================

def _compute_market_identity(stocks, qt_data):
    """计算每只股票的"前视镜"市场身份。

    从纯后视镜（只看历史涨幅）变为后视镜 + 前视镜（行业地位、涨停板人气、
    量能异动、振幅异常等"当下正在发生"的信号）。

    Returns:
        dict[code] → {
            is_industry_leader: bool,  # 行业市值第一
            is_limit_up: bool,         # 今日涨停
            vol_anomaly: bool,         # 量能异动（量比>3或成交额行业前3）
            amp_anomaly: bool,         # 振幅异常（>10%）
            industry_rank: int,        # 行业市值排名
            identity_score: int,       # 综合市场身份分(0-3)
        }
    """
    if not stocks or not qt_data:
        return {}

    # 行业内市值排名
    industry_caps = defaultdict(list)
    for s in stocks:
        industry_caps[s.get("sector", "未知")].append((s["code"], s.get("circ_mcap_yi", 0)))
    for sector in industry_caps:
        industry_caps[sector].sort(key=lambda x: x[1], reverse=True)

    # 行业内成交额排名
    industry_amounts = defaultdict(list)
    for s in stocks:
        qt = qt_data.get(s["code"], {})
        amt = qt.get("amount_yi", 0) or 0
        industry_amounts[s.get("sector", "未知")].append((s["code"], amt))
    for sector in industry_amounts:
        industry_amounts[sector].sort(key=lambda x: x[1], reverse=True)

    identities = {}
    for s in stocks:
        code = s["code"]
        qt = qt_data.get(code, {})
        sector = s.get("sector", "未知")

        # 行业市值排名
        cap_list = industry_caps.get(sector, [])
        cap_rank = next((i + 1 for i, (c, _) in enumerate(cap_list) if c == code), 99)

        # 行业成交额排名
        amt_list = industry_amounts.get(sector, [])
        amt_rank = next((i + 1 for i, (c, _) in enumerate(amt_list) if c == code), 99)

        # 涨停检测
        change = qt.get("change_pct", 0) or s.get("change_pct", 0) or 0
        is_limit_up = change > 9.5

        # 量能异动
        vol_ratio = s.get("vol_ratio", 0) or 0
        amt = qt.get("amount_yi", 0) or 0
        vol_anomaly = vol_ratio > 3 or amt_rank <= 3

        # 振幅异常
        amplitude = qt.get("amplitude", 0) or 0
        amp_anomaly = amplitude > 10

        # 综合身份评分（0-3 分）
        identity_score = 0
        if cap_rank == 1:
            identity_score += 1  # 行业市值龙头
        if is_limit_up:
            identity_score += 1  # 涨停板人气
        if vol_anomaly:
            identity_score += 1  # 量能异动
        # 硬顶 3 分

        identities[code] = {
            "is_industry_leader": cap_rank == 1,
            "is_limit_up": is_limit_up,
            "vol_anomaly": vol_anomaly,
            "amp_anomaly": amp_anomaly,
            "industry_rank": cap_rank,
            "identity_score": identity_score,
        }

    return identities


# ============================================================
# ============================================================
# ============================================================
# Step 4.2: 市场阶段自动检测（六维加权评分）
# ============================================================

def _load_indices_master():
    """加载 indices_master.json，返回 8 大指数近 20 日 OHLCV 数据"""
    path = os.path.join(DATA_DIR, "market", "indices_master.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _detect_market_phase(period_returns=None):
    """六维加权评分检测当前市场阶段。

    Args:
        period_returns: period_returns.json 的输出 dict（可选，用于增强信号）

    Returns:
        dict: {
            phase, score, confidence, dimensions, signals,
            concentration_advice, trajectory_adjustments
        }
    """
    indices = _load_indices_master()
    dims = {}
    signals = []

    # ===== 维度 1：科创 50 趋势（权重 30%） =====
    if indices and "科创50" in indices.get("data", {}):
        kc50 = indices["data"]["科创50"]
        closes = [d["close"] for d in kc50]
        if len(closes) >= 20:
            ret_20d = round((closes[-1] / closes[-21] - 1) * 100, 2) if closes[-21] != 0 else 0
            ret_10d = round((closes[-1] / closes[-11] - 1) * 100, 2) if closes[-11] != 0 else 0
            ret_5d = round((closes[-1] / closes[-6] - 1) * 100, 2) if closes[-6] != 0 else 0
        else:
            ret_20d = ret_10d = ret_5d = 0
    else:
        ret_20d = ret_10d = ret_5d = 0

    if ret_20d > 10:
        trend_score, trend_detail = 3, f"科创50 20日{ret_20d:+.1f}% 强趋势向上"
    elif ret_20d > 5:
        trend_score, trend_detail = 2, f"科创50 20日{ret_20d:+.1f}% 温和上升"
    elif ret_20d > 0:
        trend_score, trend_detail = 1, f"科创50 20日{ret_20d:+.1f}% 微涨"
    elif ret_20d > -3:
        trend_score, trend_detail = -1, f"科创50 20日{ret_20d:+.1f}% 微跌"
    elif ret_20d > -5:
        trend_score, trend_detail = -2, f"科创50 20日{ret_20d:+.1f}% 温和下跌"
    else:
        trend_score, trend_detail = -3, f"科创50 20日{ret_20d:+.1f}% 强趋势向下"
    dims["trend"] = {"score": trend_score, "weight": 0.30, "detail": trend_detail}

    # ===== 维度 2：指数广度（权重 20%） =====
    up_count = 0
    breadth_detail_parts = []
    if indices:
        for name, data in indices["data"].items():
            closes = [d["close"] for d in data]
            if len(closes) >= 11:
                ret = (closes[-1] / closes[-11] - 1) * 100
                if ret > 0:
                    up_count += 1
                breadth_detail_parts.append(f"{name}{ret:+.1f}%")
    breadth_detail = f"{up_count}/8指数10日上涨 ({', '.join(breadth_detail_parts[:4])}...)"

    if up_count >= 7:
        breadth_score = 3
    elif up_count >= 5:
        breadth_score = 2
    elif up_count >= 3:
        breadth_score = 0
    elif up_count >= 1:
        breadth_score = -2
    else:
        breadth_score = -3
    dims["breadth"] = {"score": breadth_score, "weight": 0.20, "detail": breadth_detail}

    # ===== 维度 3：动能方向（权重 15%） =====
    kc50_data = indices["data"].get("科创50", []) if indices else []
    if len(kc50_data) >= 6:
        kc50_5d = round((kc50_data[-1]["close"] / kc50_data[-6]["close"] - 1) * 100, 2)
    else:
        kc50_5d = ret_5d

    if kc50_5d > ret_20d and kc50_5d > 3:
        momentum_score, momentum_detail = 2, f"5日{kc50_5d:+.1f}% > 20日{ret_20d:+.1f}% 加速上涨"
    elif kc50_5d > ret_20d:
        momentum_score, momentum_detail = 1, f"5日{kc50_5d:+.1f}% > 20日{ret_20d:+.1f}% 短期强于长期"
    elif abs(kc50_5d - ret_20d) < 2:
        momentum_score, momentum_detail = 0, f"5日{kc50_5d:+.1f}% ≈ 20日{ret_20d:+.1f}% 动能平稳"
    elif kc50_5d < ret_20d and kc50_5d > -3:
        momentum_score, momentum_detail = -1, f"5日{kc50_5d:+.1f}% < 20日{ret_20d:+.1f}% 短期弱于长期"
    else:
        momentum_score, momentum_detail = -2, f"5日{kc50_5d:+.1f}% < 20日{ret_20d:+.1f}% 加速下跌"
    dims["momentum"] = {"score": momentum_score, "weight": 0.15, "detail": momentum_detail}

    # ===== 维度 4：极端波动（权重 15%） =====
    extreme_detail_parts = []
    extreme_score = 2  # 默认稳定
    if indices:
        big_drops = 0
        consecutive_big_drops = 0
        prev_was_drop = False
        for i in range(max(0, len(kc50_data) - 5), len(kc50_data)):
            d = kc50_data[i]
            chg = d.get("change_pct", 0)
            if chg < -3:
                big_drops += 1
                if prev_was_drop:
                    consecutive_big_drops += 1
                prev_was_drop = True
                extreme_detail_parts.append(f"{d['date']} {chg:+.1f}%")
            else:
                prev_was_drop = False
                if abs(chg) > 3:
                    extreme_detail_parts.append(f"{d['date']} {chg:+.1f}%")

        # 检查所有指数
        any_big_down = False
        all_big_downs = 0
        for name, data in indices["data"].items():
            for d in data[-5:]:
                if d.get("change_pct", 0) < -5:
                    all_big_downs += 1
                    any_big_down = True

        if any_big_down and all_big_downs >= 3:
            extreme_score = -2
            extreme_detail = f"近5日{all_big_downs}次指数单日跌>5% 恐慌释放"
            signals.append("科创50两天累计-10%+恐慌释放")
        elif consecutive_big_drops >= 1:
            extreme_score = -3
            extreme_detail = f"连续2日有指数跌>3% 退潮加速"
            signals.append("连续暴跌退潮加速")
        elif big_drops >= 1:
            extreme_score = -1
            extreme_detail = f"有指数单日跌>3% ({', '.join(extreme_detail_parts[:3])})"
        elif extreme_detail_parts:
            extreme_score = 1
            extreme_detail = f"可控波动 ({', '.join(extreme_detail_parts[:3])})"
        else:
            extreme_score = 2
            extreme_detail = "无极端波动 稳定"
    else:
        extreme_detail = "无指数数据"
    dims["extremes"] = {"score": extreme_score, "weight": 0.15, "detail": extreme_detail}

    # ===== 维度 5：大小票一致性（权重 10%） =====
    sz50_data = indices["data"].get("上证50", []) if indices else []
    zz1000_data = indices["data"].get("中证1000", []) if indices else []

    if len(sz50_data) >= 6 and len(zz1000_data) >= 6:
        sz50_5d = round((sz50_data[-1]["close"] / sz50_data[-6]["close"] - 1) * 100, 2)
        zz1000_5d = round((zz1000_data[-1]["close"] / zz1000_data[-6]["close"] - 1) * 100, 2)
        gap = abs(sz50_5d - zz1000_5d)
        same_direction = (sz50_5d > 0) == (zz1000_5d > 0)

        if same_direction and gap < 2:
            div_score, div_detail = 1, f"上证50{sz50_5d:+.1f}% vs 中证1000{zz1000_5d:+.1f}% 共振健康"
        elif same_direction:
            div_score, div_detail = 0, f"上证50{sz50_5d:+.1f}% vs 中证1000{zz1000_5d:+.1f}% 同向但幅度有差距"
        elif gap > 5:
            div_score, div_detail = -2, f"上证50{sz50_5d:+.1f}% vs 中证1000{zz1000_5d:+.1f}% 严重分化(某队护盘?)"
        else:
            div_score, div_detail = -1, f"上证50{sz50_5d:+.1f}% vs 中证1000{zz1000_5d:+.1f}% 跷跷板分歧"
    else:
        div_score, div_detail = 0, "数据不足"
    dims["divergence"] = {"score": div_score, "weight": 0.10, "detail": div_detail}

    # ===== 维度 6：量能趋势（权重 10%） =====
    if indices:
        # 统计所有指数近5日 vs 近20日均量
        vol_ratios = []
        for name in ["科创50", "上证指数", "深证成指", "创业板指"]:
            data = indices["data"].get(name, [])
            if len(data) >= 20:
                vol_5d_avg = sum(d.get("volume", 0) for d in data[-5:]) / 5
                vol_20d_avg = sum(d.get("volume", 0) for d in data[-20:]) / 20
                if vol_20d_avg > 0:
                    vol_ratios.append(vol_5d_avg / vol_20d_avg)

        if vol_ratios:
            avg_vol_ratio = sum(vol_ratios) / len(vol_ratios)
        else:
            avg_vol_ratio = 1.0
    else:
        avg_vol_ratio = 1.0

    if avg_vol_ratio > 1.3:
        vol_score, vol_detail = 1, f"四大指数5日均量/20日均量={avg_vol_ratio:.2f} 放量"
        signals.append("放量资金进场")
    elif avg_vol_ratio > 0.7:
        vol_score, vol_detail = 0, f"四大指数5日均量/20日均量={avg_vol_ratio:.2f} 正常"
    else:
        vol_score, vol_detail = -1, f"科创50 5日均量<20日均量×0.7 严重缩量"
        signals.append("缩量交投萎缩")
    dims["volume"] = {"score": vol_score, "weight": 0.10, "detail": vol_detail}

    # ===== 综合评分 =====
    weighted_score = round(sum(
        d["score"] * d["weight"] for d in dims.values()
    ), 1)

    # ===== period_returns 增强信号（调整因子） =====
    if period_returns:
        traj = period_returns.get("trajectory_analysis", {})
        cross = period_returns.get("cross_period", {})

        type_dist = traj.get("type_distribution", {})
        reversal_count = type_dist.get("高位逆转 🔴", {}).get("count", 0)
        front_loaded_count = type_dist.get("前重后轻 📉", {}).get("count", 0)
        decel_count = cross.get("decel_count", 0)
        accel_count = cross.get("accel_count", 0)

        # 20日 Top30 今日涨停数
        d20_today = period_returns.get("diagnostics", {}).get("20d", {}).get("today_distribution", {})
        d20_limits = d20_today.get("涨停(>9.5%)", 0)

        d5_only = len(cross.get("only_5d", []))

        adjustments = []
        if reversal_count >= 5:
            weighted_score -= 2
            adjustments.append(f"高位逆转{reversal_count}只→-2")
            signals.append("大量翻倍股正在派发")
        if front_loaded_count >= 12:
            weighted_score -= 1
            adjustments.append(f"前重后轻{front_loaded_count}只→-1")
        if decel_count > accel_count + 5:
            weighted_score -= 1
            adjustments.append(f"减速主导({decel_count}vs{accel_count})→-1")
        if d20_limits < 3:
            weighted_score -= 1
            adjustments.append(f"涨停骤减(仅{d20_limits}只)→-1")
        if d5_only >= 10:
            weighted_score -= 1
            adjustments.append(f"次新妖股抱团({d5_only}只仅5日)→-1")
            signals.append("次新妖股崩塌迹象")

        if adjustments:
            dims["trajectory_adjustments"] = {"detail": "; ".join(adjustments)}
            signals.append("路径分布恶化")

    # ===== 边界规则（直接判定） =====
    override_phase = None
    if indices:
        # 连续3日 ≥7/8 指数下跌 + 科创50跌幅 > 5%
        consecutive_down = 0
        for i in range(len(kc50_data) - 3, len(kc50_data)):
            if i < 0:
                continue
            d = kc50_data[i]
            # 统计当日多少指数下跌
            down_idx = 0
            for name, idata in indices["data"].items():
                if i < len(idata):
                    if idata[i].get("change_pct", 0) < 0:
                        down_idx += 1
            if down_idx >= 7:
                consecutive_down += 1
            else:
                consecutive_down = 0

        if consecutive_down >= 3 and ret_20d < -5:
            override_phase = "退潮"
            signals.append("边界规则触发: 连续3日≥7/8指数下跌")

        # 连续2日 8/8 全红 + 科创50涨幅 > 5%
        consecutive_up = 0
        for i in range(max(0, len(kc50_data) - 3), len(kc50_data)):
            if i >= len(kc50_data):
                continue
            d = kc50_data[i]
            all_up = all(
                i < len(idata) and idata[i].get("change_pct", 0) > 0
                for name, idata in indices["data"].items()
            )
            if all_up:
                consecutive_up += 1
            else:
                consecutive_up = 0

        if consecutive_up >= 2 and ret_5d > 5:
            override_phase = "主升"
            signals.append("边界规则触发: 连续2日全部指数上涨")

        # 某队明显护盘（上证50逆势涨 > 1% 而中证1000跌 > 2%）
        if len(sz50_data) >= 1 and len(zz1000_data) >= 1:
            sz50_today = sz50_data[-1].get("change_pct", 0)
            zz1000_today = zz1000_data[-1].get("change_pct", 0)
            if sz50_today > 1 and zz1000_today < -2:
                signals.append("某队护盘信号(上证50+中证1000-)")
                if not override_phase:
                    weighted_score = min(weighted_score, -2)

    # ===== 阶段判定 =====
    if override_phase:
        phase = override_phase
    elif weighted_score >= 5:
        phase = "主升"
    elif weighted_score >= 2:
        phase = "偏强震荡"
    elif weighted_score >= -1:
        phase = "震荡"
    elif weighted_score >= -4:
        phase = "偏弱震荡"
    else:
        phase = "退潮"

    # 置信度估算
    dims_with_data = sum(1 for d in dims.values() if "数据不足" not in d.get("detail", ""))
    confidence = round(min(0.95, dims_with_data / 6 * 0.9 + 0.1), 2)

    # 集中度建议
    if phase in ("主升", "偏强震荡"):
        conc_advice = "集中"
    elif phase == "震荡":
        conc_advice = "适度集中"
    else:
        conc_advice = "分散"

    # 路径调整建议
    traj_adjustments = []
    if phase in ("退潮", "偏弱震荡"):
        traj_adjustments.append("启用退潮超跌反弹识别")
    if phase == "偏强震荡":
        traj_adjustments.append("关注V型反转+匀速推进")

    return {
        "phase": phase,
        "score": round(weighted_score, 1),
        "confidence": confidence,
        "dimensions": dims,
        "signals": signals,
        "concentration_advice": conc_advice,
        "trajectory_adjustments": traj_adjustments,
    }


# ============================================================
# Step 4.5: 通过 K 线 API 获取准确的周期涨跌幅
# （替代腾讯 qt API 不可靠的 ret_5d/ret_10d/ret_20d 字段）
# ============================================================

def _get_market_prefix(code):
    """判断股票代码的市场前缀: sh / sz"""
    return "sh" if code.startswith(("6", "9")) else "sz"


def _fetch_single_stock_klines(code, market_prefix, days=45):
    """获取单只个股的日K线数据（腾讯 K 线 API）

    默认请求 45 个交易日，足够计算 5/10/20/40 日四个周期的区间涨跌幅。
    """
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {"param": f"{market_prefix}{code},day,,,{days},qfq"}
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = SESSION.get(url, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            return code, []
        inner = data.get("data", {}).get(f"{market_prefix}{code}", {})
        klines = inner.get("day", []) or inner.get("qfqday", [])
        # 提取收盘价序列
        closes = []
        for line in klines:
            if len(line) >= 3:
                try:
                    closes.append(float(line[2]))
                except (ValueError, TypeError):
                    continue
        return code, closes
    except Exception:
        return code, []


def fetch_period_returns_via_klines(stocks, max_workers=15):
    """通过 K 线数据批量计算准确的多周期涨跌幅。

    返回: dict[code] → {ret_1d, ret_3d, ret_5d, ret_10d, ret_20d, ret_40d, valid}
    valid=False 表示数据不足以计算（新股或API失败）
    """
    print(f"\n[STOCK] Step 4.5: 通过 K 线 API 计算准确周期涨跌幅...")
    print(f"  并发线程: {max_workers} | 股票数: {len(stocks)} | 周期: 1/3/5/10/20/40日")

    results = {}
    completed = 0
    total = len(stocks)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for s in stocks:
            code = s["code"]
            prefix = _get_market_prefix(code)
            futures[executor.submit(_fetch_single_stock_klines, code, prefix, 45)] = code

        for future in as_completed(futures):
            code, closes = future.result()
            completed += 1

            if len(closes) >= 45:
                # ≥45条K线：可计算全部 6 个周期（1/3/5/10/20/40日）
                # 45 条确保 40 日涨幅的参考点距离上市日至少 4 个交易日，排除 IPO 首日噪音
                current = closes[-1]
                ret_1d = round((current / closes[-2] - 1) * 100, 2) if len(closes) >= 2 and closes[-2] != 0 else 0
                ret_3d = round((current / closes[-4] - 1) * 100, 2) if closes[-4] != 0 else 0
                ret_5d = round((current / closes[-6] - 1) * 100, 2) if closes[-6] != 0 else 0
                ret_10d = round((current / closes[-11] - 1) * 100, 2) if closes[-11] != 0 else 0
                ret_20d = round((current / closes[-21] - 1) * 100, 2) if closes[-21] != 0 else 0
                ret_40d = round((current / closes[-41] - 1) * 100, 2) if closes[-41] != 0 else 0
                results[code] = {"ret_1d": ret_1d, "ret_3d": ret_3d, "ret_5d": ret_5d, "ret_10d": ret_10d, "ret_20d": ret_20d, "ret_40d": ret_40d, "valid": True, "has_40d": True}
            elif len(closes) >= 25:
                # 25-44条K线：可计算 1/3/5/10/20日，但不够算 40 日
                current = closes[-1]
                ret_1d = round((current / closes[-2] - 1) * 100, 2) if len(closes) >= 2 and closes[-2] != 0 else 0
                ret_3d = round((current / closes[-4] - 1) * 100, 2) if closes[-4] != 0 else 0
                ret_5d = round((current / closes[-6] - 1) * 100, 2) if closes[-6] != 0 else 0
                ret_10d = round((current / closes[-11] - 1) * 100, 2) if closes[-11] != 0 else 0
                ret_20d = round((current / closes[-21] - 1) * 100, 2) if closes[-21] != 0 else 0
                results[code] = {"ret_1d": ret_1d, "ret_3d": ret_3d, "ret_5d": ret_5d, "ret_10d": ret_10d, "ret_20d": ret_20d, "ret_40d": 0, "valid": True, "has_40d": False}
            elif len(closes) >= 6:
                # 只有部分数据（可能是新股），只计算短周期
                current = closes[-1]
                ret_1d = round((current / closes[-2] - 1) * 100, 2) if len(closes) >= 2 and closes[-2] != 0 else 0
                ret_3d = round((current / closes[-4] - 1) * 100, 2) if len(closes) >= 4 and closes[-4] != 0 else 0
                ret_5d = round((current / closes[-6] - 1) * 100, 2) if closes[-6] != 0 else 0
                ret_10d = round((current / closes[-1] - 1) * 100, 2) if len(closes) >= 11 and closes[-11] != 0 else 0
                results[code] = {"ret_1d": ret_1d, "ret_3d": ret_3d, "ret_5d": ret_5d, "ret_10d": ret_10d, "ret_20d": 0, "ret_40d": 0, "valid": False, "has_40d": False}
            else:
                results[code] = {"ret_1d": 0, "ret_3d": 0, "ret_5d": 0, "ret_10d": 0, "ret_20d": 0, "ret_40d": 0, "valid": False, "has_40d": False}

            if completed % 500 == 0:
                print(f"  已处理 {completed}/{total} 只...")

    valid_count = sum(1 for v in results.values() if v["valid"])
    has_40d_count = sum(1 for v in results.values() if v.get("has_40d"))
    print(f"  [OK] {valid_count}/{total} 只有效K线数据（≥25日），其中 {has_40d_count} 只可计算40日涨幅")
    return results


# ============================================================
# Step 5: 多周期区间涨幅排名 + 诊断框架（赚钱效应时间维度）
# ============================================================

def _compute_single_period_diag(top30):
    """对单个周期的 Top 30 计算聚合诊断指标"""
    if not top30:
        return {}

    returns = [s["ret"] for s in top30]
    caps = [s.get("cap_yi", 0) for s in top30]
    today_chgs = [s.get("today_chg", 0) for s in top30]

    # --- 收益分布 ---
    dist = {
        "top_return": round(max(returns), 1),
        "bottom_return": round(min(returns), 1),
        "median_return": round(sorted(returns)[len(returns)//2], 1),
        "翻倍(>100%)": sum(1 for r in returns if r > 100),
        "大涨(50-100%)": sum(1 for r in returns if 50 < r <= 100),
        "中涨(30-50%)": sum(1 for r in returns if 30 < r <= 50),
        "小涨(10-30%)": sum(1 for r in returns if 10 < r <= 30),
        "微涨(0-10%)": sum(1 for r in returns if 0 < r <= 10),
        "下跌(<0%)": sum(1 for r in returns if r < 0),
    }

    # --- 行业集中度 ---
    theme_counts = defaultdict(int)
    sector_counts = defaultdict(int)
    for s in top30:
        theme_counts[s.get("theme", "其他")] += 1
        sector_counts[s.get("sector", "未知")] += 1
    dist["theme_top5"] = [{"theme": t, "count": c} for t, c in
                          sorted(theme_counts.items(), key=lambda x: x[1], reverse=True)[:5]]
    dist["sector_top5"] = [{"sector": t, "count": c} for t, c in
                           sorted(sector_counts.items(), key=lambda x: x[1], reverse=True)[:5]]
    # 行业集中度：Top1 行业占比
    dist["top_sector_pct"] = round(dist["sector_top5"][0]["count"] / 30 * 100) if dist["sector_top5"] else 0

    # --- 市值分布 ---
    dist["cap_distribution"] = {
        "大票(>500亿)": sum(1 for c in caps if c > 500),
        "中票(200-500亿)": sum(1 for c in caps if 200 < c <= 500),
        "中票(80-200亿)": sum(1 for c in caps if 80 < c <= 200),
        "中小票(50-80亿)": sum(1 for c in caps if 50 < c <= 80),
        "小票(<50亿)": sum(1 for c in caps if 0 < c < 50),
    }

    # --- 今日涨跌分布 ---
    dist["today_distribution"] = {
        "上涨": sum(1 for c in today_chgs if c > 0),
        "下跌": sum(1 for c in today_chgs if c < 0),
        "涨停(>9.5%)": sum(1 for c in today_chgs if c > 9.5),
        "大跌(< -5%)": sum(1 for c in today_chgs if c < -5),
    }

    return dist


def _compute_cross_period(rankings):
    """跨周期分析：持续走强 / 加速 / 减速

    纳入 5/10/20/40 日四个周期维度：
      - 短周期（5/10/20日）：判断加速/减速/短期爆发
      - 长周期（40日）：发现慢牛股（20日排名不突出但40日突出）
    """
    # 各周期代码集合
    codes = {}
    for period in ["1d", "3d", "5d", "10d", "20d", "40d"]:
        codes[period] = {s["code"] for s in rankings.get(period, [])}

    cross = {
        "persistent_3periods": [],   # 三周期同时出现（5+10+20）
        "persistent_4periods": [],   # 四周期同时出现（5+10+20+40）— 最强持续性
        "persistent_2periods": [],   # 两周期同时出现
        "only_5d": [],               # 仅5日出现（新爆发）
        "only_20d": [],              # 仅20日出现（早期强但近期弱）
        "only_40d": [],              # 仅40日出现（慢牛：长期强但短期不突出）
        "accelerating": [],          # 5d > 10d > 20d（加速中）
        "decelerating": [],          # 5d < 10d < 20d（减速中）
        "sudden_breakout": [],       # 1d 占比 > 50%（今日突然爆发）
        "micro_pullback": [],        # 3d 负 1d 正（微V反转）
    }

    # 跨周期出现
    all_3 = codes["5d"] & codes["10d"] & codes["20d"]
    cross["persistent_3periods"] = list(all_3)[:15]
    all_4 = codes["5d"] & codes["10d"] & codes["20d"] & codes["40d"]
    cross["persistent_4periods"] = list(all_4)[:15]
    cross["persistent_2periods"] = list(
        (codes["5d"] & codes["10d"]) | (codes["10d"] & codes["20d"]) | (codes["5d"] & codes["20d"])
    )[:20]
    cross["only_5d"] = list(codes["5d"] - codes["10d"] - codes["20d"])[:15]
    cross["only_20d"] = list(codes["20d"] - codes["10d"] - codes["5d"])[:15]
    # 🔥 仅40日出现：长期牛股但不在短周期Top30 — 这类就是"利通电子型"慢牛
    cross["only_40d"] = list(codes["40d"] - codes["20d"] - codes["10d"] - codes["5d"])[:20]

    # 加速/减速分析：构建 code → {1d, 3d, 5d, 10d, 20d, 40d} 映射
    period_ret_map = defaultdict(lambda: {"name": "?", "1d": 0, "3d": 0, "5d": 0, "10d": 0, "20d": 0, "40d": 0, "theme": ""})
    for period in ["1d", "3d", "5d", "10d", "20d", "40d"]:
        for s in rankings.get(period, []):
            code = s["code"]
            period_ret_map[code]["name"] = s.get("name", "?")
            period_ret_map[code][period] = s.get("ret", 0)
            period_ret_map[code]["theme"] = s.get("theme", "")

    accel_list, decel_list = [], []
    for code, rets in period_ret_map.items():
        r5, r10, r20 = rets["5d"], rets["10d"], rets["20d"]
        if r5 and r10 and r20 and r5 > 0 and r10 > 0 and r20 > 0:
            if r5 > r10 > r20:
                accel_list.append({"code": code, "name": rets["name"], "theme": rets["theme"],
                                   "5d": round(r5, 1), "10d": round(r10, 1), "20d": round(r20, 1),
                                   "gap": round(r5 - r20, 1)})
            elif r5 < r10 < r20:
                decel_list.append({"code": code, "name": rets["name"], "theme": rets["theme"],
                                   "5d": round(r5, 1), "10d": round(r10, 1), "20d": round(r20, 1),
                                   "gap": round(r20 - r5, 1)})

    cross["accelerating"] = sorted(accel_list, key=lambda x: x["gap"], reverse=True)[:10]
    cross["decelerating"] = sorted(decel_list, key=lambda x: x["gap"], reverse=True)[:10]
    cross["accel_count"] = len(accel_list)
    cross["decel_count"] = len(decel_list)

    # 突发爆发（今日涨幅占5日涨幅 > 50%）
    breakout_list = []
    # 微V反转（3日跌但今日涨）
    micro_v_list = []
    for code, rets in period_ret_map.items():
        r1, r3, r5 = rets["1d"], rets["3d"], rets["5d"]
        if r1 > 0 and r5 > 5 and r1 > r5 * 0.5:
            breakout_list.append({"code": code, "name": rets["name"], "theme": rets["theme"],
                                  "1d": round(r1, 1), "5d": round(r5, 1),
                                  "pct_of_5d": round(r1 / r5 * 100) if r5 != 0 else 0})
        if r1 > 1 and r3 < 0:
            micro_v_list.append({"code": code, "name": rets["name"], "theme": rets["theme"],
                                 "1d": round(r1, 1), "3d": round(r3, 1),
                                 "swing": round(r1 - r3, 1)})

    cross["sudden_breakout"] = sorted(breakout_list, key=lambda x: x["pct_of_5d"], reverse=True)[:10]
    cross["micro_pullback"] = sorted(micro_v_list, key=lambda x: x["swing"], reverse=True)[:10]
    cross["sudden_breakout_count"] = len(breakout_list)
    cross["micro_pullback_count"] = len(micro_v_list)

    return cross


def _compute_trajectory_analysis(rankings, kline_returns, market_phase=None):
    """涨幅路径分解：翻倍是怎么走出来的？

    使用全量 K 线周期涨幅数据，对 20 日 Top 30 的每只标的分解为三段：
      - d1~10（最早）: 前 10 个交易日的贡献
      - d11~15（中间）: 中间 5 个交易日的贡献
      - d16~20（最近）: 最近 5 个交易日 = ret_5d

    关系：(1+ret_20d) = (1+ret_d1_10) × (1+ret_d11_15) × (1+ret_5d)

    退潮期（market_phase 为退潮/偏弱震荡）时，额外识别超跌反弹和退潮逆势放量。
    """
    is_bear = market_phase and market_phase.get("phase") in ("退潮", "偏弱震荡")
    if not kline_returns:
        return {"total_analyzed": 0, "summary": "无K线数据", "insights": ["数据不足"]}

    top20 = rankings.get("20d", [])[:30]  # 20日 Top 30

    trajectories = []
    for s in top20:
        code = s["code"]
        kr = kline_returns.get(code, {})
        if not kr or not kr.get("valid"):
            continue

        r5 = kr["ret_5d"]
        r10 = kr["ret_10d"]
        r20 = kr["ret_20d"]

        if r20 == 0:
            continue

        # 分解三段涨幅
        try:
            ret_d11_15 = round(((1 + r10 / 100) / (1 + r5 / 100) - 1) * 100, 1) if (1 + r5 / 100) != 0 else 0
            ret_d1_10 = round(((1 + r20 / 100) / (1 + r10 / 100) - 1) * 100, 1) if (1 + r10 / 100) != 0 else 0
        except (ZeroDivisionError, OverflowError):
            continue

        # 路径分类
        total_abs = abs(ret_d1_10) + abs(ret_d11_15) + abs(r5)
        if total_abs == 0:
            continue

        pct_old = abs(ret_d1_10) / total_abs
        pct_mid = abs(ret_d11_15) / total_abs
        pct_new = abs(r5) / total_abs

        # 取得 3 日涨幅用于退潮期判断
        ret_3d = kr.get("ret_3d", 0)

        # 退潮期特殊路径：超跌反弹 和 退潮逆势放量
        if is_bear and r20 < -10 and ret_3d > 5:
            traj_type = "超跌反弹 🟢"          # 20日跌>10%但近3日反弹>5%——退潮中的反抽机会
        elif is_bear and r5 > 10 and r20 < 0:
            traj_type = "退潮逆势放量 🔴"      # 退潮中近5日放量拉升——可能假突破
        elif r5 < -3 and r20 > 20:
            traj_type = "高位逆转 🔴"        # 20日涨了不少但近5日在跌——正在派发
        elif r5 < 0 and r20 > 0:
            traj_type = "短期回调 🟡"         # 20日正但近5日小跌——正常回调
        elif r5 > 0 and ret_d1_10 < -5:
            traj_type = "V型反转 🟢"          # 早期在跌、最近在涨——弱转强
        elif pct_new > 0.55:
            traj_type = "近期爆发 ⚡"          # 超过一半涨幅在最近5天完成
        elif pct_old > 0.50:
            traj_type = "前重后轻 📉"          # 大半涨幅在10天前完成，现在接近停滞
        elif pct_mid > 0.45:
            traj_type = "中段发力 🟡"          # 主要涨幅在中间5天
        elif abs(pct_old - 0.33) < 0.15 and abs(pct_mid - 0.33) < 0.15 and abs(pct_new - 0.33) < 0.15:
            traj_type = "匀速推进 🟢"          # 三段贡献均匀
        else:
            traj_type = "混合型"

        trajectories.append({
            "code": code,
            "name": s.get("name", "?"),
            "theme": s.get("theme", ""),
            "cap_yi": s.get("cap_yi", 0),
            "ret_20d": round(r20, 1),
            "ret_10d": round(r10, 1),
            "ret_5d": round(r5, 1),
            "decomp": {
                "d1_10": ret_d1_10,      # 最早段
                "d11_15": ret_d11_15,    # 中间段
                "d16_20": round(r5, 1),  # 最近段
            },
            "trajectory": traj_type,
        })

    # 按 20d 涨幅排序
    trajectories.sort(key=lambda x: x["ret_20d"], reverse=True)

    # 聚合统计
    type_counts = defaultdict(int)
    type_examples = defaultdict(list)
    for t in trajectories:
        type_counts[t["trajectory"]] += 1
        if len(type_examples[t["trajectory"]]) < 3:
            type_examples[t["trajectory"]].append(f"{t['name']}(20d:{t['ret_20d']:.0f}% 10d:{t['ret_10d']:.0f}% 5d:{t['ret_5d']:.0f}%)")

    # 取 Top 20 的路径摘要（最值得关注的标的）
    top_trajectories = trajectories[:20]

    traj_analysis = {
        "summary": "、".join(f"{k}{v}只" for k, v in sorted(type_counts.items(), key=lambda x: x[1], reverse=True)),
        "type_distribution": {k: {"count": v, "examples": type_examples[k]} for k, v in
                              sorted(type_counts.items(), key=lambda x: x[1], reverse=True)},
        "top20_trajectories": top_trajectories,
        "total_analyzed": len(trajectories),
    }

    # 关键洞察（自动生成）
    insights = []
    high_reversal = type_counts.get("高位逆转 🔴", 0)
    recent_breakout = type_counts.get("近期爆发 ⚡", 0)
    front_loaded = type_counts.get("前重后轻 📉", 0)
    steady = type_counts.get("匀速推进 🟢", 0)
    v_recovery = type_counts.get("V型反转 🟢", 0)

    if high_reversal > 3:
        insights.append(f"⚠️ {high_reversal}只高位逆转：翻倍股正在派发，追高风险极大")
    if front_loaded > 5:
        insights.append(f"📉 {front_loaded}只前重后轻：大部分涨幅已在10天前完成，当前接近停滞")
    if recent_breakout > 5:
        insights.append(f"⚡ {recent_breakout}只近期爆发：加速赶顶中，需警惕衰竭板")
    if steady > 5:
        insights.append(f"🟢 {steady}只匀速推进：趋势健康，可持续性强")
    if v_recovery > 3:
        insights.append(f"🟢 {v_recovery}只V型反转：弱转强信号，关注回调确认")
    oversold_bounce = type_counts.get("超跌反弹 🟢", 0)
    bear_fake_breakout = type_counts.get("退潮逆势放量 🔴", 0)

    if oversold_bounce > 2:
        insights.append(f"🟢 {oversold_bounce}只超跌反弹：退潮期跌够了的票开始反抽——关注反弹持续性和量能配合")
    if bear_fake_breakout > 2:
        insights.append(f"🔴 {bear_fake_breakout}只退潮逆势放量：退潮中拉升可能是假突破——除非有强逻辑支撑，否则不追")
    if not insights:
        insights.append("路径分布均匀，无极端信号")

    traj_analysis["insights"] = insights

    # === 40日涨幅路径分析 ===
    # 40日窗口更能捕捉"慢牛股"——这些票在20日窗口排名可能不突出，
    # 但拉长到40日就很明显（如利通电子型：40日翻倍但20日仅30%+）
    # 分解为 d1-20（前半段）和 d21-40（后半段=近20日）
    top40 = rankings.get("40d", [])[:30]
    trajectories_40d = []
    for s in top40:
        code = s["code"]
        kr = kline_returns.get(code, {})
        if not kr or not kr.get("has_40d"):
            continue
        r40 = kr["ret_40d"]
        r20 = kr["ret_20d"]
        if r40 == 0:
            continue
        try:
            # d1-20 = 前半段涨幅；d21-40 = 后半段 = r20（近20日涨幅）
            ret_d1_20 = round(((1 + r40 / 100) / (1 + r20 / 100) - 1) * 100, 1) if (1 + r20 / 100) != 0 else 0
        except (ZeroDivisionError, OverflowError):
            continue

        r_first = ret_d1_20   # 前半段（d1-20）
        r_second = r20         # 后半段（d21-40）= 近20日涨幅

        # 40日路径分类（比20日路径更简洁：只看前后两段的对比）
        if r_second < -3 and r40 > 20:
            traj_40d_type = "高位逆转 🔴"          # 40日涨了不少但近20日在跌
        elif r_first > r_second * 2 and r_second < 10:
            traj_40d_type = "前重后轻 📉"          # 大部分涨幅在前半段，后半段接近停滞
        elif r_second > r_first * 1.5 and r_first > -5:
            traj_40d_type = "后重前轻 🟡"          # 后半段加速，慢牛转为加速
        elif abs(r_first - r_second) < 15 and r_first > 0 and r_second > 0:
            traj_40d_type = "匀速推进 🟢"          # 前后两段均匀，慢牛趋势
        elif r_first < -5 and r_second > 10:
            traj_40d_type = "V型反转 🟢"           # 前半段跌后半段涨，弱转强
        else:
            traj_40d_type = "混合型"

        trajectories_40d.append({
            "code": code,
            "name": s.get("name", "?"),
            "theme": s.get("theme", ""),
            "cap_yi": s.get("cap_yi", 0),
            "ret_40d": round(r40, 1),
            "ret_20d": round(r20, 1),
            "decomp": {
                "d1_20": ret_d1_20,        # 前半段（前20个交易日）
                "d21_40": round(r20, 1),   # 后半段（近20个交易日）
            },
            "trajectory": traj_40d_type,
        })

    # 40日聚合统计
    type_counts_40d = defaultdict(int)
    type_examples_40d = defaultdict(list)
    for t in trajectories_40d:
        type_counts_40d[t["trajectory"]] += 1
        if len(type_examples_40d[t["trajectory"]]) < 3:
            type_examples_40d[t["trajectory"]].append(
                f"{t['name']}(40d:{t['ret_40d']:.0f}% 20d:{t['ret_20d']:.0f}%)")

    top20_40d = trajectories_40d[:20]

    traj_40d = {
        "summary": "、".join(
            f"{k}{v}只" for k, v in sorted(type_counts_40d.items(), key=lambda x: x[1], reverse=True)),
        "type_distribution": {k: {"count": v, "examples": type_examples_40d[k]} for k, v in
                              sorted(type_counts_40d.items(), key=lambda x: x[1], reverse=True)},
        "top20_trajectories": top20_40d,
        "total_analyzed": len(trajectories_40d),
    }

    # 40日关键洞察
    insights_40d = []
    front_loaded_40d = type_counts_40d.get("前重后轻 📉", 0)
    steady_40d = type_counts_40d.get("匀速推进 🟢", 0)
    back_loaded_40d = type_counts_40d.get("后重前轻 🟡", 0)
    reversal_40d = type_counts_40d.get("高位逆转 🔴", 0)
    v_recovery_40d = type_counts_40d.get("V型反转 🟢", 0)

    if steady_40d > 3:
        insights_40d.append(f"🟢 {steady_40d}只匀速推进(40d)：慢牛趋势健康——这类票在20日窗口可能被忽略")
    if back_loaded_40d > 3:
        insights_40d.append(f"🟡 {back_loaded_40d}只后重前轻(40d)：近期加速，慢牛可能正在转为加速段")
    if front_loaded_40d > 5:
        insights_40d.append(f"📉 {front_loaded_40d}只前重后轻(40d)：早期涨幅已兑现，近20日动能衰竭")
    if reversal_40d > 3:
        insights_40d.append(f"🔴 {reversal_40d}只高位逆转(40d)：40日涨幅为正但近20日转跌，派发进行中")
    if v_recovery_40d > 2:
        insights_40d.append(f"🟢 {v_recovery_40d}只V型反转(40d)：前半段跌后半段涨，弱转强确认")
    if not insights_40d:
        insights_40d.append("40日路径分布均匀，无极端信号")

    traj_40d["insights"] = insights_40d
    traj_analysis["trajectory_40d"] = traj_40d

    return traj_analysis


def _derive_trader_implications(diag, cross, traj=None):
    """根据诊断数据自动生成三类交易者的策略启示

    纳入 5/10/20/40 日四个周期的诊断数据：
      - 短周期（5/10/20日）：判断短期动能和情绪
      - 长周期（40日）：发现慢牛股和长期趋势
    """
    d20 = diag.get("20d", {})
    d5 = diag.get("5d", {})
    d40 = diag.get("40d", {})  # 🔥 40日诊断数据

    top_ret_20d = d20.get("top_return", 0)
    top_ret_40d = d40.get("top_return", 0)  # 🔥 40日首涨幅
    cap_dist = d20.get("cap_distribution", {})
    large_pct = cap_dist.get("大票(>500亿)", 0)
    small_pct = cap_dist.get("小票(<80亿)", 0)
    theme_top = d20.get("theme_top5", [])
    top_theme = theme_top[0]["theme"] if theme_top else "未知"
    top_theme_pct = theme_top[0]["count"] / 30 if theme_top else 0
    today_up = d5.get("today_distribution", {}).get("上涨", 0)
    today_limit = d5.get("today_distribution", {}).get("涨停(>9.5%)", 0)
    accel_n = cross.get("accel_count", 0)
    decel_n = cross.get("decel_count", 0)
    persistent_n = len(cross.get("persistent_3periods", []))
    persistent_4_n = len(cross.get("persistent_4periods", []))  # 🔥 四周期持续
    only_40d_n = len(cross.get("only_40d", []))  # 🔥 仅40日出现的慢牛
    # 40日翻倍/大涨统计
    d40_doubled = d40.get("翻倍(>100%)", 0)
    d40_big = d40.get("大涨(50-100%)", 0)

    # === 机构视角 ===
    if large_pct >= 10:
        if top_theme_pct > 0.5:
            inst_verdict = "主线清晰+大票主导，机构标准做多环境"
            inst_action = "【持有+加仓】趋势龙头底仓不动，等回调到均线附近加仓。聚焦主线（{}），不追高、等回踩。".format(top_theme)
        else:
            inst_verdict = "大票主导但方向分散，轮动行情"
            inst_action = "【持有+轮动】底仓分散配置（CPO/算力/半导体），活仓做行业轮动。不重仓单一方向。"
    elif large_pct >= 5:
        inst_verdict = "大小票混合，趋势+情绪并存"
        inst_action = "【半仓参与】趋势仓位持有，短线仓位观望。关注大票能否持续走强，若大票占比下降则降仓。"
    else:
        inst_verdict = "小票主导，非机构友好环境"
        inst_action = "【观望为主】小票行情下机构票的波动和趋势性不足。等大票重新主导时再加仓。"

    # === 游资视角 ===
    # 路径分析优于简单的加速/减速
    if traj:
        front_loaded = traj.get("type_distribution", {}).get("前重后轻 📉", {}).get("count", 0)
        high_reversal = traj.get("type_distribution", {}).get("高位逆转 🔴", {}).get("count", 0)
        recent_breakout = traj.get("type_distribution", {}).get("近期爆发 ⚡", {}).get("count", 0)
        steady = traj.get("type_distribution", {}).get("匀速推进 🟢", {}).get("count", 0)
        v_recovery = traj.get("type_distribution", {}).get("V型反转 🟢", {}).get("count", 0)

        if front_loaded > 5 or high_reversal > 3:
            youzi_verdict = "多数翻倍股已是前重后轻或高位逆转，追高盈亏比极差"
            youzi_action = "【收缩】不追已翻倍的标的。关注V型反转和匀速推进的少数标的，等回调确认。"
        elif recent_breakout > 5:
            youzi_verdict = "近期爆发占主导，行情在加速赶顶"
            youzi_action = "【警惕】持仓标的一旦出现放量滞涨立刻走。不做新开仓。准备好清仓条件。"
        elif steady > 5 and v_recovery > 2:
            youzi_verdict = "匀速推进+V型反转并存，赚钱效应健康且有新方向"
            youzi_action = "【积极参与】匀速推进=底仓锁仓，V型反转=试仓新方向。多手法并行。"
        elif steady > 3:
            youzi_verdict = "匀速推进为主，趋势健康可持续"
            youzi_action = "【持有+低吸】趋势底仓不动，等均线回调低吸。不做加速追涨。"
        else:
            # 回退到原始的加速/减速逻辑
            if today_limit >= 3 and accel_n > decel_n:
                youzi_verdict = "涨停活跃+加速信号，游资积极参与阶段"
                youzi_action = "【积极参与】首板/一进二接力，聚焦加速方向。弱转强信号出现即试仓，做错隔夜摁掉。警惕高潮后的衰竭板。"
            elif accel_n > decel_n and persistent_n >= 3:
                youzi_verdict = "持续走强方向存在，游资可选择性参与"
                youzi_action = "【选择性参与】聚焦持续走强的方向（跨周期出现标的），做分歧转一致。不碰仅5日新爆发的（可能一日游）。"
            elif decel_n > accel_n and today_limit <= 1:
                youzi_verdict = "减速信号增多+涨停减少，游资应收缩"
                youzi_action = "【收缩/休息】减少新开仓位，有利润的先锁。等待新的加速信号出现再积极参与。"
            else:
                youzi_verdict = "信号不明确，游资应谨慎"
                youzi_action = "【轻仓套利】小仓位打首板套利，不接力。做错立刻走。"
    else:
        if today_limit >= 3 and accel_n > decel_n:
            youzi_verdict = "涨停活跃+加速信号，游资积极参与阶段"
            youzi_action = "【积极参与】首板/一进二接力，聚焦加速方向。弱转强信号出现即试仓，做错隔夜摁掉。警惕高潮后的衰竭板。"
        elif accel_n > decel_n and persistent_n >= 3:
            youzi_verdict = "持续走强方向存在，游资可选择性参与"
            youzi_action = "【选择性参与】聚焦持续走强的方向（跨周期出现标的），做分歧转一致。不碰仅5日新爆发的（可能一日游）。"
        elif decel_n > accel_n and today_limit <= 1:
            youzi_verdict = "减速信号增多+涨停减少，游资应收缩"
            youzi_action = "【收缩/休息】减少新开仓位，有利润的先锁。等待新的加速信号出现再积极参与。"
        else:
            youzi_verdict = "信号不明确，游资应谨慎"
            youzi_action = "【轻仓套利】小仓位打首板套利，不接力。做错立刻走。"

    # === 40日慢牛视角（综合以上判断，额外参考40日长周期数据）===
    insight_40d = ""
    if only_40d_n >= 5 and d40_doubled + d40_big >= 3:
        insight_40d = f"🔥 {only_40d_n}只仅40日Top30的慢牛股（40日内翻倍/大涨{d40_doubled + d40_big}只）——这些票在20日窗口被忽略但长期趋势强劲。关注其40日路径：匀速推进型可低吸，前重后轻型等调整后再看。"
    elif only_40d_n >= 3:
        insight_40d = f"🟡 {only_40d_n}只仅40日Top30标的，短期不突出但长期有累积涨幅。需逐个看40日路径判断是否值得关注。"
    elif top_ret_40d > top_ret_20d * 1.5:
        insight_40d = f"📊 40日首涨幅({top_ret_40d:.0f}%)远超20日({top_ret_20d:.0f}%)——存在慢牛股在20日窗口外。拉长周期看赚钱效应比短期看到的更强。"

    # === 散户视角 ===
    if top_ret_20d > 100:
        sanhu_verdict = "已有翻倍股，追高风险极大"
        sanhu_action = "【不追高】20日涨幅超100%的标的绝不追。若已持有且盈利→分批止盈。若空仓→等大幅回调至均线附近再看。不要被FOMO驱动。"
    elif top_ret_20d > 50:
        sanhu_verdict = "赚钱效应偏强但未过热，可谨慎参与"
        sanhu_action = "【回调低吸】只在回调到关键均线附近时买入，不追涨。严格止损（-5%无条件走）。只用一种框架（短线or趋势），不要混。"
    elif today_up >= 20:
        sanhu_verdict = "今日普涨，短期追涨风险中等"
        sanhu_action = "【等分歧】今日普涨后明日大概率分歧。等分歧后的方向确认再考虑。不盘中追直线拉升。"
    else:
        sanhu_verdict = "赚钱效应偏弱，散户应空仓等待"
        sanhu_action = "【空仓等待】弱市下散户最容易亏钱。不抄底、不埋伏、不等反弹。等赚钱效应明确转强再进场。"

    result = {
        "机构": {"评估": inst_verdict, "策略": inst_action},
        "游资": {"评估": youzi_verdict, "策略": youzi_action},
        "散户": {"评估": sanhu_verdict, "策略": sanhu_action},
    }

    # 如果有40日慢牛发现，加入额外维度
    if insight_40d:
        result["40日慢牛发现"] = insight_40d

    return result


def compute_period_rankings(stocks, qt_data, kline_returns=None):
    """计算 5日/10日/20日/40日 区间涨幅排名 + 诊断框架，输出各周期 Top 30

    40日窗口用于发现"慢牛股"——这些票在20日窗口排名可能不突出，
    但拉长到40日就很明显（如利通电子型：40日翻倍但20日仅30%+）。

    Args:
        stocks: 股票列表
        qt_data: 腾讯 qt 实时行情（用于 today_chg 等实时字段）
        kline_returns: 通过 K 线 API 计算的准确周期涨幅（优先使用）
    """
    print(f"\n[STOCK] Step 5: 计算多周期区间涨幅排名 + 诊断...")

    # 优先使用 K 线计算的准确涨幅，回退到 qt API
    use_kline = kline_returns is not None and len(kline_returns) > 0
    if use_kline:
        print(f"  数据源: K线计算（{sum(1 for v in kline_returns.values() if v['valid'])} 只有效）")
    else:
        print(f"  数据源: 腾讯 qt API（可能有偏差）")

    rankings = {"1d": [], "3d": [], "5d": [], "10d": [], "20d": [], "40d": []}

    for s in stocks:
        code = s["code"]
        qt = qt_data.get(code, {})
        if not qt:
            continue

        name = s.get("name", qt.get("name", "?"))
        sector = s.get("sector", "")
        theme = s.get("theme", "")
        cap_yi = s.get("circ_mcap_yi", 0)
        today_chg = qt.get("change_pct", 0)

        # 确定使用的周期涨幅
        if use_kline and code in kline_returns:
            kr = kline_returns[code]
            # 只有 valid 的才用K线数据，否则回退到 qt
            if kr.get("valid"):
                ret_1d = kr.get("ret_1d", 0)
                ret_3d = kr.get("ret_3d", 0)
                ret_5d = kr["ret_5d"]
                ret_10d = kr["ret_10d"]
                ret_20d = kr["ret_20d"]
                # 40日涨幅仅来自K线数据（qt API 不提供），部分股票可能没满45日
                ret_40d = kr.get("ret_40d", 0) if kr.get("has_40d") else 0
            else:
                ret_1d = qt.get("change_pct", 0)  # 1日 = 今日涨跌幅
                ret_3d = 0
                ret_5d = qt.get("ret_5d", 0)
                ret_10d = qt.get("ret_10d", 0)
                ret_20d = qt.get("ret_20d", 0)
                ret_40d = 0
        else:
            ret_1d = qt.get("change_pct", 0)
            ret_3d = 0
            ret_5d = qt.get("ret_5d", 0)
            ret_10d = qt.get("ret_10d", 0)
            ret_20d = qt.get("ret_20d", 0)
            ret_40d = 0

        # 过滤掉涨跌幅异常的
        for period, ret in [("1d", ret_1d), ("3d", ret_3d), ("5d", ret_5d), ("10d", ret_10d), ("20d", ret_20d), ("40d", ret_40d)]:
            if abs(ret) > 500:  # 超过500%视为异常
                continue
            rankings[period].append({
                "code": code,
                "name": name,
                "sector": sector,
                "theme": theme,
                "cap_yi": cap_yi,
                "ret": round(ret, 2),
                "today_chg": round(today_chg, 2),
            })

    # 各周期排序，取 Top 30
    result = {}
    for period in ["1d", "3d", "5d", "10d", "20d", "40d"]:
        sorted_list = sorted(rankings[period], key=lambda x: x["ret"], reverse=True)
        top30 = sorted_list[:30]
        result[period] = top30

        # 打印摘要
        top5_names = ", ".join(f"{r['name']}({r['ret']:+.1f}%)" for r in top30[:5])
        print(f"  {period} Top 5: {top5_names} ... （共 {len(sorted_list)} 只有效数据）")

    # === 诊断计算 ===
    print(f"\n  [诊断] 计算聚合指标...")
    diag = {}
    for period in ["1d", "3d", "5d", "10d", "20d", "40d"]:
        diag[period] = _compute_single_period_diag(result.get(period, []))
        p = diag[period]
        print(f"  {period}: 首涨幅={p.get('top_return',0)}%  "
              f"翻倍={p.get('翻倍(>100%)',0)}只  大涨={p.get('大涨(50-100%)',0)}只  "
              f"涨停={p.get('today_distribution',{}).get('涨停(>9.5%)',0)}只  "
              f"大票={p.get('cap_distribution',{}).get('大票(>500亿)',0)}只")

    print(f"\n  [诊断] 跨周期分析...")
    cross = _compute_cross_period(result)
    print(f"  四周期持续: {len(cross['persistent_4periods'])}只  三周期持续: {len(cross['persistent_3periods'])}只  加速: {cross['accel_count']}只  减速: {cross['decel_count']}只")
    print(f"  仅5日新爆: {len(cross['only_5d'])}只  仅20日: {len(cross['only_20d'])}只  仅40日慢牛: {len(cross['only_40d'])}只")

    print(f"\n  [诊断] 涨幅路径分解...")
    # 先运行简单阶段检测（不依赖 period_returns）供轨迹分析使用
    phase_basic = _detect_market_phase()
    traj = _compute_trajectory_analysis(result, kline_returns, phase_basic)
    print(f"  分析 {traj['total_analyzed']} 只标的，路径分布: {traj['summary']}")
    for insight in traj.get("insights", []):
        print(f"  {insight}")

    print(f"\n  [诊断] 交易者策略启示...")
    implications = _derive_trader_implications(diag, cross, traj)
    for trader_type, impl in implications.items():
        if isinstance(impl, dict):
            print(f"  {trader_type}: {impl['评估'][:60]}...")
        else:
            print(f"  {trader_type}: {impl[:80]}...")

    # 赚钱效应自动评级（综合考虑涨幅+路径）
    top_ret_20d = diag.get("20d", {}).get("top_return", 0)
    front_loaded_count = traj.get("type_distribution", {}).get("前重后轻 📉", {}).get("count", 0)
    reversal_count = traj.get("type_distribution", {}).get("高位逆转 🔴", {}).get("count", 0)

    # 基础评级
    if top_ret_20d > 200:
        base_level = "极强"
    elif top_ret_20d > 100:
        base_level = "强"
    elif top_ret_20d > 50:
        base_level = "偏强"
    elif top_ret_20d > 20:
        base_level = "中等"
    elif top_ret_20d > 10:
        base_level = "偏弱"
    else:
        base_level = "弱"

    # 路径修正：如果翻倍股中很多是前重后轻或高位逆转，降一级
    if base_level in ("极强", "强") and (front_loaded_count > 8 or reversal_count > 3):
        downgrade = {"极强": "强 ⚠️(高位滞涨)", "强": "偏强 ⚠️(高位滞涨)"}
        profit_level = downgrade.get(base_level, base_level)
    else:
        emoji_map = {"极强": "🔥🔥🔥", "强": "🔥🔥", "偏强": "🔥", "中等": "🟡", "偏弱": "🔴", "弱": "🔴🔴"}
        profit_level = f"{base_level} {emoji_map.get(base_level, '')}"

    # 市场阶段检测（含 period_returns 增强信号）
    # 先构建临时 output 用于传给 _detect_market_phase 做增强
    temp_output = {
        "diagnostics": diag,
        "cross_period": cross,
        "trajectory_analysis": traj,
        "rankings": result,
    }
    market_phase_enhanced = _detect_market_phase(period_returns=temp_output)

    # 保存到文件
    output = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "auto_rating": profit_level,
        "market_phase": market_phase_enhanced,
        "diagnostics": diag,
        "cross_period": cross,
        "trajectory_analysis": traj,
        "trader_implications": implications,
        "rankings": result,
    }
    save_json(OUTPUT_PERIOD_FILE, output)
    print(f"\n  [OK] 多周期涨幅排名+诊断已保存 → {OUTPUT_PERIOD_FILE}")
    print(f"  [OK] 自动评级: {profit_level}")

    return result


# ============================================================
# 工具
# ============================================================

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ============================================================
# 主流程
# ============================================================

def main():
    print("=" * 60)
    print("[Stock Pool] A股 Wiki 选股池生成")
    print(f"   运行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   最低流通市值：{MIN_CAP_YI}亿")
    print("=" * 60)

    # Step 1: 拉取股票列表
    stocks = fetch_stock_universe()
    if not stocks:
        print("[ERROR] 未获取到股票数据")
        return 1

    # Step 2: 批量获取实时行情
    qt_data = fetch_recent_klines(stocks)

    # Step 3: Wiki 框架打分（先检测市场阶段 + 前视镜身份，用于打分）
    market_phase = _detect_market_phase()
    print(f"\n  [阶段检测] {market_phase['phase']} (得分{market_phase['score']}, 置信度{market_phase['confidence']})")
    identities = _compute_market_identity(stocks, qt_data)
    leader_count = sum(1 for v in identities.values() if v.get("is_industry_leader"))
    limit_up_count = sum(1 for v in identities.values() if v.get("is_limit_up"))
    print(f"  [身份识别] 行业龙头{leader_count}只 涨停{limit_up_count}只")
    scored = compute_wiki_scores(stocks, qt_data, market_phase, identities)

    # Step 4: 构建选股池
    pool = build_stock_pool(scored, market_phase)

    # Step 4.5: 通过 K 线 API 获取准确的周期涨跌幅
    kline_returns = fetch_period_returns_via_klines(stocks)

    # Step 5: 多周期区间涨幅排名 + 诊断
    period_rankings = compute_period_rankings(stocks, qt_data, kline_returns)

    print(f"\n[OK] 完成！选股池包含 {len(pool['observe'])} 只观察股 + {len(pool['trade'])} 只交易股")
    print(f"     多周期涨幅排名+诊断框架已更新（5/10/20/40日 Top 30 + 聚合诊断 + 交易者启示）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
