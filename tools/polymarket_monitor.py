# coding=utf-8
"""
Polymarket BTC 5分钟赔率监控器

功能：
1. 自动发现当前/下一个 BTC 5分钟市场
2. 每隔几秒刷新赔率
3. 当 Yes 或 No 价格 < 阈值时推送企业微信告警
4. 所有数据记录到 CSV 供后续分析

使用方法：
  python polymarket_monitor.py

环境变量：
  WEWORK_WEBHOOK_URL - 企业微信机器人 webhook（可选，不填就只记录不推送）

依赖：
  pip install requests
"""

import csv
import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

# ============================================
# 配置区
# ============================================

# 企业微信 webhook（留空则只记录不推送）
WEWORK_WEBHOOK_URL = os.environ.get("WEWORK_WEBHOOK_URL", "")

# 监控参数
CHECK_INTERVAL = 5          # 每隔多少秒检查一次赔率
BUY_THRESHOLD = 0.40        # 低于此价格触发告警（0.40 = 40¢）
SELL_THRESHOLD = 0.45       # 高于此价格提示可卖出
ALERT_COOLDOWN = 60         # 同一个市场告警冷却时间（秒），避免刷屏

# Polymarket API
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# 数据记录
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# ============================================
# API 封装
# ============================================


def fetch_btc_5min_events(limit: int = 10) -> List[Dict]:
    """
    获取 BTC 5分钟市场列表
    返回活跃的 BTC up/down 5min 事件
    """
    btc_events = []

    # 方法1: 用 slug_contains 直接搜索（不过滤 closed，因为 BTC 5分钟事件 closed=true 但子市场仍活跃）
    slug_keywords = ["btc-updown-5m", "bitcoin-up-or-down-5-minutes"]
    for slug_kw in slug_keywords:
        try:
            url = f"{GAMMA_API}/events"
            params = {
                "limit": limit,
                "active": "true",
                "slug_contains": slug_kw,
            }
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            events = resp.json()
            if events:
                btc_events.extend(events)
                break  # 找到了就不用试下一个关键词
        except Exception as e:
            print(f"[调试] slug '{slug_kw}' 搜索失败: {e}")
            continue

    # 方法2: 如果方法1没找到，用通用搜索 + 过滤
    if not btc_events:
        try:
            url = f"{GAMMA_API}/events"
            params = {
                "limit": 50,
                "active": "true",
                "closed": "false",
            }
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            events = resp.json()

            for event in events:
                title = event.get("title", "").lower()
                slug = event.get("slug", "").lower()
                if ("btc" in title or "bitcoin" in title) and ("5 min" in title or "5min" in title):
                    btc_events.append(event)
                elif "btc-updown-5m" in slug or "btc" in slug and "5m" in slug:
                    btc_events.append(event)
        except Exception as e:
            print(f"[错误] 获取事件列表失败: {e}")

    return btc_events


def fetch_market_prices(token_id: str) -> Optional[Dict]:
    """
    获取指定 token 的当前价格（order book 最优价）
    """
    try:
        url = f"{CLOB_API}/price"
        params = {
            "token_id": token_id,
            "side": "buy",
        }
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[错误] 获取价格失败 (token={token_id[:20]}...): {e}")
        return None


def fetch_orderbook(token_id: str) -> Optional[Dict]:
    """
    获取 order book 数据
    """
    try:
        url = f"{CLOB_API}/book"
        params = {"token_id": token_id}
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[错误] 获取订单簿失败: {e}")
        return None


def fetch_market_by_condition(condition_id: str) -> Optional[Dict]:
    """
    通过 condition_id 获取市场详情
    """
    try:
        url = f"{GAMMA_API}/markets"
        params = {"condition_id": condition_id}
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        markets = resp.json()
        return markets[0] if markets else None
    except Exception as e:
        print(f"[错误] 获取市场详情失败: {e}")
        return None


def get_midpoint_prices(token_id: str) -> Optional[float]:
    """
    获取 midpoint 价格（买卖中间价）
    """
    try:
        url = f"{CLOB_API}/midpoint"
        params = {"token_id": token_id}
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return float(data.get("mid", 0))
    except Exception as e:
        print(f"[错误] 获取中间价失败: {e}")
        return None


