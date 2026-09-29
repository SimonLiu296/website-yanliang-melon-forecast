# -*- coding: utf-8 -*-
"""阎良甜瓜价格预测 DEMO（Flask + ECharts，数据存储于 Neon Postgres）

本地运行：python app.py  ->  http://127.0.0.1:5001
Vercel：框架预设直接托管本文件（原生路径路由）；图表 PNG 在 public/figures/（CDN 直出），静态资源在 public/static/。

数据：TABLE_STEMS 中可生成的 13 张表在 Neon 中与表名同名（列名保留中文表头），
      灌库：python seed.py（源 CSV 快照在 data/ 下）；数据日期见 meta 表。
DEMO 模式：价格预测基于模拟日级时序，仅作流程演示，不构成实际价格判断。
"""
import json
import os
import socket
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from flask import (Flask, abort, jsonify, render_template, request,
                   send_file)
from sqlalchemy import create_engine

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

app = Flask(__name__, static_folder="public/static", static_url_path="/static")

FIG_DIR = BASE_DIR / "public" / "figures"

MISSING_HINT = "暂无预测结果，请先运行 python seed.py 灌入数据。"

# 逻辑表名 -> 中文说明（实际数据在 Neon，同名表；缺表时接口按“暂无结果”降级）
TABLE_STEMS = {
    "arima_forecast": "ARIMA 预测明细",
    "lstm_forecast": "LSTM 预测明细",
    "fusion_forecast": "融合预测明细",
    "fusion_metrics": "融合评估指标",
    "model_comparison": "模型对比表",
    "arima_metrics": "ARIMA 评估指标",
    "lstm_metrics": "LSTM 评估指标",
    "arima_params": "ARIMA 定阶参数",
    "variety_forecast": "各品种预测对比",
    "skin_type_forecast": "薄皮 / 厚皮预测对比",
    "month_forecast": "4-6 月价格对比",
    "planting_advice": "种植建议",
    "sales_timing_advice": "销售时机建议",
    "industry_knowledge": "调研品种知识",
    "monthly_price_range": "调研月度价格区间",
    "planting_batch_plan": "调研种植批次",
}

MODEL_ORDER = ["ARIMA", "LSTM", "ARIMA-LSTM融合"]
MODEL_COLORS = {"ARIMA": "#4C7BD9", "LSTM": "#2ECC71", "ARIMA-LSTM融合": "#E67E22"}

_cache = {}


def _db_url():
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("缺少 DATABASE_URL（复制 .env.example 为 .env 并填写，或在 Vercel 设置环境变量）")
    # Neon 连接串带 channel_binding=require，部分驱动不支持 → 降级为 disable（保留 sslmode=require）
    # 显式指定 psycopg3 驱动（postgresql+psycopg://）
    import re
    url = re.sub(r"channel_binding=\w+", "channel_binding=disable", url)
    return re.sub(r"^postgres(ql)?://", "postgresql+psycopg://", url)


engine = create_engine(_db_url(), pool_pre_ping=True, pool_recycle=300,
                       connect_args={"connect_timeout": 10})


def _meta():
    """meta 表：table_name / data_date / source_file / rows / size_kb / loaded_at"""
    if "meta" not in _cache:
        try:
            _cache["meta"] = pd.read_sql('SELECT * FROM "meta"', engine)
        except Exception:
            _cache["meta"] = pd.DataFrame(columns=["table_name", "data_date", "source_file", "rows"])
    return _cache["meta"]


def read_table(stem):
    """读取 Neon 表；缺表 / 查询失败返回 None（等同原“文件不存在”语义）。"""
    key = "df:" + stem
    if key in _cache:
        return _cache[key]
    df = None
    try:
        df = pd.read_sql(f'SELECT * FROM "{stem}"', engine)
    except Exception as err:
        print(f"[warn] read_table({stem}): {err}")
    _cache[key] = df
    return df


def data_date(stem):
    """meta 表中的数据日期（如 '2026-09-28'）；无记录返回 '-'。"""
    m = _meta()
    row = m[m["table_name"].astype(str) == stem]
    if len(row):
        return str(row["data_date"].iloc[0])
    return "-"


def to_records(df):
    """DataFrame -> JSON 友好的 list[dict]（NaN 转 null）。"""
    return json.loads(df.to_json(orient="records", force_ascii=False))


def safe_float(v, nd=4):
    try:
        if v is None or pd.isna(v):
            return None
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def unavailable(msg=MISSING_HINT):
    return jsonify({"available": False, "hint": msg, "demo": True})


def forecast_frames():
    """读取三个模型的预测明细（缺一不可）。"""
    out = {}
    for name, stem in (("ARIMA", "arima_forecast"),
                       ("LSTM", "lstm_forecast"),
                       ("ARIMA-LSTM融合", "fusion_forecast")):
        out[name] = read_table(stem)
    return out


