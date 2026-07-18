#!/usr/bin/env python3

import json
import os
import sys
import urllib.request
from datetime import date, timedelta

import pandas as pd


API_HISTORY_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"


def get_history(symbol: str, days: int = 30, start_date: str = None, end_date: str = None) -> list[dict]:
    if start_date:
        today = date.today()
        start = date.fromisoformat(start_date)
        calendar_days = (today - start).days
        days = max(30, int(calendar_days * 0.75) + 20)

    params = {
        "symbol": symbol,
        "scale": "240",
        "ma": "no",
        "datalen": str(days),
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{API_HISTORY_URL}?{query}"

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8")

    if not raw.strip():
        sys.exit("错误: 返回数据为空，请检查股票代码是否正确")

    data = json.loads(raw)

    if start_date:
        data = [d for d in data if d["day"] >= start_date]
    if end_date:
        data = [d for d in data if d["day"] <= end_date]

    if data:
        print(f"[sina] Get {symbol} from {data[0]['day']} to {data[-1]['day']}")
    return data


class Stock:

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.filepath = f"./data/{symbol}.kline"
        self._listing_date = None

        if os.path.exists(self.filepath):
            self._read_kline()
        else:
            self.df = pd.DataFrame(columns=["date", "open", "close", "high", "low", "volume"])

        self._catch_up()

    def _read_kline(self):
        with open(self.filepath) as f:
            first_line = f.readline()
        if first_line.startswith("# listing_date:"):
            self._listing_date = first_line.strip().split(":")[1]
            self.df = pd.read_csv(self.filepath, parse_dates=["date"], skiprows=1)
        else:
            self.df = pd.read_csv(self.filepath, parse_dates=["date"])

    def _catch_up(self):
        today = date.today()
        weekday = today.weekday()
        if weekday >= 5:
            reference_date = today - timedelta(days=weekday - 4)
        else:
            reference_date = today

        if self.df.empty:
            data = get_history(self.symbol, days=60)
            if data:
                self._merge(data)

        if not self.df.empty:
            latest_date = pd.Timestamp(self.df["date"].max()).date()
            if latest_date < reference_date:
                self.update()

        if len(self.df) < 60:
            target_date = (today - timedelta(days=90)).strftime("%Y-%m-%d")
            self.backfill(target_date)

    def update(self):
        today = date.today()
        weekday = today.weekday()
        if weekday >= 5:
            reference_date = today - timedelta(days=weekday - 4)
        else:
            reference_date = today
        reference_str = reference_date.strftime("%Y-%m-%d")

        data = get_history(self.symbol, start_date=reference_str)
        if not data:
            return

        self._merge(data)

    def backfill(self, target_date: str):
        if not self.df.empty:
            earliest = pd.Timestamp(self.df["date"].min())
            if self._listing_date and pd.Timestamp(target_date) < pd.Timestamp(self._listing_date):
                return
            if earliest <= pd.Timestamp(target_date) + pd.Timedelta(days=4):
                return
            data = get_history(self.symbol, start_date=target_date, end_date=earliest.strftime("%Y-%m-%d"))
        else:
            data = get_history(self.symbol, start_date=target_date)

        if not data:
            return

        if not self.df.empty:
            fetched_earliest = pd.Timestamp(data[0]["day"])
            current_earliest = pd.Timestamp(self.df["date"].min())
            if fetched_earliest >= current_earliest:
                self._listing_date = current_earliest.strftime("%Y-%m-%d")
                self._save()
                return

        self._merge(data)

        if self.df is not None and not self.df.empty:
            new_earliest = pd.Timestamp(self.df["date"].min())
            if new_earliest > pd.Timestamp(target_date) + pd.Timedelta(days=4):
                self._listing_date = new_earliest.strftime("%Y-%m-%d")
                self._save()

    def _merge(self, data: list[dict]):
        rows = []
        for item in data:
            rows.append({
                "date": item["day"],
                "open": float(item["open"]),
                "close": float(item["close"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "volume": float(item["volume"]),
            })

        new_df = pd.DataFrame(rows)
        new_df["date"] = pd.to_datetime(new_df["date"])

        if not self.df.empty:
            self.df = pd.concat([self.df, new_df], ignore_index=True)
        else:
            self.df = new_df

        self.df = self.df.drop_duplicates(subset=["date"], keep="last")
        self.df = self.df.sort_values("date").reset_index(drop=True)

        self._save()

    def _save(self):
        os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
        with open(self.filepath, "w") as f:
            if self._listing_date:
                f.write(f"# listing_date:{self._listing_date}\n")
            self.df.to_csv(f, index=False)