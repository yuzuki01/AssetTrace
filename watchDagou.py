#!/usr/bin/env python3
import http.client
import os
import select
import subprocess
import sys
import termios
import time
import tty
import unicodedata
from datetime import datetime

TRADE_SESSIONS = [
    ((9, 30), (11, 30)),
    ((13, 0), (15, 0)),
]

ALERT_DURATION = 30

RED = "\033[31m"
GREEN = "\033[32m"
WHITE = "\033[37m"
GOLD = "\033[1;33m"
RESET = "\033[0m"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchDagou.txt")


def market_status(now=None):
    now = now or datetime.now()
    if now.weekday() >= 5:
        return "CL"
    cur = (now.hour, now.minute)
    if any(start <= cur <= end for start, end in TRADE_SESSIONS):
        return "OP"
    if (11, 30) < cur < (13, 0):
        return "BK"
    return "CL"


def _is_ashare(sym):
    return not (sym.startswith("int_") or sym.startswith("b_") or sym.startswith("znb_") or sym.startswith("gb_"))


def _is_us(sym):
    return sym.startswith("gb_")


class SinaSession:
    HOST = "hq.sinajs.cn"
    HEADERS = {
        "Referer": "https://finance.sina.com.cn",
        "User-Agent": "Mozilla/5.0",
    }

    def __init__(self, timeout=5):
        self.timeout = timeout
        self.conn = None

    def get(self, path):
        last_err = None
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


def fetch(symbols):
    raw = SESSION.get(f"/list={','.join(symbols)}").decode("gbk")
    results = {}
    for line in raw.strip().split("\n"):
        sym = line.split("=")[0].replace("var hq_str_", "")
        raw_data = line.split('"')[1]
        if not raw_data:
            continue
        fields = raw_data.split(",")
        is_a = _is_ashare(sym)
        if is_a:
            price = float(fields[3])
            prev_close = float(fields[2])
            change_val = price - prev_close
            change_pct = (price - prev_close) / prev_close * 100
            time_str = fields[31]
            bid1_price = float(fields[11]) if len(fields) > 11 and fields[11] else None
            bid1_vol = float(fields[10]) if len(fields) > 10 and fields[10] else None
            ask1_price = float(fields[21]) if len(fields) > 21 and fields[21] else None
            ask1_vol = float(fields[20]) if len(fields) > 20 and fields[20] else None
        elif _is_us(sym):
            price = float(fields[1])
            change_pct = float(fields[2])
            change_val = float(fields[4])
            prev_close = price - change_val
            time_str = fields[3].split(" ")[-1] if len(fields) > 3 and fields[3] else datetime.now().strftime("%H:%M:%S")
            bid1_price = bid1_vol = ask1_price = ask1_vol = None
        else:
            price = float(fields[1])
            change_val = float(fields[2])
            prev_close = price - change_val
            change_pct = float(fields[3])
            time_str = fields[5] if len(fields) > 5 and fields[5] else datetime.now().strftime("%H:%M:%S")
            bid1_price = bid1_vol = ask1_price = ask1_vol = None
        results[sym] = {
            "name": fields[0],
            "price": price,
            "change_val": change_val,
            "prev_close": prev_close,
            "change_pct": change_pct,
            "time_str": time_str,
            "isAShare": is_a,
            "bid1_price": bid1_price,
            "bid1_vol": bid1_vol,
            "ask1_price": ask1_price,
            "ask1_vol": ask1_vol,
        }
    return results


def _char_width(c):
    return 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1


def _display_width(s):
    return sum(_char_width(c) for c in s)


def trunc_name(name, limit=16):
    w = _display_width(name)
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


def _fmt_order(price, vol):
    if not price:
        return "--".ljust(12)
    qty = vol / 100
    qty_s = f"{qty / 1000:.1f}k" if qty > 1000 else f"{qty:.0f}"
    return f"{price:.3f} x {qty_s}".ljust(12)


HEADER = "代码       | 名称             | 价格       | 变幅     | 买一           | 卖一           | 目标       | 时间     | 状态"