# ============================================
# 企业微信推送
# ============================================


def send_wework_alert(title: str, content: str):
    """发送企业微信告警"""
    if not WEWORK_WEBHOOK_URL:
        return

    try:
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": f"## {title}\n{content}"
            }
        }
        resp = requests.post(WEWORK_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code == 200:
            print(f"[推送] 企业微信告警已发送")
        else:
            print(f"[推送] 发送失败: {resp.status_code}")
    except Exception as e:
        print(f"[推送] 发送异常: {e}")


# ============================================
# 数据记录
# ============================================


def record_price(timestamp: str, event_title: str, yes_price: float,
                 no_price: float, market_status: str, event_end_time: str = ""):
    """记录价格到 CSV"""
    csv_file = DATA_DIR / f"polymarket_btc5m_{datetime.now().strftime('%Y%m%d')}.csv"
    file_exists = csv_file.exists()

    with open(csv_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "timestamp", "event_title", "yes_price", "no_price",
                "spread", "market_status", "event_end_time"
            ])
        writer.writerow([
            timestamp, event_title, f"{yes_price:.4f}", f"{no_price:.4f}",
            f"{abs(yes_price - no_price):.4f}", market_status, event_end_time
        ])


def record_result(timestamp: str, event_title: str, result: str,
                  entry_price: float, exit_price: float):
    """记录结算结果"""
    csv_file = DATA_DIR / "polymarket_results.csv"
    file_exists = csv_file.exists()

    with open(csv_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "timestamp", "event_title", "result",
                "entry_price", "exit_price", "pnl"
            ])
        pnl = exit_price - entry_price if result == "win" else -entry_price
        writer.writerow([
            timestamp, event_title, result,
            f"{entry_price:.4f}", f"{exit_price:.4f}", f"{pnl:.4f}"
        ])


# ============================================
# 主监控逻辑
# ============================================