def product_list():
    """可选商品列表（含平台，来自模拟序列）。"""
    frame = None
    for df in forecast_frames().values():
        if df is not None and not df.empty:
            frame = df
            break
    if frame is None:
        return []
    fks = sorted(int(x) for x in frame["商品FK"].dropna().unique())
    platform = {}
    sim = read_table("simulated_price_series")
    if sim is not None:
        platform = dict(zip(sim["商品FK"].astype(int), sim["平台"]))
    return [{"商品FK": fk, "平台": platform.get(fk, "-")} for fk in fks]


def merge_product_frames(frames, fk):
    """把三个模型的预测明细按日期对齐。

    各模型的 train/test 切分并不一致，故数据段按 train < test < future 取优先级；
    实际值取各模型中首个非空值。
    返回按日期升序的 DataFrame：数据段、实际值、<模型>、<模型>_lo、<模型>_hi。
    """
    prio = {"train": 0, "test": 1, "future": 2}
    out = None
    actual = {}
    seg = {}

    for model, df in frames.items():
        sub = df[df["商品FK"] == fk].copy()
        if sub.empty:
            continue
        sub["日期"] = pd.to_datetime(sub["日期"]).dt.strftime("%Y-%m-%d")
        nums = pd.to_numeric(sub["实际值"], errors="coerce")
        for d, s, a in zip(sub["日期"], sub["数据段"].astype(str), nums):
            if pd.notna(a) and d not in actual:
                actual[d] = float(a)
            p = prio.get(s, 0)
            if d not in seg or p > seg[d][0]:
                seg[d] = (p, s)
        sub = sub[~sub["日期"].duplicated(keep="last")].set_index("日期")
        part = pd.DataFrame({
            model: pd.to_numeric(sub["预测值"], errors="coerce"),
            f"{model}_lo": pd.to_numeric(sub["下限"], errors="coerce"),
            f"{model}_hi": pd.to_numeric(sub["上限"], errors="coerce"),
        })
        out = part if out is None else out.join(part, how="outer")

    if out is None or out.empty:
        return pd.DataFrame()
    out = out.sort_index()
    out["实际值"] = [actual.get(d) for d in out.index]
    out["数据段"] = [seg.get(d, (0, "train"))[1] for d in out.index]
    return out.reset_index()


# ---------------------------------------------------------------- 页面
@app.route("/")
def index():
    return render_template("index.html", active="index")


@app.route("/forecast")
def forecast():
    return render_template("forecast.html", active="forecast")


@app.route("/advice")
def advice():
    return render_template("advice.html", active="advice")


# ---------------------------------------------------------------- API
@app.route("/api/forecast/summary")
def api_summary():
    """概览：KPI + 数据日期 + 生成的图表清单。"""
    cmp_df = read_table("model_comparison")
    if cmp_df is None or cmp_df.empty:
        return unavailable()
    base = cmp_df.drop_duplicates("模型").set_index("模型")
    best = base["MAE"].astype(float).idxmin() if not base.empty else "-"
    fusions = read_table("fusion_forecast")
    horizon = int(pd.to_numeric(cmp_df["预测天数"], errors="coerce").max() or 0)
    figures = sorted(p.name for p in FIG_DIR.glob("*.png")) if FIG_DIR.is_dir() else []

    return jsonify({
        "available": True,
        "demo": True,
        "data_date": data_date("model_comparison"),
        "hint": MISSING_HINT,
        "kpis": {
            "product_count": int(fusions["商品FK"].nunique()) if fusions is not None else 0,
            "forecast_days": horizon,
            "model_count": int(base.shape[0]),
            "best_model": str(best),
            "best_mae": safe_float(base.loc[best, "MAE"], 4) if best in base.index else None,
            "best_mape": safe_float(base.loc[best, "MAPE"], 4) if best in base.index else None,
        },
        "figures": figures,
        "missing_hint": MISSING_HINT,
    })


