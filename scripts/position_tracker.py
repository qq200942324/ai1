#!/usr/bin/env python3
"""
持仓跟踪模块（持有期管理）
========================
维护 positions.json，记录每只持仓的入场时间、价格、浮盈、分歧阶段变化。

CLI 接口：
  python scripts/position_tracker.py add <code> <name> <price> <entry_date> [--divergence PENDING]
  python scripts/position_tracker.py remove <code>
  python scripts/position_tracker.py update <code> --price 12.34 [--divergence VERIFIED]
  python scripts/position_tracker.py status              # 查看全部持仓
  python scripts/position_tracker.py status <code>       # 查看单只持仓
  python scripts/position_tracker.py cross-ref           # 生成 stock_pool 交叉引用

与 screen_stocks 集成：
  - stock_pool.json 顶层新增 position_cross_ref 数组
  - meta 新增 positions_file 路径
"""

import json
import os
import sys
from datetime import datetime

# 修复 Windows GBK 终端编码问题
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

VAULT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(VAULT_ROOT, "2-wiki", "data")
POSITIONS_FILE = os.path.join(DATA_DIR, "positions.json")

VALID_DIVERGENCES = ["PENDING", "VERIFIED", "FAILED", "NEW"]


def _load():
    """加载持仓文件，不存在则返回默认结构"""
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # 确保基础结构完整
                if "positions" not in data:
                    data["positions"] = []
                if "meta" not in data:
                    data["meta"] = {"updated": datetime.now().isoformat()}
                return data
        except (json.JSONDecodeError, IOError):
            pass
    return {"meta": {"updated": datetime.now().isoformat()}, "positions": []}


def _save(data):
    """保存持仓到文件"""
    data["meta"]["updated"] = datetime.now().isoformat()
    with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _find(data, code):
    """查找持仓，返回 (index, position) 或 (None, None)"""
    for i, pos in enumerate(data["positions"]):
        if pos["code"] == code:
            return i, pos
    return None, None


def cmd_add(args):
    """add <code> <name> <price> <entry_date> [--divergence PENDING]"""
    code = args[0]
    name = args[1]
    price = float(args[2])
    entry_date = args[3]  # YYYY-MM-DD
    divergence = "PENDING"  # 默认

    # 解析可选参数
    extra = args[4:]
    for i, a in enumerate(extra):
        if a == "--divergence" and i + 1 < len(extra):
            div = extra[i + 1]
            if div not in VALID_DIVERGENCES:
                print(f"[ERROR] 无效分歧阶段: {div}，可选: {', '.join(VALID_DIVERGENCES)}")
                return 1
            divergence = div

    data = _load()
    idx, existing = _find(data, code)
    if existing:
        print(f"[WARN] {code} {name} 已在持仓中，用 update 命令更新")
        return 1

    pos = {
        "code": code,
        "name": name,
        "entry_date": entry_date,
        "entry_price": price,
        "current_price": price,
        "days_held": 0,
        "floating_pnl_pct": 0.0,
        "divergence_stage": divergence,
        "divergence_history": [{"date": entry_date, "stage": divergence}],
        "updated": datetime.now().isoformat(),
    }
    data["positions"].append(pos)
    _save(data)
    print(f"[OK] 已添加 {code} {name} 入场价{price} 分歧阶段{divergence}")
    return 0


def cmd_remove(args):
    """remove <code>"""
    code = args[0]
    data = _load()
    idx, pos = _find(data, code)
    if pos is None:
        print(f"[WARN] 未找到 {code}")
        return 1
    removed = data["positions"].pop(idx)
    _save(data)
    print(f"[OK] 已移除 {code} {removed['name']}（入场{removed['entry_date']} 入场价{removed['entry_price']}）")
    return 0