class BTCMonitor:
    """BTC 5分钟市场监控器"""

    def __init__(self):
        self.last_alert_time: Dict[str, float] = {}  # 告警冷却
        self.tracked_markets: Dict[str, Dict] = {}   # 正在追踪的市场

    def should_alert(self, market_id: str) -> bool:
        """检查是否应该发送告警（冷却机制）"""
        now = time.time()
        last = self.last_alert_time.get(market_id, 0)
        if now - last > ALERT_COOLDOWN:
            self.last_alert_time[market_id] = now
            return True
        return False

    def check_and_alert(self, event_title: str, yes_price: float,
                        no_price: float, market_id: str):
        """检查价格并触发告警"""
        alerts = []

        if yes_price > 0 and yes_price < BUY_THRESHOLD:
            alerts.append(f"**Yes 价格偏低**: {yes_price:.2f}¢ (阈值 {BUY_THRESHOLD:.2f})")

        if no_price > 0 and no_price < BUY_THRESHOLD:
            alerts.append(f"**No 价格偏低**: {no_price:.2f}¢ (阈值 {BUY_THRESHOLD:.2f})")

        if alerts and self.should_alert(market_id):
            content = f"**市场**: {event_title}\n"
            content += f"**Yes**: {yes_price:.4f} | **No**: {no_price:.4f}\n"
            content += "\n".join(alerts)
            content += f"\n**时间**: {datetime.now().strftime('%H:%M:%S')}"

            print(f"\n{'='*50}")
            print(f"🚨 告警触发!")
            print(content)
            print(f"{'='*50}\n")

            send_wework_alert("⚡ BTC 5分钟赔率偏离", content)

    def run(self):
        """主循环"""
        print("=" * 60)
        print("  Polymarket BTC 5分钟赔率监控器")
        print("=" * 60)
        print(f"  检查间隔: {CHECK_INTERVAL} 秒")
        print(f"  买入阈值: < {BUY_THRESHOLD} ({BUY_THRESHOLD*100:.0f}¢)")
        print(f"  卖出提示: > {SELL_THRESHOLD} ({SELL_THRESHOLD*100:.0f}¢)")
        print(f"  企业微信: {'已配置 ✅' if WEWORK_WEBHOOK_URL else '未配置 ❌（只记录不推送）'}")
        print(f"  数据目录: {DATA_DIR.absolute()}")
        print("=" * 60)
        print("\n开始监控...\n")

        consecutive_errors = 0

        while True:
            try:
                # 1. 获取活跃的 BTC 5分钟事件
                events = fetch_btc_5min_events(limit=50)

                if not events:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 未找到活跃的 BTC 5分钟市场，等待重试...")
                    time.sleep(CHECK_INTERVAL * 3)
                    continue

                consecutive_errors = 0

                # 2. 遍历每个事件，检查价格
                for event in events:
                    event_title = event.get("title", "Unknown")
                    markets = event.get("markets", [])

                    for market in markets:
                        # 获取 token IDs (Yes 和 No)
                        tokens = market.get("clobTokenIds", [])
                        outcomes = market.get("outcomes", [])
                        outcome_prices = market.get("outcomePrices", [])

                        if not outcome_prices or len(outcome_prices) < 2:
                            continue

                        try:
                            yes_price = float(outcome_prices[0]) if outcome_prices[0] else 0
                            no_price = float(outcome_prices[1]) if outcome_prices[1] else 0
                        except (ValueError, IndexError):
                            continue

                        if yes_price == 0 and no_price == 0:
                            continue

                        market_id = market.get("id", market.get("conditionId", "unknown"))
                        market_status = "active" if market.get("active") else "closed"
                        end_date = market.get("endDate", "")

                        # 记录数据
                        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        record_price(now_str, event_title, yes_price, no_price,
                                    market_status, end_date)

                        # 打印实时状态
                        spread = abs(yes_price - no_price)
                        bias = "→" if spread < 0.05 else ("↑Yes" if yes_price > no_price else "↓No")
                        print(
                            f"[{datetime.now().strftime('%H:%M:%S')}] "
                            f"{event_title[:40]:40s} | "
                            f"Yes: {yes_price:.3f} | No: {no_price:.3f} | "
                            f"偏差: {spread:.3f} {bias}"
                        )

                        # 检查告警
                        self.check_and_alert(event_title, yes_price, no_price, market_id)

                time.sleep(CHECK_INTERVAL)

            except KeyboardInterrupt:
                print("\n\n监控已停止。")
                print(f"数据已保存到: {DATA_DIR.absolute()}")
                break
            except Exception as e:
                consecutive_errors += 1
                print(f"[错误] {e}")
                if consecutive_errors > 10:
                    print("[错误] 连续错误过多，等待 60 秒...")
                    time.sleep(60)
                else:
                    time.sleep(CHECK_INTERVAL)


# ============================================
# 数据分析工具（跑完数据后用）
# ============================================