def format_line(symbol, info, status, target_info=None, triggered=False):
    name = trunc_name(info["name"])
    price = info["price"]
    change_pct = info["change_pct"]
    time_str = info["time_str"]
    if not info["isAShare"]:
        status = "--"
    sign = "+" if change_pct >= 0 else ""
    color = RED if change_pct > 0 else GREEN if change_pct < 0 else WHITE
    name_s = color + name + RESET
    price_s = color + f"{price:.3f}".rjust(10) + RESET
    pct_s = color + f"{sign}{change_pct:.2f}%".rjust(8) + RESET
    bid1 = info["bid1_price"]
    ask1 = info["ask1_price"]
    bid_hit = bool(bid1) and round(price, 3) == round(bid1, 3)
    ask_hit = bool(ask1) and round(price, 3) == round(ask1, 3)
    bid_txt = _fmt_order(bid1, info["bid1_vol"])
    bid_mark = RED + "> " + RESET if bid_hit else "  "
    bid_s = bid_mark + (WHITE + bid_txt + RESET if bid1 else bid_txt)
    ask_txt = _fmt_order(ask1, info["ask1_vol"])
    ask_mark = GREEN + "> " + RESET if ask_hit else "  "
    ask_s = ask_mark + (WHITE + ask_txt + RESET if ask1 else ask_txt)
    if target_info:
        tgt, op = target_info
        tgt_s = f"{op}:{tgt:.3f}".ljust(10)
    else:
        tgt_s = " " * 10
    sym_s = GOLD + f"{symbol:<10}" + RESET if triggered else f"{symbol:<10}"
    pad = 4 - _display_width(status)
    status_s = " " * (pad // 2) + status + " " * (pad - pad // 2)
    line = f"{sym_s} | {name_s} | {price_s} | {pct_s} | {bid_s} | {ask_s} | {tgt_s} | {time_str:<8} | {status_s}"
    return line


def load_config():
    if not os.path.isfile(CONFIG_FILE):
        print(f"配置文件不存在: {CONFIG_FILE}")
        sys.exit(1)
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


def strip_target(symbol):
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


def notify(symbol, op, tgt, price):
    action = "买入" if op == "B" else "卖出"
    try:
        subprocess.run(
            [
                "notify-send",
                f"{symbol} 条件达成",
                f"{action}:{tgt:.3f}  现价:{price:.3f}",
            ],
            timeout=3,
        )
    except Exception:
        pass


class WatchDagou:
    def __init__(self):
        self.interval = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
        self.symbols, self.targets = load_config()
        self._data = {}
        self._triggered = {}
        self._prices = {}

    def reload_config(self):
        self.symbols, self.targets = load_config()

    def poll(self, status, force_all=False):
        self.reload_config()
        a_symbols = [s for s in self.symbols if _is_ashare(s)]
        intl_symbols = [s for s in self.symbols if not _is_ashare(s)]
        fetch_symbols = intl_symbols + (a_symbols if force_all or status in ("OP", "BK") else [])
        if fetch_symbols:
            results = fetch(fetch_symbols)
            self._data.update(results)
            self._prices = {sym: info["price"] for sym, info in results.items()}
        self.check_alert()
        self.lines = [
            format_line(
                sym, info, status,
                target_info=self.targets.get(sym),
                triggered=sym in self._triggered,
            )
            for sym, info in self._data.items()
            if sym in self.symbols
        ]

    def check_alert(self):
        now = time.time()
        for sym in list(self.targets):
            tgt, op = self.targets[sym]
            price = self._prices.get(sym)
            if price is None:
                continue
            if sym in self._triggered:
                if now - self._triggered[sym] > ALERT_DURATION:
                    del self._triggered[sym]
                    del self.targets[sym]
                continue
            if (op == "B" and price <= tgt) or (op == "S" and price >= tgt):
                self._triggered[sym] = now
                strip_target(sym)
                sys.stdout.write("\a")
                sys.stdout.flush()
                notify(sym, op, tgt, price)

    def render(self):
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.write(HEADER + "\n")
        for line in self.lines:
            sys.stdout.write(line + "\n")
        sys.stdout.flush()

    def run(self):
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            self.poll(market_status(), force_all=True)
            self.render()
            while True:
                self.poll(market_status())
                self.render()
                r, _, _ = select.select([sys.stdin], [], [], self.interval)
                if r and sys.stdin.read(1) == "q":
                    break
        except KeyboardInterrupt:
            pass
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            sys.stdout.write("\n")


if __name__ == "__main__":
    WatchDagou().run()