def cmd_update(args):
    """update <code> [--price X] [--divergence STAGE] [--name NAME]"""
    code = args[0]
    data = _load()
    idx, pos = _find(data, code)
    if pos is None:
        print(f"[WARN] 未找到 {code}")
        return 1

    updates = {}
    extra = args[1:]
    i = 0
    while i < len(extra):
        if extra[i] == "--price" and i + 1 < len(extra):
            new_price = float(extra[i + 1])
            pos["current_price"] = new_price
            if pos["entry_price"] > 0:
                pos["floating_pnl_pct"] = round(
                    (new_price / pos["entry_price"] - 1) * 100, 2
                )
            # 计算持仓天数
            try:
                entry_dt = datetime.strptime(pos["entry_date"], "%Y-%m-%d")
                pos["days_held"] = (datetime.now() - entry_dt).days
            except ValueError:
                pass
            updates["price"] = new_price
            i += 2
        elif extra[i] == "--divergence" and i + 1 < len(extra):
            new_div = extra[i + 1]
            if new_div not in VALID_DIVERGENCES:
                print(f"[ERROR] 无效分歧阶段: {new_div}，可选: {', '.join(VALID_DIVERGENCES)}")
                return 1
            old_div = pos.get("divergence_stage", "")
            if new_div != old_div:
                pos["divergence_stage"] = new_div
                pos.setdefault("divergence_history", []).append({
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "stage": new_div,
                    "from": old_div,
                })
                print(f"  分歧阶段变化: {old_div} → {new_div}")
            updates["divergence"] = new_div
            i += 2
        elif extra[i] == "--name" and i + 1 < len(extra):
            pos["name"] = extra[i + 1]
            updates["name"] = extra[i + 1]
            i += 2
        else:
            print(f"[WARN] 未知参数: {extra[i]}")
            i += 1

    pos["updated"] = datetime.now().isoformat()
    data["positions"][idx] = pos
    _save(data)
    parts = [f"{k}={v}" for k, v in updates.items()]
    print(f"[OK] 已更新 {code} {pos['name']}（{', '.join(parts)}）")
    return 0


def cmd_status(args=None):
    """status [code] — 查看全部或单只持仓"""
    data = _load()
    positions = data["positions"]

    if args and len(args) > 0:
        # 查看单只
        code = args[0]
        _, pos = _find(data, code)
        if pos is None:
            print(f"[INFO] 未找到 {code} 的持仓记录")
            return 0
        _print_position(pos, detail=True)
    else:
        # 查看全部
        if not positions:
            print("[INFO] 当前无持仓记录")
            return 0

        print(f"\n持仓概览（{len(positions)} 只）")
        print(f"{'='*80}")
        print(f"{'代码':8s} {'名称':8s} {'入场日':10s} {'入场价':>8s} {'现价':>8s} "
              f"{'浮盈':>8s} {'天数':>5s} {'分歧':>10s}")
        print(f"{'-'*80}")
        for pos in positions:
            pnl = pos.get("floating_pnl_pct", 0)
            pnl_str = f"{pnl:+.1f}%"
            div = pos.get("divergence_stage", "?")
            div_emoji = {"VERIFIED": "🟢", "PENDING": "🟡", "FAILED": "🔴", "NEW": "🆕"}.get(div, "")
            print(f"{pos['code']:8s} {pos['name']:8s} {pos['entry_date']:10s} "
                  f"{pos['entry_price']:>8.2f} {pos.get('current_price', 0):>8.2f} "
                  f"{pnl_str:>8s} {pos.get('days_held', 0):>5d} {div_emoji} {div:>6s}")

        # 汇总
        total = len(positions)
        verified = sum(1 for p in positions if p.get("divergence_stage") == "VERIFIED")
        pending = sum(1 for p in positions if p.get("divergence_stage") == "PENDING")
        failed = sum(1 for p in positions if p.get("divergence_stage") == "FAILED")
        print(f"{'-'*80}")
        print(f"🟢 已验证 {verified} | 🟡 待验 {pending} | 🔴 失败 {failed} | 合计 {total}")

    print(f"\n  数据文件: {POSITIONS_FILE}")
    return 0


