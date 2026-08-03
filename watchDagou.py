#!/usr/bin/env python3
"""股票目标价监控工具 - 重构版"""

import http.client
import os
import select
import subprocess
import sys
import termios
import threading
import time
import tty
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


# ============================================================
# 常量
# ============================================================

TRADE_SESSIONS = [
    ((9, 30), (11, 30)),
    ((13, 0), (15, 0)),
]

ALERT_DURATION = 30
REFRESH_INTERVAL = 0.1

CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".config", "watchDagou")
CONFIG_HEADER = "symbol operation target"
CONFIG_DEFAULT = "sh000001"

RED = "\033[31m"
GREEN = "\033[32m"
WHITE = "\033[37m"
GOLD = "\033[1;33m"
DIM = "\033[2m"
RESET = "\033[0m"

HEADER = "代码       | 名称             | 价格       | 变幅     | 买一               | 卖一               | 目标       | 时间     | 状态"
HEADER_SIMPLE = "Symbol     | Price      | ChgPct   | Target"

Target = tuple  # (target_price: float, operation: str)


# ============================================================
# 数据类型
# ============================================================

@dataclass
class Quote:
    name: str
    price: float
    change_val: float
    prev_close: float
    change_pct: float
    time_str: str
    is_ashare: bool
    bid1_price: Optional[float] = None
    bid1_vol: Optional[float] = None
    ask1_price: Optional[float] = None
    ask1_vol: Optional[float] = None


# ============================================================
# 市场工具函数
# ============================================================