@app.route("/api/forecast/models")
def api_models():
    """模型对比：MAE / RMSE / MAPE 与 95% 预测区间。"""
    cmp_df = read_table("model_comparison")
    if cmp_df is None or cmp_df.empty:
        return unavailable()
    rows = to_records(cmp_df)
    horizon = int(pd.to_numeric(cmp_df["预测天数"], errors="coerce").max() or 90)
    base = cmp_df[cmp_df["预测天数"] == horizon].copy()

    categories, series = [], []
    for model in MODEL_ORDER:
        sub = base[base["模型"] == model]
        if sub.empty:
            continue
        categories.append(model)
        row = sub.iloc[0]
        series.append({
            "name": model,
            "color": MODEL_COLORS.get(model, "#888888"),
            "MAE": safe_float(row["MAE"]),
            "RMSE": safe_float(row["RMSE"]),
            "MAPE": safe_float(row["MAPE"]),
            "interval_low": safe_float(row["置信区间下限"], 2),
            "interval_high": safe_float(row["置信区间上限"], 2),
        })

    metrics = [
        {"key": "MAE", "label": "MAE（平均绝对误差）"},
        {"key": "RMSE", "label": "RMSE（均方根误差）"},
        {"key": "MAPE", "label": "MAPE（平均绝对百分比误差 %）"},
    ]
    best = min((s for s in series if s["MAE"] is not None),
               key=lambda s: s["MAE"], default=None)
    return jsonify({
        "available": True,
        "demo": True,
        "data_date": data_date("model_comparison"),
        "horizon": horizon,
        "categories": categories,
        "series": series,
        "metrics": metrics,
        "best": (best or {}).get("name"),
        "window": str(base.iloc[0].get("评估窗口", "")) if len(base) else "",
        "table": rows,
        "note": str(cmp_df.iloc[0].get("数据说明", "")),
    })


def series_item(frames, fk):
    """单个商品的历史 + 三模型预测 + 融合 95% 区间。"""
    merged = merge_product_frames(frames, fk)
    if merged.empty:
        return None

    hist = merged[merged["数据段"] != "future"]
    fut = merged[merged["数据段"] == "future"]

    def series_of(name, nd=3):
        vals = merged[name] if name in merged.columns else [None] * len(merged)
        return [safe_float(v, nd) for v in vals]

    return {
        "fk": fk,
        "dates": merged["日期"].tolist(),
        "segments": merged["数据段"].tolist(),
        "actual": series_of("实际值"),
        "arima": series_of(MODEL_ORDER[0]),
        "lstm": series_of(MODEL_ORDER[1]),
        "fusion": series_of(MODEL_ORDER[2]),
        "lo": series_of(f"{MODEL_ORDER[2]}_lo"),
        "hi": series_of(f"{MODEL_ORDER[2]}_hi"),
        "history_len": int(len(hist)),
        "future_from": (fut["日期"].iloc[0] if len(fut) else None),
        "future_days": int(len(fut)),
    }


@app.route("/api/forecast/series")
def api_series():
    """价格序列：可多选商品（fks=75,5,7 或 fk=75），逐商品返回曲线数据。

    不做数量上限（25 个商品全选时响应约 270 KB、耗时 < 0.2s），
    limit 字段返回可选商品总数，供前端展示「已选 n / 总数」。
    """
    frames = forecast_frames()
    if any(df is None or df.empty for df in frames.values()):
        return unavailable()

    products = product_list()
    if not products:
        return unavailable()
    valid = {p["商品FK"] for p in products}
    limit = len(products)
    first = products[0]["商品FK"]

    raw = (request.args.get("fks") or request.args.get("fk") or "").strip()
    picked = []
    if raw:
        for token in raw.replace("，", ",").split(","):
            token = token.strip()
            if not token:
                continue
            try:
                picked.append(int(token))
            except ValueError:
                continue

    selected, seen = [], set()
    for fk in picked:
        if fk in valid and fk not in seen:
            seen.add(fk)
            selected.append(fk)
    if not selected:
        selected = [first]
    truncated = len(selected) > limit
    selected = selected[:limit]

    items = [it for fk in selected for it in [series_item(frames, fk)] if it]
    if not items:
        return unavailable()
    platform = {p["商品FK"]: p["平台"] for p in products}
    for it in items:
        it["platform"] = platform.get(it["fk"], "-")

    return jsonify({
        "available": True,
        "demo": True,
        "products": products,
        "selected": [it["fk"] for it in items],
        "series": items,
        "limit": limit,
        "truncated": truncated,
        "note": "DEMO 模拟数据，仅作流程演示",
    })


@app.route("/api/forecast/table")
def api_table():
    """预测明细表：指定商品的历史与未来逐日预测。"""
    frames = forecast_frames()
    if any(df is None or df.empty for df in frames.values()):
        return unavailable()

    products = product_list()
    try:
        fk = int(request.args.get("fk", products[0]["商品FK"]))
    except (TypeError, ValueError):
        fk = products[0]["商品FK"]
    if fk not in {p["商品FK"] for p in products}:
        fk = products[0]["商品FK"]

    seg = (request.args.get("seg") or "").strip().lower()
    try:
        limit = min(max(int(request.args.get("limit", 200)), 1), 2000)
    except (TypeError, ValueError):
        limit = 200

    merged = merge_product_frames(frames, fk)
    if merged.empty:
        return unavailable()

    if seg:
        merged = merged[merged["数据段"].str.lower() == seg]
    merged = merged.sort_values("日期")

    cols = {"日期": "日期", "数据段": "数据段", "实际值": "实际值",
            "ARIMA": "ARIMA", "LSTM": "LSTM", "ARIMA-LSTM融合": "融合",
            "ARIMA-LSTM融合_lo": "融合下限", "ARIMA-LSTM融合_hi": "融合上限"}
    for col_name in cols:
        if col_name not in merged.columns:
            merged[col_name] = None
    view = merged[list(cols)].rename(columns=cols)
    total = int(len(view))
    rows = to_records(view.head(limit))
    for row in rows:
        for key, val in list(row.items()):
            if key not in ("日期", "数据段") and isinstance(val, float):
                row[key] = round(val, 3)

    return jsonify({
        "available": True,
        "demo": True,
        "fk": fk,
        "products": products,
        "seg": seg,
        "total": total,
        "limit": limit,
        "rows": rows,
        "columns": list(cols.values()),
        "note": "DEMO 模拟数据，仅作流程演示",
    })


