"""来源校验采集路由：站点字段校验回执、采集分流、受控共享写入。

与通用资源路由分离，路径统一挂在 /api/ingestion 下，避免与 /api/{module} 动态路由冲突。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.data_extract import DataExtractService
from app.services.schema_check import SchemaCheckService, SHARED_SCOPE

router = APIRouter(prefix="/api/ingestion", tags=["来源校验采集"])


class RuleRegisterPayload(BaseModel):
    rule_code: str
    target_data: str
    field_name: str
    type_constraint: str
    owner_site: str = Field(description="站点标识为专属规则；传 shared 登记共用定义")
    format_constraint: str = ""
    null_policy: str = "允许"
    error_category: str = ""


class RuleUpdatePayload(BaseModel):
    actor_site: str
    changes: dict[str, Any] = Field(default_factory=dict)


class SourceIngestPayload(BaseModel):
    source_site: str
    source_record_id: str
    target_data: str
    field_name: str
    raw_value: Any = None


class ConversionPayload(BaseModel):
    actor_site: str
    source_site: str
    source_record_id: str
    field_name: str


class SharedWritePayload(BaseModel):
    actor_site: str
    receipt_id: str


class RecheckPayload(SourceIngestPayload):
    pass


@router.post("/rules")
def register_rule(payload: RuleRegisterPayload) -> dict[str, Any]:
    svc = SchemaCheckService()
    entry = svc.register_rule(
        rule_code=payload.rule_code,
        target_data=payload.target_data,
        field_name=payload.field_name,
        type_constraint=payload.type_constraint,
        owner_site=payload.owner_site,
        format_constraint=payload.format_constraint,
        null_policy=payload.null_policy,
        error_category=payload.error_category,
    )
    return {"ok": True, "message": "来源校验规则已登记并生效", "entry": entry}


@router.post("/rules/{rule_id}")
def update_rule(rule_id: int, payload: RuleUpdatePayload) -> dict[str, Any]:
    svc = SchemaCheckService()
    entry, message = svc.update_site_rule(rule_id, actor_site=payload.actor_site, changes=payload.changes)
    return {"ok": entry is not None, "message": message, "entry": entry}


@router.post("/receipts")
def issue_receipt(payload: SourceIngestPayload) -> dict[str, Any]:
    """仅出具站点字段校验回执（两层结论），不产生采集行。"""
    svc = SchemaCheckService()
    receipt = svc.issue_source_receipt(
        source_site=payload.source_site,
        source_record_id=payload.source_record_id,
        target_data=payload.target_data,
        field_name=payload.field_name,
        raw_value=payload.raw_value,
    )
    return {"ok": True, "receipt": receipt}


@router.post("/receipts/recheck")
def recheck_receipt(payload: RecheckPayload) -> dict[str, Any]:
    """复查旧回执：原结果不变，另附按现行规则重新评估的结论。"""
    svc = SchemaCheckService()
    return {"ok": True, "result": svc.recheck_source_receipt(
        source_site=payload.source_site,
        source_record_id=payload.source_record_id,
        target_data=payload.target_data,
        field_name=payload.field_name,
        raw_value=payload.raw_value,
    )}


@router.post("/sources")
def ingest_source(payload: SourceIngestPayload) -> dict[str, Any]:
    """采集来源：出具回执并分流为站点结构化行 / 待转换来源 / 拦截记录。"""
    svc = DataExtractService()
    outcome = svc.ingest_source(
        source_site=payload.source_site,
        source_record_id=payload.source_record_id,
        target_data=payload.target_data,
        field_name=payload.field_name,
        raw_value=payload.raw_value,
    )
    return {"ok": True, "outcome": outcome}


@router.post("/conversions/confirm")
def confirm_conversion(payload: ConversionPayload) -> dict[str, Any]:
    svc = DataExtractService()
    pending, message = svc.confirm_conversion(
        actor_site=payload.actor_site,
        source_site=payload.source_site,
        source_record_id=payload.source_record_id,
        field_name=payload.field_name,
    )
    return {"ok": pending is not None, "message": message, "pending": pending}


@router.post("/shared-writes")
def write_shared(payload: SharedWritePayload) -> dict[str, Any]:
    """受控的共享数据集写入：校验回执归属与共享层结论。"""
    svc = DataExtractService()
    row, message = svc.write_shared_dataset(actor_site=payload.actor_site, receipt_id=payload.receipt_id)
    return {"ok": row is not None, "message": message, "row": row}


@router.get("/views/local-rows")
def local_rows(site: str | None = None) -> dict[str, Any]:
    svc = DataExtractService()
    items = svc.local_accepted_rows(site)
    return {"items": items, "total": len(items)}


@router.get("/views/shared-rows")
def shared_rows(site: str | None = None) -> dict[str, Any]:
    svc = DataExtractService()
    items = svc.shared_available_rows(site)
    return {"items": items, "total": len(items)}


@router.get("/views/pending")
def pending_rows(site: str | None = None) -> dict[str, Any]:
    svc = DataExtractService()
    items = svc.pending_conversions(site)
    return {"items": items, "total": len(items)}


@router.get("/views/interceptions")
def interceptions() -> dict[str, Any]:
    svc = DataExtractService()
    items = svc.intercepted_values()
    return {"items": items, "total": len(items)}
