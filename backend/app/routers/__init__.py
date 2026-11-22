"""业务路由汇总：来源校验采集路由先注册，通用模块资源路由兜底。"""
from __future__ import annotations

from app.routers import ingestion, resource

ROUTERS = [ingestion, resource]