@app.route("/api/advice")
def api_advice():
    """种植与销售建议：种植表 + 销售时机表 + 品种/月度对比（供 /advice 页渲染）。"""
    planting = read_table("planting_advice")
    if planting is None or planting.empty:
        return unavailable(MISSING_HINT + "（步骤 10 advice）")

    sales = read_table("sales_timing_advice")
    month = read_table("month_forecast")
    variety = read_table("variety_forecast")
    skin = read_table("skin_type_forecast")

    figures = {}
    for key, stem in (("planting", "planting_advice"),
                      ("sales", "forecast_by_skin_month"),
                      ("variety", "forecast_by_variety")):
        if (FIG_DIR / f"{stem}.png").is_file():
            figures[key] = f"{stem}.png"

    batches = []
    if "建议批次" in planting.columns:
        batches = sorted({str(v) for v in planting["建议批次"].dropna()
                          if str(v) and str(v) != "未列入调研批次"})

    return jsonify({
        "available": True,
        "demo": True,
        "data_date": data_date("planting_advice"),
        "hint": MISSING_HINT,
        "planting": to_records(planting),
        "sales": to_records(sales) if sales is not None else [],
        "month": to_records(month) if month is not None else [],
        "variety": to_records(variety) if variety is not None else [],
        "skin": to_records(skin) if skin is not None else [],
        "figures": figures,
        "batches": batches,
        "caliber": "模型=线上零售口径 / 调研=产地批发口径，绝对值不可直接比较",
        "note": "DEMO 模拟数据，仅作流程演示",
    })


# ---------------------------------------------------------------- 图表文件
@app.route("/figures/<path:filename>")
def figure_file(filename):
    """本地开发：提供 public/figures/ 下的 PNG（线上由 Vercel CDN 直出）。"""
    name = Path(filename).name
    if not name.endswith(".png") or name != filename:
        abort(404)
    path = FIG_DIR / name
    if not path.is_file():
        abort(404)
    return send_file(str(path), mimetype="image/png")


# ---------------------------------------------------------------- 图标
@app.route("/favicon.ico")
def favicon():
    """浏览器默认请求 /favicon.ico，直接指向 public/static 下的图标，避免 404。"""
    path = BASE_DIR / "public" / "static" / "favicon.ico"
    if not path.is_file():
        abort(404)
    return send_file(str(path), mimetype="image/x-icon")


# ---------------------------------------------------------------- 错误处理
@app.errorhandler(404)
def not_found(err):
    if request.path.startswith("/api/"):
        return jsonify({"available": False, "hint": MISSING_HINT}), 404
    return ("页面不存在", 404)


@app.errorhandler(500)
def server_error(err):
    if request.path.startswith("/api/"):
        return jsonify({"available": False, "hint": "服务暂时不可用，请稍后重试"}), 500
    return ("服务暂时不可用，请稍后重试", 500)


def find_free_port(host, start_port, limit=20):
    """从 start_port 起探测可用端口。"""
    for port in range(start_port, start_port + limit):
        fam = socket.AF_INET6 if ":" in host else socket.AF_INET
        sock = socket.socket(fam, socket.SOCK_STREAM)
        if fam == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port, 0) if fam == socket.AF_INET6 else (host, port))
            return port
        except OSError:
            continue
        finally:
            sock.close()
    return None


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    want = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_DEBUG", "0").lower() in ("1", "true", "yes")

    port = find_free_port(host, want)
    if port is None:
        raise SystemExit(f"[错误] {host} 起始端口 {want} 附近 20 个端口均不可用，请指定 PORT。")
    if port != want:
        print(f"[提示] 端口 {want} 不可用，已自动切换为 {port}")

    print(" * 阎良甜瓜价格预测 DEMO（Neon 数据，DEMO 模式仅作流程演示）")
    print(f" * http://{host}:{port}")
    app.run(host=host, port=port, debug=debug)