def _print_position(pos, detail=False):
    """打印单只持仓详情"""
    pnl = pos.get("floating_pnl_pct", 0)
    div = pos.get("divergence_stage", "?")
    print(f"\n{'='*50}")
    print(f"  {pos['code']} {pos['name']}")
    print(f"{'='*50}")
    print(f"  入场日期: {pos['entry_date']}")
    print(f"  入场价格: {pos['entry_price']:.2f}")
    print(f"  当前价格: {pos.get('current_price', 0):.2f}")
    print(f"  浮动盈亏: {pnl:+.1f}%")
    print(f"  持仓天数: {pos.get('days_held', 0)} 天")
    print(f"  分歧阶段: {div}")
    if detail and pos.get("divergence_history"):
        print(f"\n  分歧历史:")
        for h in pos["divergence_history"]:
            arrow = f" (← {h.get('from', '')})" if h.get("from") else ""
            print(f"    {h['date']} → {h['stage']}{arrow}")


def cmd_cross_ref():
    """生成 stock_pool.json 的交叉引用（供日报 LLM 使用）"""
    data = _load()
    positions = data["positions"]
    if not positions:
        print("[INFO] 无持仓，无需生成交叉引用")
        return 0

    # 尝试读取 stock_pool.json
    pool_path = os.path.join(DATA_DIR, "stock_pool.json")
    pool = {}
    if os.path.exists(pool_path):
        try:
            with open(pool_path, "r", encoding="utf-8") as f:
                pool = json.load(f)
        except Exception:
            pass

    cross_ref = []
    all_stocks = []
    if pool:
        all_stocks = pool.get("observe", []) + pool.get("trade", [])

    for pos in positions:
        code = pos["code"]
        in_observe = any(s["code"] == code for s in pool.get("observe", []))
        in_trade = any(s["code"] == code for s in pool.get("trade", []))
        cross_ref.append({
            "code": code,
            "name": pos["name"],
            "entry_date": pos["entry_date"],
            "floating_pnl_pct": pos.get("floating_pnl_pct", 0),
            "days_held": pos.get("days_held", 0),
            "divergence_stage": pos.get("divergence_stage", "?"),
            "in_observe": in_observe,
            "in_trade": in_trade,
        })

    # 写入 stock_pool.json
    if pool:
        pool["position_cross_ref"] = cross_ref
        pool["meta"]["positions_file"] = "2-wiki/data/positions.json"
        with open(pool_path, "w", encoding="utf-8") as f:
            json.dump(pool, f, ensure_ascii=False, indent=2)
        print(f"[OK] 已将 {len(cross_ref)} 只持仓的交叉引用写入 stock_pool.json")
    else:
        print("[WARN] stock_pool.json 不存在，跳过交叉引用写入")

    # 打印交叉引用摘要
    print(f"\n持仓交叉引用（{len(cross_ref)} 只）:")
    for ref in cross_ref:
        tags = []
        if ref["in_observe"]:
            tags.append("观察")
        if ref["in_trade"]:
            tags.append("交易")
        tag_str = "+".join(tags) if tags else "未入库"
        pnl = ref["floating_pnl_pct"]
        print(f"  {ref['code']} {ref['name']:8s} | 浮盈{pnl:+.1f}% | {ref['days_held']}天 | {ref['divergence_stage']} | [{tag_str}]")

    return 0


def print_usage():
    print(__doc__)


def main():
    if len(sys.argv) < 2:
        print_usage()
        return 0

    cmd = sys.argv[1]
    args = sys.argv[2:]

    commands = {
        "add": cmd_add,
        "remove": cmd_remove,
        "update": cmd_update,
        "status": cmd_status,
        "cross-ref": cmd_cross_ref,
    }

    if cmd not in commands:
        print(f"[ERROR] 未知命令: {cmd}")
        print(f"  可用: {', '.join(commands.keys())}")
        return 1

    try:
        return commands[cmd](args)
    except Exception as e:
        print(f"[ERROR] {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
