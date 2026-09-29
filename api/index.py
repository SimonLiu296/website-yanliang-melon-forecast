# Vercel Python 函数入口：打包根目录 app.py 的 Flask 实例。
# 文件系统路由（public/**）优先于 rewrite，静态由 CDN 直出。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import app  # noqa: E402,F401