def market_status(now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return "CL"
    cur = (now.hour, now.minute)
    if any(start <= cur <= end for start, end in TRADE_SESSIONS):
        return "OP"
    if (11, 30) < cur < (13, 0):
        return "BK"
    return "CL"


def is_ashare(sym: str) -> bool:
    return not (
        sym.startswith("int_")
        or sym.startswith("b_")
        or sym.startswith("znb_")
        or sym.startswith("gb_")
    )


def is_us(sym: str) -> bool:
    return sym.startswith("gb_")


# ============================================================
# 数据获取
# ============================================================

class SinaSession:
    HOST = "hq.sinajs.cn"
    HEADERS = {
        "Referer": "https://finance.sina.com.cn",
        "User-Agent": "Mozilla/5.0",
    }

    def __init__(self, timeout: int = 5):
        self.timeout = timeout
        self.conn: Optional[http.client.HTTPSConnection] = None

    def get(self, path: str) -> bytes:
        last_err: Optional[Exception] = None
        for _ in range(2):
            if self.conn is None:
                self.conn = http.client.HTTPSConnection(self.HOST, timeout=self.timeout)
            try:
                self.conn.request("GET", path, headers=self.HEADERS)
                return self.conn.getresponse().read()
            except (http.client.HTTPException, OSError) as e:
                last_err = e
                self.conn = None
        raise last_err


SESSION = SinaSession()


def _opt_float(fields: list, idx: int) -> Optional[float]:
    if len(fields) > idx and fields[idx]:
        return float(fields[idx])
    return None


def _parse_ashare(fields: list) -> Quote:
    price = float(fields[3])
    prev_close = float(fields[2])
    return Quote(
        name=fields[0],
        price=price,
        change_val=price - prev_close,
        prev_close=prev_close,
        change_pct=(price - prev_close) / prev_close * 100,
        time_str=fields[31],
        is_ashare=True,
        bid1_price=_opt_float(fields, 11),
        bid1_vol=_opt_float(fields, 10),
        ask1_price=_opt_float(fields, 21),
        ask1_vol=_opt_float(fields, 20),
    )


def _parse_us(fields: list) -> Quote:
    price = float(fields[1])
    change_pct = float(fields[2])
    change_val = float(fields[4])
    time_str = (
        fields[3].split(" ")[-1]
        if len(fields) > 3 and fields[3]
        else datetime.now().strftime("%H:%M:%S")
    )
    return Quote(
        name=fields[0],
        price=price,
        change_val=change_val,
        prev_close=price - change_val,
        change_pct=change_pct,
        time_str=time_str,
        is_ashare=False,
    )


def _parse_other(fields: list) -> Quote:
    price = float(fields[1])
    change_val = float(fields[2])
    change_pct = float(fields[3])
    time_str = (
        fields[5]
        if len(fields) > 5 and fields[5]
        else datetime.now().strftime("%H:%M:%S")
    )
    return Quote(
        name=fields[0],
        price=price,
        change_val=change_val,
        prev_close=price - change_val,
        change_pct=change_pct,
        time_str=time_str,
        is_ashare=False,
    )


def fetch_quotes(symbols: list) -> dict:
    raw = SESSION.get(f"/list={','.join(symbols)}").decode("gbk")
    results = {}
    for line in raw.strip().split("\n"):
        sym = line.split("=")[0].replace("var hq_str_", "")
        raw_data = line.split('"')[1]
        if not raw_data:
            continue
        fields = raw_data.split(",")
        if is_ashare(sym):
            results[sym] = _parse_ashare(fields)
        elif is_us(sym):
            results[sym] = _parse_us(fields)
        else:
            results[sym] = _parse_other(fields)
    return results


# ============================================================
# 显示格式化
# ============================================================

def _char_width(c: str) -> int:
    return 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1


def display_width(s: str) -> int:
    return sum(_char_width(c) for c in s)


def trunc_name(name: str, limit: int = 16) -> str:
    w = display_width(name)
    if w <= limit - 2:
        return name + " " * (limit - w)
    out, cw_sum = "", 0
    for c in name:
        cw = _char_width(c)
        if cw_sum + cw > limit - 2:
            break
        out += c
        cw_sum += cw
    return out + " " * (limit - 2 - cw_sum) + ".."


def format_order(price: Optional[float], vol: Optional[float]) -> str:
    if not price:
        return "--".ljust(16)
    qty = vol / 100
    qty_s = f"{qty / 1000:.1f}k" if qty > 1000 else f"{qty:.0f}"
    return f"{price:.3f} x {qty_s}".ljust(16)


def _change_color(change_pct: float) -> str:
    if change_pct > 0:
        return RED
    if change_pct < 0:
        return GREEN
    return WHITE


def _format_target(target_info: Optional[Target]) -> str:
    if target_info:
        tgt, op = target_info
        return f"{op}:{tgt:.3f}".ljust(10)
    return " " * 10


def format_line(
    symbol: str,
    info: Quote,
    status: str,
    target_info: Optional[Target] = None,
    highlighted: bool = False,
) -> str:
    price = info.price
    change_pct = info.change_pct
    time_str = info.time_str

    if not info.is_ashare:
        status = "--"

    sign = "+" if change_pct >= 0 else ""
    color = _change_color(change_pct)

    name_s = color + trunc_name(info.name) + RESET
    price_s = color + f"{price:.3f}".rjust(10) + RESET
    pct_s = color + f"{sign}{change_pct:.2f}%".rjust(8) + RESET

    bid1 = info.bid1_price
    ask1 = info.ask1_price
    bid_hit = bool(bid1) and round(price, 3) == round(bid1, 3)
    ask_hit = bool(ask1) and round(price, 3) == round(ask1, 3)

    bid_txt = format_order(bid1, info.bid1_vol)
    bid_mark = RED + "> " + RESET if bid_hit else "  "
    bid_s = bid_mark + (WHITE + bid_txt + RESET if bid1 else bid_txt)

    ask_txt = format_order(ask1, info.ask1_vol)
    ask_mark = GREEN + "> " + RESET if ask_hit else "  "
    ask_s = ask_mark + (WHITE + ask_txt + RESET if ask1 else ask_txt)

    tgt_s = _format_target(target_info)
    sym_s = GOLD + f"{symbol:<10}" + RESET if highlighted else f"{symbol:<10}"

    pad = 4 - display_width(status)
    status_s = " " * (pad // 2) + status + " " * (pad - pad // 2)

    return f"{sym_s} | {name_s} | {price_s} | {pct_s} | {bid_s} | {ask_s} | {tgt_s} | {time_str:<8} | {status_s}"


def format_line_simple(
    symbol: str,
    info: Quote,
    target_info: Optional[Target] = None,
    highlighted: bool = False,
) -> str:
    change_pct = info.change_pct
    sign = "+" if change_pct >= 0 else ""
    color = _change_color(change_pct)

    price_s = color + f"{info.price:.3f}".rjust(10) + RESET
    pct_s = color + f"{sign}{change_pct:.2f}%".rjust(8) + RESET
    tgt_s = _format_target(target_info)
    sym_s = GOLD + f"{symbol:<10}" + RESET if highlighted else f"{symbol:<10}"

    return f"{sym_s} | {price_s} | {pct_s} | {tgt_s}"


# ============================================================
# 配置管理
# ============================================================

def ensure_config() -> None:
    if os.path.isfile(CONFIG_FILE) and os.path.getsize(CONFIG_FILE) > 0:
        return
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write(CONFIG_HEADER + "\n")
        f.write(CONFIG_DEFAULT + "\n")


def load_config() -> tuple:
    ensure_config()

    symbols = []
    targets = {}

    with open(CONFIG_FILE, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if lineno == 1 and line.lower().startswith("symbol"):
                continue

            parts = line.split()
            sym = parts[0]
            symbols.append(sym)

            if len(parts) == 3:
                op = parts[1].upper()
                if op not in ("B", "S"):
                    print(f"警告: 第{lineno}行操作无效(需B/S): {line}")
                    continue
                try:
                    tgt = float(parts[2])
                except ValueError:
                    print(f"警告: 第{lineno}行目标价无效: {line}")
                    continue
                targets[sym] = (tgt, op)
            elif len(parts) != 1:
                print(f"警告: 第{lineno}行格式错误: {line}")

    if not symbols:
        print("配置文件为空，无监听标的")
        sys.exit(1)

    return symbols, targets


def strip_target(symbol: str) -> None:
    lines = []
    with open(CONFIG_FILE, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            stripped = line.strip()
            if lineno == 1 and stripped.lower().startswith("symbol"):
                lines.append(stripped)
                continue
            if not stripped:
                lines.append("")
                continue
            parts = stripped.split()
            if parts[0] == symbol:
                lines.append(parts[0])
            else:
                lines.append(stripped)

    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, CONFIG_FILE)


# ============================================================
# 通知
# ============================================================

def send_notification(symbol: str, op: str, tgt: float, price: float) -> None:
    action = "买入" if op == "B" else "卖出"
    try:
        subprocess.run(
            ["notify-send", f"{symbol} 条件达成", f"{action}:{tgt:.3f}  现价:{price:.3f}"],
            timeout=3,
        )
    except Exception:
        pass


# ============================================================
# 主程序
# ============================================================

class StockWatcher:
    def __init__(self):
        self.interval = 1.0
        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        self._simple = False
        self._data: dict = {}
        self._prices: dict = {}
        self._triggered: dict = dict(load_config()[1])
        self._highlight_times: dict = {}
        self._last_fetch_time: Optional[datetime] = None

    # ---- 数据获取 ----

    def _fetch(self, symbols: list) -> None:
        if not symbols:
            return
        results = fetch_quotes(symbols)
        with self._lock:
            self._data.update(results)
            self._prices = {sym: q.price for sym, q in results.items()}
            self._last_fetch_time = datetime.now()

    def _poll_loop(self) -> None:
        while not self._shutdown.is_set():
            symbols, _ = load_config()
            status = market_status()
            a_symbols = [s for s in symbols if is_ashare(s)]
            intl_symbols = [s for s in symbols if not is_ashare(s)]
            fetch_symbols = intl_symbols + (a_symbols if status in ("OP", "BK") else [])
            try:
                self._fetch(fetch_symbols)
            except Exception:
                pass
            self._shutdown.wait(self.interval)

    # ---- 触发检查 ----

    def _check_alerts(self, config: tuple) -> None:
        _, targets = config
        now = time.time()
        alerts = []

        with self._lock:
            for sym in targets:
                if sym not in self._triggered:
                    self._triggered[sym] = targets[sym]

            for sym in list(self._triggered):
                if sym not in targets:
                    continue
                tgt, op = self._triggered[sym]
                price = self._prices.get(sym)
                if price is None:
                    continue
                if (op == "B" and price <= tgt) or (op == "S" and price >= tgt):
                    del self._triggered[sym]
                    self._highlight_times[sym] = now
                    alerts.append((sym, op, tgt, price))

            for sym in list(self._highlight_times):
                if now - self._highlight_times[sym] > ALERT_DURATION:
                    del self._highlight_times[sym]

        for sym, op, tgt, price in alerts:
            strip_target(sym)
            sys.stdout.write("\a")
            sys.stdout.flush()
            send_notification(sym, op, tgt, price)

    # ---- 渲染 ----

    def _render(self, config: tuple, status: str) -> None:
        symbols, targets = config

        with self._lock:
            data = dict(self._data)
            highlights = dict(self._highlight_times)
            last_fetch = self._last_fetch_time

        if self._simple:
            lines = [
                format_line_simple(
                    sym, info,
                    target_info=targets.get(sym),
                    highlighted=sym in highlights,
                )
                for sym, info in data.items()
                if sym in symbols
            ]
        else:
            lines = [
                format_line(
                    sym, info, status,
                    target_info=targets.get(sym),
                    highlighted=sym in highlights,
                )
                for sym, info in data.items()
                if sym in symbols
            ]

        header = HEADER_SIMPLE if self._simple else HEADER
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.write(header + "\n")
        for line in lines:
            sys.stdout.write(line + "\n")

        if last_fetch:
            ts = last_fetch.strftime("%Y-%m-%d %H:%M:%S")
            if self._simple:
                sys.stdout.write(f"{DIM}{ts}{RESET}\n")
            else:
                sys.stdout.write(f"{DIM}数据获取时间: {ts}{RESET}\n")

        sys.stdout.flush()

    # ---- 输入处理 ----

    def _open_editor(self) -> None:
        try:
            subprocess.run(
                ["gnome-text-editor", CONFIG_FILE],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def _handle_input(self, ch: str) -> bool:
        if ch == "q":
            self._shutdown.set()
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
            return True
        if ch == "s":
            with self._lock:
                self._simple = not self._simple
        elif ch == "e":
            threading.Thread(target=self._open_editor, daemon=True).start()
        elif ch == "\x1b\x4f\x50":  # F1
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sys.stdout.write(f"\033[K实时时间: {ts}\n")
            sys.stdout.flush()
        return False

    # ---- 主循环 ----

    def run(self) -> None:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)

            symbols, _ = load_config()
            status = market_status()
            a_symbols = [s for s in symbols if is_ashare(s)]
            intl_symbols = [s for s in symbols if not is_ashare(s)]
            self._fetch(intl_symbols + a_symbols)

            config = load_config()
            self._render(config, status)

            t = threading.Thread(target=self._poll_loop, daemon=True)
            t.start()

            try:
                while True:
                    time.sleep(REFRESH_INTERVAL)
                    config = load_config()
                    status = market_status()
                    self._render(config, status)
                    self._check_alerts(config)
                    r, _, _ = select.select([sys.stdin], [], [], 0)
                    if r:
                        ch = sys.stdin.read(1)
                        if self._handle_input(ch):
                            break
            except KeyboardInterrupt:
                self._shutdown.set()
        finally:
            t.join(timeout=1)
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            sys.stdout.write("\n")


def main():
    StockWatcher().run()


if __name__ == "__main__":
    main()
