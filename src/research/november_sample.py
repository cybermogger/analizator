"""Fetch the independent November USD-M sample used by strategy_challenge."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import subprocess
import zipfile

import numpy as np
import pandas as pd

from .signal_audit import SYMBOLS, write_json

START = pd.Timestamp("2026-11-01", tz="UTC")
END = pd.Timestamp("2026-11-25", tz="UTC")
COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "close_time"]


def get(url):
    return subprocess.run(
        ["curl", "--fail", "--silent", "--show-error", "--max-time", "12",
         "--retry", "2", url], capture_output=True, check=True, timeout=50,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
    ).stdout


def fetch_day(job):
    symbol, day, folder = job
    filename = f"{symbol}-5m-{day:%Y-%m-%d}.zip"
    url = f"https://data.binance.vision/data/futures/um/daily/klines/{symbol}/5m/{filename}"
    path = folder / filename
    checksum_file = path.with_suffix(".zip.CHECKSUM")
    checksum = checksum_file.read_text().strip() if checksum_file.exists() else get(url+".CHECKSUM").decode().split()[0]
    body = path.read_bytes() if path.exists() else get(url)
    if hashlib.sha256(body).hexdigest() != checksum:
        raise ValueError(f"Checksum mismatch for {filename}")
    if not path.exists():
        path.write_bytes(body)
    checksum_file.write_text(checksum)
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        frame = pd.read_csv(archive.open(archive.namelist()[0]), header=None, usecols=range(7))
    frame = frame[pd.to_numeric(frame[0], errors="coerce").notna()].astype(float)
    frame.columns = COLUMNS
    expected = day.value // 1_000_000 + np.arange(288)*300_000
    if not np.array_equal(frame.open_time, expected) or not np.array_equal(frame.close_time, expected+299_999):
        raise ValueError(f"Incomplete day {filename}")
    return frame, {"url": url, "sha256": checksum}


def fetch(folder):
    folder.mkdir(parents=True, exist_ok=True)
    archives = folder / "public_archives"
    archives.mkdir(exist_ok=True)
    start_ms, end_ms = START.value//1_000_000, END.value//1_000_000
    dates = pd.date_range(START, END, inclusive="left")
    with ThreadPoolExecutor(max_workers=8) as pool:
        for symbol in SYMBOLS:
            daily = list(pool.map(fetch_day, ((symbol, day, archives) for day in dates)))
            bars = pd.concat([frame for frame, _ in daily], ignore_index=True)
            url = ("https://fapi.binance.com/fapi/v1/fundingRate?"
                   f"symbol={symbol}&startTime={start_ms}&endTime={end_ms-1}&limit=1000")
            body = get(url)
            rates = pd.DataFrame(json.loads(body)).sort_values("fundingTime")
            times = rates.fundingTime.to_numpy(dtype=np.int64)
            if (not len(times) or len(times)>=1000 or times[0]<start_ms or times[-1]>=end_ms
                    or max(np.diff(np.r_[start_ms, times, end_ms])) > 8*3_600_000+60_000
                    or rates.symbol.ne(symbol).any() or rates.fundingTime.duplicated().any()):
                raise ValueError(f"Incomplete funding history {symbol}")
            np.savez_compressed(folder / (symbol+".npz"), **{k: bars[k].to_numpy() for k in COLUMNS},
                                funding_time=times, funding_rate=rates.fundingRate.to_numpy(dtype=float))
            write_json(folder / (symbol+"-sources.json"),
                       {"klines": [info for _, info in daily], "funding_url": url,
                        "funding_sha256": hashlib.sha256(body).hexdigest(), "funding_events": len(times)})
            print(symbol, len(bars), len(times), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    fetch(parser.parse_args().folder)