def analyze_data():
    """分析历史数据，验证策略可行性"""
    import glob

    csv_files = sorted(glob.glob(str(DATA_DIR / "polymarket_btc5m_*.csv")))
    if not csv_files:
        print("没有找到数据文件，请先运行监控器采集数据。")
        return

    total_records = 0
    below_threshold_count = 0
    opportunities = []

    for csv_file in csv_files:
        with open(csv_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                total_records += 1
                yes_price = float(row["yes_price"])
                no_price = float(row["no_price"])

                if yes_price < BUY_THRESHOLD or no_price < BUY_THRESHOLD:
                    below_threshold_count += 1
                    opportunities.append({
                        "time": row["timestamp"],
                        "event": row["event_title"],
                        "yes": yes_price,
                        "no": no_price,
                        "cheap_side": "Yes" if yes_price < no_price else "No",
                        "cheap_price": min(yes_price, no_price),
                    })

    print("=" * 60)
    print("  数据分析报告")
    print("=" * 60)
    print(f"  总记录数: {total_records}")
    print(f"  低于 {BUY_THRESHOLD*100:.0f}¢ 的机会: {below_threshold_count} 次")
    print(f"  出现频率: {below_threshold_count/max(total_records,1)*100:.1f}%")
    print(f"  数据文件: {len(csv_files)} 个")
    print("=" * 60)

    if opportunities:
        print(f"\n最近 10 次机会:")
        print("-" * 60)
        for opp in opportunities[-10:]:
            print(
                f"  {opp['time']} | {opp['event'][:30]:30s} | "
                f"{opp['cheap_side']}: {opp['cheap_price']:.3f}"
            )
    else:
        print("\n暂未发现低于阈值的机会，继续采集数据...")


# ============================================
# 快速测试（验证 API 是否可通）
# ============================================


def quick_test():
    """快速测试 API 连通性"""
    print("=" * 60)
    print("  API 连通性测试")
    print("=" * 60)

    # 测试 1: Gamma API
    print("\n[测试1] 访问 Gamma API...")
    try:
        url = f"{GAMMA_API}/events?limit=3&active=true&closed=false"
        print(f"  请求: {url}")
        resp = requests.get(url, timeout=15)
        print(f"  状态码: {resp.status_code}")
        data = resp.json()
        print(f"  返回事件数: {len(data)}")
        for event in data[:3]:
            print(f"    - {event.get('title', 'N/A')[:60]}")
            slug = event.get("slug", "")
            print(f"      slug: {slug}")
    except Exception as e:
        print(f"  ❌ 失败: {e}")

    # 测试 2: 搜索 BTC 相关
    print("\n[测试2] 搜索 BTC 相关市场...")
    try:
        url = f"{GAMMA_API}/events?limit=20&active=true&closed=false"
        print(f"  请求: {url}")
        resp = requests.get(url, timeout=15)
        data = resp.json()
        print(f"  总事件数: {len(data)}")

        btc_found = []
        for event in data:
            title = event.get("title", "").lower()
            slug = event.get("slug", "").lower()
            if "btc" in title or "bitcoin" in title or "btc" in slug:
                btc_found.append(event)

        if btc_found:
            print(f"  ✅ 找到 {len(btc_found)} 个 BTC 相关事件:")
            for event in btc_found[:5]:
                print(f"    - {event.get('title', 'N/A')}")
                markets = event.get("markets", [])
                print(f"      markets 数量: {len(markets)}")
                for m in markets[:2]:
                    prices = m.get("outcomePrices", [])
                    outcomes = m.get("outcomes", [])
                    print(f"      outcomes: {outcomes}, prices: {prices}")
        else:
            print("  ⚠️ 未找到 BTC 相关事件")
            print("  尝试打印所有事件标题:")
            for event in data:
                print(f"    - {event.get('title', 'N/A')[:60]}")
    except Exception as e:
        print(f"  ❌ 失败: {e}")

    # 测试 3: 直接用 slug 搜索
    print("\n[测试3] 用 slug 直接搜索 BTC 5分钟...")
    try:
        # 尝试几种可能的 slug 格式
        slugs_to_try = [
            "btc-updown-5m",
            "bitcoin-up-or-down-5-minutes",
        ]
        for slug in slugs_to_try:
            url = f"{GAMMA_API}/events?slug_contains={slug}&limit=5"
            resp = requests.get(url, timeout=15)
            data = resp.json()
            if data:
                print(f"  ✅ slug '{slug}' 找到 {len(data)} 个事件")
                for event in data[:3]:
                    print(f"    - {event.get('title', 'N/A')}")
            else:
                print(f"  ❌ slug '{slug}' 无结果")
    except Exception as e:
        print(f"  ❌ 失败: {e}")

    # 测试 4: CLOB API
    print("\n[测试4] 访问 CLOB API...")
    try:
        url = f"{CLOB_API}/time"
        print(f"  请求: {url}")
        resp = requests.get(url, timeout=15)
        print(f"  状态码: {resp.status_code}")
        print(f"  响应: {resp.text[:200]}")
    except Exception as e:
        print(f"  ❌ 失败: {e}")

    print("\n" + "=" * 60)
    print("  测试完成！把上面的输出截图发给我")
    print("=" * 60)


# ============================================
# 入口
# ============================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "analyze":
        analyze_data()
    elif len(sys.argv) > 1 and sys.argv[1] == "test":
        quick_test()
    elif len(sys.argv) > 1 and sys.argv[1] == "monitor":
        monitor = BTCMonitor()
        monitor.run()
    else:
        # 默认先跑测试
        print("提示: 首次运行，先执行 API 测试...\n")
        quick_test()
        print("\n\n如果测试通过，再运行:")
        print("  python polymarket_monitor.py monitor")
        print("\n按任意键退出...")
        input()

