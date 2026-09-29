# 阎良甜瓜价格预测 DEMO（Flask + ECharts + Neon + Vercel）

`yanliang-melon-forecast` 的部署版：13 张预测表存 Neon Postgres，可一键部署到 Vercel。
原文件夹保持只读，本文件夹自包含（数据快照、模板、静态资源、图表 PNG 齐全）。

> **DEMO 模式 · 数据不足**：预测所用日级价格序列由模拟脚本生成，
> 页面中的预测、指标与图表**仅作流程演示，不构成任何实际价格判断**。

## 页面与 API

| 路由 | 内容 |
| --- | --- |
| `/` | 预测概览：KPI、模型精度柱状图、95% 区间表、图表输出、数据说明 |
| `/forecast` | 商品筛选、历史 + 三模型预测 + 融合区间曲线、逐日明细表 |
| `/advice` | 批次时间轴、种植建议、销售时机、4-6 月与品种价格对比 |

| 接口 | 返回 |
| --- | --- |
| `/api/forecast/summary` | KPI、数据日期、图表清单（52 张 PNG） |
| `/api/forecast/series?fk=/fks=` | 历史实际值 + ARIMA / LSTM / 融合预测 + 95% 区间 |
| `/api/forecast/models` | MAE / RMSE / MAPE 对比与明细表 |
| `/api/forecast/table?fk=&seg=&limit=` | 逐日预测明细（seg = train/test/future） |
| `/api/advice` | 种植/销售建议表、对比数据、图片清单、口径提示 |

数据缺失时统一返回 `{"available": false, "hint": "..."}`。

## 目录结构

```
website-yanliang-melon-forecast/
├── app.py              # Flask 应用（读 Neon：pd.read_sql + 进程内缓存；缺表降级 unavailable；Vercel 框架入口）
├── vercel.json         # 项目配置（函数区域在 Dashboard → Settings → Functions 设置）
├── requirements.txt    # flask/pandas/sqlalchemy/psycopg[binary]/python-dotenv（无 torch）
├── seed.py             # artifacts CSV → Neon 灌库 + 行数对账 + meta 表
├── .env                # 真实 DATABASE_URL（已 gitignore，不提交）
├── .env.example        # 连接串模板
├── .gitignore          # .env / .vercel / __pycache__
├── data/               # 数据快照（13 张 CSV，seed.py 从 artifacts 只读复制）
├── templates/          # 页面模板（与原版一致）
└── public/
    ├── static/         # css/js/favicon（本地与 Vercel CDN 同一路径）
    └── figures/        # 52 张图表 PNG（线上 CDN 直出 /figures/*，本地走 Flask 路由）
```

## 本地运行

```bash
cd website-yanliang-melon-forecast
pip install -r requirements.txt          # 已装可跳过；不含 torch/matplotlib
python seed.py                           # 首次：灌库（读 .env 的 DATABASE_URL）
python app.py                            # -> http://127.0.0.1:5001
```

## 数据说明（Neon）

- 13 张表（arima/lstm/fusion 预测明细、模型对比、评估指标、品种/皮类/月度对比、
  种植与销售建议、模拟价格序列）以**同名表**存于 Neon，列名保留中文表头。
- `meta` 表记录 `data_date / source_file / rows / size_kb / loaded_at`，
  页面「数据日期」来自 `meta`（当前为 2026-09-28 快照）。
- 数据更新：重新执行 `python seed.py`（按「表名_日期最新」从 `artifacts/` 取源文件）。
- 连接串在 `.env`；代码会把 `channel_binding=require` 自动降级为 `disable`。

## Vercel 部署（手动执行）

```bash
npm i -g vercel                          # 或 npx vercel
cd website-yanliang-melon-forecast
vercel login
vercel                                   # 首次（预览）
vercel --prod                            # 生产
vercel env add DATABASE_URL              # 粘贴 .env 中的连接串（production/preview 各设一次）
```

- Vercel 按 Flask **框架预设**直接托管根目录 `app.py`（原生路径路由，无需 rewrite）；
  `public/figures/*.png` 与 `public/static/**` 由 CDN 直出。不要在 `vercel.json`
  里配 rewrite——内部重写会把 PATH_INFO 覆盖成目标路径，导致 404 JSON（历史故障根因）。
- 函数区域（新加坡 `sin1`，与 Neon 同区）：Dashboard → Settings → Functions →
  Serverless Region 选 `Singapore (sin1)`；函数依赖仅 5 个小包，冷启动轻。
- GitHub 导入备选：Root Directory 填 `website-yanliang-melon-forecast`，
  `DATABASE_URL` 在 Vercel 环境变量里设置（`.env` 已 gitignore 不上传）。

## 故障排查

| 现象 | 原因与处理 |
| --- | --- |
| 首页 KPI 显示「暂无预测结果」 | 表未灌库或连接失败，执行 `python seed.py` 看对账输出 |
| 500 + `缺少 DATABASE_URL` | 本地缺 `.env`，或 Vercel 未设置 `DATABASE_URL` |
| 图表 PNG 404 | `public/figures/` 未随部署上传，确认 52 张 PNG 在目录内 |
| 构建失败 | requirements 必须保持无 torch/matplotlib/statsmodels |
