import json
import urllib.request
from datetime import date

import matplotlib.pyplot as plt
import pandas as pd

from .stock import Stock


SINA_REALTIME_URL = "https://hq.sinajs.cn/list={}"


def _fetch_names(symbols: list[str]) -> dict[str, str]:
    if not symbols:
        return {}
    url = SINA_REALTIME_URL.format(",".join(symbols))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("gbk")
    names = {}
    for line in raw.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split('"')
        if len(parts) < 2:
            continue
        var_name = parts[0].split("=")[0].strip().replace("var hq_str_", "")
        data = parts[1].split(",")
        if data:
            names[var_name] = data[0].replace(" ", "")
    return names


class Index:

    def __init__(self, config_path: str):
        self.config_path = config_path
        with open(config_path) as f:
            self.config = json.load(f)

        self.name = self.config["name"]
        self._validate_config()
        self._stocks = {}

        for item in self.config["contents"]:
            symbol = item["symbol"]
            self._stocks[symbol] = {
                "stock": Stock(symbol),
                "amount": item["amount"],
            }

    def _validate_config(self):
        symbols = self.config.get("symbols", [])
        contents = self.config.get("contents", [])
        existing = {item["symbol"] for item in contents}

        if not symbols:
            symbols = list(existing)
            self.config["symbols"] = symbols

        for sym in symbols:
            if sym not in existing:
                contents.append({"symbol": sym, "amount": 1})

        names = _fetch_names(symbols)
        for item in contents:
            if "amount" not in item:
                item["amount"] = 1
            item["name"] = names.get(item["symbol"], "")

        self.config["contents"] = contents
        self._save_config()

    def _save_config(self):
        with open(self.config_path, "w") as f:
            json.dump(self.config, f, ensure_ascii=False, indent=4)

    @property
    def df(self) -> pd.DataFrame:
        dfs = []
        for item in self._stocks.values():
            s = item["stock"]
            df = s.df[["date", "close"]].copy()
            df = df.rename(columns={"close": s.symbol})
            dfs.append(df)

        if not dfs:
            return pd.DataFrame(columns=["date", "close"])

        merged = dfs[0]
        for df in dfs[1:]:
            merged = pd.merge(merged, df, on="date", how="outer")

        merged = merged.sort_values("date").reset_index(drop=True)

        merged["close"] = 0.0
        for item in self._stocks.values():
            sym = item["stock"].symbol
            if sym in merged.columns:
                merged[sym] = merged[sym].ffill()
                merged["close"] += merged[sym].fillna(0) * item["amount"]

        return merged[["date", "close"]].dropna().reset_index(drop=True)

    def update(self):
        for item in self._stocks.values():
            item["stock"].update()

    def backfill(self, target_date: str):
        for item in self._stocks.values():
            item["stock"].backfill(target_date)

    def plot(self, *overlay_symbols: str, use_old: bool = False):
        if not use_old:
            self.update()

        plt.rcParams["font.sans-serif"] = ["AR PL UKai CN"]
        plt.rcParams["axes.unicode_minus"] = False

        idx_df = self.df
        start_date = idx_df["date"].min()
        start_value = idx_df["close"].iloc[0]

        plt.figure(figsize=(12, 6))
        plt.plot(idx_df["date"], idx_df["close"], linewidth=1.2, label=self.name, color="#FF55FF")

        for sym in overlay_symbols:
            s = Stock(sym)
            if not use_old:
                s.update()
                s.backfill(start_date.strftime("%Y-%m-%d"))

            stock_df = s.df[["date", "close"]].copy()
            merged = pd.merge(idx_df[["date"]], stock_df, on="date", how="left")
            merged["close"] = merged["close"].ffill()
            merged = merged.dropna()

            if merged.empty:
                continue

            scale = start_value / merged["close"].iloc[0]
            merged["close"] = merged["close"] * scale

            plt.plot(merged["date"], merged["close"], linewidth=1.0, label=sym)

        plt.title(f"{self.name} 指数")
        plt.xlabel("日期")
        plt.ylabel("价格")
        plt.legend()
        plt.grid(True, alpha=0.3)
        plt.tight_layout()

        filename = f"{self.name}_{date.today().strftime('%Y%m%d')}_price.svg"
        plt.savefig(filename)
        plt.close()

    def info(self, use_old: bool = False) -> pd.DataFrame:
        if not use_old:
            self.update()

        idx_df = self.df
        index_close = idx_df["close"].iloc[-1]

        rows = []
        for item in self.config["contents"]:
            sym = item["symbol"]
            amount = item["amount"]
            name = item.get("name", "")
            price = self._stocks[sym]["stock"].df["close"].iloc[-1]
            ratio = price * amount / index_close * 100
            rows.append({
                "symbol": sym,
                "name": name,
                "price": price,
                "amount": amount,
                "position": price * amount,
                "ratio": round(ratio, 2),
            })

        df = pd.DataFrame(rows)
        df = df.sort_values("position", ascending=True)

        plt.rcParams["font.sans-serif"] = ["AR PL UKai CN"]
        plt.rcParams["axes.unicode_minus"] = False

        labels = [f"{r['name']}\n{r['symbol']}" for _, r in df.iterrows()]
        positions = df["position"].values
        ratios = df["ratio"].values
        prices = df["price"].values
        y = range(len(labels))
        height = 0.35

        fig, ax1 = plt.subplots(figsize=(12, max(8, len(df) * 0.5)))
        ax2 = ax1.twiny()

        bars1 = ax1.barh([i + height / 2 for i in y], positions, height, color="#4472C4", label="头寸")
        for bar, ratio_val in zip(bars1, ratios):
            ax1.text(bar.get_width(), bar.get_y() + bar.get_height() / 2,
                     f" {ratio_val:.2f}%", va="center", fontsize=8, color="black")

        bars2 = ax2.barh([i - height / 2 for i in y], prices, height, color="#C00000", label="价格")
        for bar, price_val in zip(bars2, prices):
            ax2.text(bar.get_width(), bar.get_y() + bar.get_height() / 2,
                     f" {price_val:.3f}", va="center", fontsize=8, color="black")

        ax1.set_yticks(y)
        ax1.set_yticklabels(labels, fontsize=8)
        ax1.set_xlabel("头寸", color="#4472C4")
        ax2.set_xlabel("价格", color="#C00000")

        fig.suptitle(f"{self.name} 成分股 ({idx_df['date'].iloc[-1].strftime('%Y-%m-%d')})")
        fig.legend(loc="lower right")
        plt.tight_layout()

        filename = f"{self.name}_{date.today().strftime('%Y%m%d')}_contents.svg"
        plt.savefig(filename)
        plt.close()

        return df
