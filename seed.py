# -*- coding: utf-8 -*-
"""灌库：把 13 张预测表从 artifacts 只读复制快照并写入 Neon。

用法：python seed.py
  - 源：../artifacts/outputs/tables/*.csv、../artifacts/data/processed/simulated_price_series*.csv
        （只读，按表名取日期最新）
  - 快照：复制到本目录 data/ 下（website-* 自包含）
  - 目标：Neon（.env 的 DATABASE_URL），同名表 replace，另建 meta 表
  - 校验：打印 源行数 / 库内行数 对账
"""
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

BASE_DIR = Path(__file__).resolve().parent
ROOT = BASE_DIR.parent
SNAPSHOT_DIR = BASE_DIR / "data"
SOURCE_DIRS = [
    ROOT / "artifacts" / "outputs" / "tables",
    ROOT / "artifacts" / "data" / "processed",
]
TABLES = [
    "arima_forecast", "lstm_forecast", "fusion_forecast",
    "fusion_metrics", "model_comparison", "arima_metrics", "lstm_metrics",
    "variety_forecast", "skin_type_forecast", "month_forecast",
    "planting_advice", "sales_timing_advice", "simulated_price_series",
]
ENCODINGS = ("utf-8-sig", "utf-8", "gb18030")

load_dotenv(BASE_DIR / ".env")
url = os.environ.get("DATABASE_URL", "").strip()
if not url:
    sys.exit("缺少 DATABASE_URL（见 .env.example）")
url = re.sub(r"channel_binding=\w+", "channel_binding=disable", url)
url = re.sub(r"^postgres(ql)?://", "postgresql+psycopg://", url)
engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 10})


def file_date_key(path, stem):
    """`stem_YYYYMMDD[_HHMM]` -> 'YYYYMMDDHHMM'；无日期 -> ''；其它后缀 -> None。"""
    p = path.stem
    if p == stem:
        return ""
    suffix = p[len(stem) + 1:] if p.startswith(stem + "_") else ""
    m = re.fullmatch(r"(\d{8}(?:_?\d{4})?)", suffix)
    return m.group(1).replace("_", "") if m else None


def fmt_date(key):
    if key == "":
        return "无日期后缀"
    return f"{key[0:4]}-{key[4:6]}-{key[6:8]}" + (f" {key[8:10]}:{key[10:12]}" if len(key) >= 12 else "")


def latest_source(stem):
    best = None
    for folder in SOURCE_DIRS:
        if not folder.is_dir():
            continue
        for path in folder.glob(stem + "*.csv"):
            key = file_date_key(path, stem)
            if key is None:
                continue
            score = (key, path.stat().st_mtime)
            if best is None or score > best[0]:
                best = (score, path)
    if best:
        return best[1]
    for folder in SOURCE_DIRS:
        plain = folder / (stem + ".csv")
        if plain.is_file():
            return plain
    return None


def read_csv(path):
    last = None
    for enc in ENCODINGS:
        try:
            return pd.read_csv(path, encoding=enc)
        except UnicodeDecodeError as err:
            last = err
    raise last


def main():
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    meta_rows, failed = [], []
    for stem in TABLES:
        src = latest_source(stem)
        if src is None:
            failed.append(stem)
            print(f"[skip] {stem}: 未找到源文件")
            continue
        key = file_date_key(src, stem) or ""
        snap = SNAPSHOT_DIR / src.name
        shutil.copy2(src, snap)  # 源只读
        df = read_csv(snap)
        df.to_sql(stem, engine, if_exists="replace", index=False)
        with engine.connect() as conn:
            db_rows = int(conn.execute(text(f'SELECT COUNT(*) FROM "{stem}"')).scalar())
        ok = "OK" if db_rows == len(df) else "!! MISMATCH"
        print(f"[{ok}] {stem}: src={src.name} rows={len(df)} db={db_rows} cols={len(df.columns)}")
        if db_rows != len(df):
            failed.append(stem)
        meta_rows.append({
            "table_name": stem,
            "data_date": fmt_date(key),
            "source_file": src.name,
            "rows": int(len(df)),
            "size_kb": round(snap.stat().st_size / 1024, 1),
            "loaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
    meta = pd.DataFrame(meta_rows)
    meta.to_sql("meta", engine, if_exists="replace", index=False)
    print(f"[meta] {len(meta)} 张表已记录")
    print("DONE" + (f" FAILED={failed}" if failed else ""))


if __name__ == "__main__":
    main()
