#!/usr/bin/env python3
"""
A股每日自动化数据管线
======================
串联 update_data.py -> screen_stocks.py，统一错误处理。

用法：python scripts/run_pipeline.py
退出码：
  0 — 全部成功
  1 — update_data 失败（致命：数据未刷新）
  2 — update_data 成功但 screen_stocks 失败（非致命：可用旧 stock_pool.json）
  3 — 两者均失败
"""

import json
import os
import sys
import subprocess
from datetime import datetime

# 修复 Windows GBK 终端编码问题
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# 路径（与现有脚本一致的约定）
VAULT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(VAULT_ROOT, "2-wiki", "data")
META_FILE = os.path.join(DATA_DIR, "meta.json")
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))


def run_step(name, script_name, timeout=600):
    """运行子脚本，返回 (success: bool, output: str)。"""
    script_path = os.path.join(SCRIPTS_DIR, script_name)
    t0 = datetime.now()
    print(f"\n{'='*60}")
    print(f"[Pipeline] Start: {script_name}")
    print(f"[Pipeline] Time: {t0.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")
    try:
        result = subprocess.run(
            [sys.executable, script_path],
            cwd=VAULT_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        elapsed = (datetime.now() - t0).total_seconds()
        output = result.stdout + "\n" + result.stderr
        if result.returncode == 0:
            print(f"[Pipeline] [OK] {script_name} completed ({elapsed:.0f}s)")
        else:
            print(f"[Pipeline] [FAIL] {script_name} exit_code={result.returncode} ({elapsed:.0f}s)")
        # 打印脚本输出的最后部分（避免刷屏）
        tail = output[-3000:] if len(output) > 3000 else output
        print(tail)
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        elapsed = (datetime.now() - t0).total_seconds()
        print(f"[Pipeline] [TIMEOUT] {script_name} >{timeout}s")
        return False, f"TIMEOUT after {elapsed:.0f}s"
    except Exception as e:
        print(f"[Pipeline] [ERROR] {script_name}: {e}")
        return False, str(e)


def update_meta_pipeline():
    """更新 meta.json，记录管线运行时间"""
    meta = {}
    if os.path.exists(META_FILE):
        try:
            with open(META_FILE, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            pass
    # 保留已有字段
    meta["last_pipeline_run"] = datetime.now().isoformat()
    # 确保不丢失已有字段
    for key in ["last_update", "last_analysis", "last_stock_pool"]:
        if key not in meta:
            meta[key] = None
    with open(META_FILE, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def main():
    print("=" * 60)
    print("[Pipeline] A-Share Daily Data Pipeline")
    print(f"[Pipeline] Start: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[Pipeline] Vault: {VAULT_ROOT}")
    print("=" * 60)

    ok_1, ok_2 = False, False

    # Step 1: 行情 + 新闻数据
    ok_1, _ = run_step("Market+News Data", "update_data.py")

    if not ok_1:
        print("\n[Pipeline] FATAL: Data update failed, stopping pipeline.")
        update_meta_pipeline()
        return 1

    # Step 2: 选股池
    ok_2, _ = run_step("Stock Screening", "screen_stocks.py")

    # 输出 meta.json 摘要
    print(f"\n{'='*60}")
    print("[Pipeline] Pipeline complete. meta.json status:")
    if os.path.exists(META_FILE):
        try:
            meta = json.load(open(META_FILE, "r", encoding="utf-8"))
            for key in ["last_update", "last_analysis", "last_stock_pool", "last_pipeline_run"]:
                val = meta.get(key, "N/A")
                if val:
                    print(f"  {key}: {val}")
        except Exception as e:
            print(f"  (read meta.json failed: {e})")

    update_meta_pipeline()

    if ok_1 and ok_2:
        print("\n[Pipeline] ALL OK (exit 0)")
        return 0
    elif ok_1 and not ok_2:
        print("\n[Pipeline] PARTIAL: data updated, stock screening failed (exit 2)")
        return 2
    else:
        print("\n[Pipeline] ALL FAILED (exit 3)")
        return 3


if __name__ == "__main__":
    sys.exit(main())
