"""数据抽取领域服务：消费behavior_script脚本和element_selector锚点，执行字段抽取。

新增来源校验驱动的采集入口 ingest_source：
- Schema 校验先按来源站点出具两层结论的站点字段校验回执；
- 站点层通过的来源交付为站点结构化行（本地可接收行）；
- 共享层只允许“确定整数”进入受控的共享数据集写入范围；
- 可解析但仍是文本的原值列为待转换来源，转换确认前不得写入公共编号列；
- 站点层拦截、共享写入越权等情况记入拦截清单（保留原值与适用规则）；
- 相同来源重送幂等，不重复增加产出行。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services.base import BaseModuleService
from app.services.schema_check import (
    SHARED_SCOPE,
    SHARED_ACCEPTED,
    SHARED_BLOCKED,
    SHARED_PENDING,
    SITE_BLOCKED,
)
from app import persistence as persist

# 内存中的采集产物表（仅承载运行期结构化结果，权威审计落 JSONL/JSON）。
INGESTION_TABLE = "ingestion_structured_rows"
PENDING_TABLE = "ingestion_pending_conversions"
SHARED_TABLE = "shared_dataset_rows"
INTERCEPT_LOG = "ingestion_interceptions.jsonl"
CONVERSION_LOG = "conversion_confirmations.jsonl"
SHARED_WRITE_LOG = "shared_dataset_writes.jsonl"


class DataExtractService(BaseModuleService):
    MODULE = "data_extract"
    ENTITY = "抽取模板"
    OPERATION_TERM = "解析"
    LIST_FIELDS = ["模板编号", "关联脚本", "抽取字段", "提取方式", "数据处理", "输出类型", "默认值", "模板版本"]
    REQUIRED_FIELDS = ["模板编号", "关联脚本", "抽取字段"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用模板": "已停用"}
    NEGATIVE_ACTIONS = ["停用模板"]

    # ------------------------------------------------------------------
    def _schema_service(self):
        from app.services.schema_check import SchemaCheckService
        return SchemaCheckService()

    def _log_interception(self, payload: dict[str, Any]) -> None:
        payload.setdefault("timestamp", persist.now_iso())
        persist.append_jsonl("reports", INTERCEPT_LOG, payload)

    def _find_pending(self, source_site: str, source_record_id: str, field_name: str) -> dict[str, Any] | None:
        for row in store.rows(PENDING_TABLE):
            if (row["source_site"] == source_site
                    and row["source_record_id"] == str(source_record_id)
                    and row["field_name"] == field_name):
                return row
        return None

    # ------------------------------------------------------------------
    def ingest_source(
        self,
        *,
        source_site: str,
        source_record_id: str,
        target_data: str,
        field_name: str,
        raw_value: Any,
    ) -> dict[str, Any]:
        """采集一个来源字段：先取站点字段校验回执，再按两层结论分流。"""
        if source_site == SHARED_SCOPE:
            raise ValueError("来源站点不能占用共享归属")
        schema_svc = self._schema_service()
        receipt = schema_svc.issue_source_receipt(
            source_site=source_site,
            source_record_id=source_record_id,
            target_data=target_data,
            field_name=field_name,
            raw_value=raw_value,
        )
        replay = receipt.get("idempotent_replay", False)
        site_layer = receipt["site_layer"]
        shared_layer = receipt["shared_layer"]

        outcome: dict[str, Any] = {
            "receipt_id": receipt["receipt_id"],
            "source_site": source_site,
            "source_record_id": str(source_record_id),
            "raw_value": raw_value,
            "idempotent_replay": replay,
            "site_decision": site_layer["decision"],
            "shared_decision": shared_layer["decision"],
        }

        # 站点层未通过：不产生本地可接收行，记录拦截原值与适用规则。
        if site_layer["decision"] == SITE_BLOCKED:
            self._log_interception({
                "stage": "site_layer",
                "source_site": source_site,
                "source_record_id": str(source_record_id),
                "field_name": field_name,
                "raw_value": raw_value,
                "applied_rule": site_layer["rule"],
                "reason": site_layer["reason"],
            })
            outcome.update(structured_row=None, blocked=True, reason=site_layer["reason"])
            return outcome

        # 站点层通过 → 站点结构化行（本地可接收行），相同来源重送不增加产出。
        row_key = (source_site, str(source_record_id), field_name)
        existing_row = next(
            (r for r in store.rows(INGESTION_TABLE)
             if (r["source_site"], r["source_record_id"], r["field_name"]) == row_key),
            None,
        )
        if existing_row is None:
            structured_row = {
                "id": len(store.rows(INGESTION_TABLE)) + 1,
                "source_site": source_site,
                "source_record_id": str(source_record_id),
                "target_data": target_data,
                "field_name": field_name,
                "raw_value": raw_value,
                "raw_type": receipt["raw_type"],
                "leading_zeros": receipt["leading_zeros"],
                "site_rule": site_layer["rule"],
                "shared_rule": shared_layer["rule"],
                "shared_decision": shared_layer["decision"],
                "receipt_id": receipt["receipt_id"],
                "ingested_at": persist.now_iso(),
            }
            store.rows(INGESTION_TABLE).append(structured_row)
            outcome["structured_row"] = structured_row
        else:
            outcome["structured_row"] = existing_row

        # 共享层分流。
        if shared_layer["decision"] == SHARED_ACCEPTED:
            outcome["blocked"] = False
            outcome["reason"] = "已获得受控共享数据集写入范围"
        elif shared_layer["decision"] == SHARED_PENDING:
            # 待转换来源：保留原值与前导零，转换确认前不进入公共编号列。
            if self._find_pending(source_site, source_record_id, field_name) is None:
                store.rows(PENDING_TABLE).append({
                    "source_site": source_site,
                    "source_record_id": str(source_record_id),
                    "target_data": target_data,
                    "field_name": field_name,
                    "raw_value": raw_value,
                    "leading_zeros": shared_layer["leading_zeros"],
                    "converted_value": shared_layer["converted_value"],
                    "applied_rule": shared_layer["rule"],
                    "receipt_id": receipt["receipt_id"],
                    "confirmed": False,
                    "registered_at": persist.now_iso(),
                })
            self._log_interception({
                "stage": "shared_pending_conversion",
                "source_site": source_site,
                "source_record_id": str(source_record_id),
                "field_name": field_name,
                "raw_value": raw_value,
                "leading_zeros": shared_layer["leading_zeros"],
                "applied_rule": shared_layer["rule"],
                "reason": shared_layer["reason"],
            })
            outcome.update(blocked=True, reason=shared_layer["reason"])
        else:
            self._log_interception({
                "stage": "shared_layer",
                "source_site": source_site,
                "source_record_id": str(source_record_id),
                "field_name": field_name,
                "raw_value": raw_value,
                "applied_rule": shared_layer["rule"],
                "reason": shared_layer["reason"],
            })
            outcome.update(blocked=True, reason=shared_layer["reason"])
        return outcome

    # ------------------------------------------------------------------
    def confirm_conversion(
        self,
        *,
        actor_site: str,
        source_site: str,
        source_record_id: str,
        field_name: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """确认待转换文本：仅归属站点可确认；确认不改动原回执，只追加确认登记。"""
        if actor_site != source_site:
            return None, f"待转换来源归属{source_site}，{actor_site}无权确认"
        pending = self._find_pending(source_site, source_record_id, field_name)
        if pending is None:
            return None, "未找到该来源的待转换记录"
        if pending["confirmed"]:
            return pending, "该来源已完成转换确认，重复确认不产生新登记"
        pending["confirmed"] = True
        pending["confirmed_at"] = persist.now_iso()
        record = {
            "source_site": source_site,
            "source_record_id": str(source_record_id),
            "field_name": field_name,
            "raw_value": pending["raw_value"],
            "leading_zeros": pending["leading_zeros"],
            "converted_value": pending["converted_value"],
            "receipt_id": pending["receipt_id"],
            "confirmed_by": actor_site,
            "timestamp": persist.now_iso(),
        }
        persist.append_jsonl("reports", CONVERSION_LOG, record)
        return pending, f"原值{pending['raw_value']!r}确认转换为整数{pending['converted_value']}"

    # ------------------------------------------------------------------
    def write_shared_dataset(
        self,
        *,
        actor_site: str,
        receipt_id: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """按回执向共享数据集写入；写入范围受控，其他站点无权占用归属。"""
        receipt = self._find_receipt_by_id(receipt_id)
        if receipt is None:
            self._log_interception({
                "stage": "shared_write",
                "actor_site": actor_site,
                "receipt_id": receipt_id,
                "raw_value": None,
                "applied_rule": None,
                "reason": "回执不存在",
            })
            return None, f"回执{receipt_id}不存在"

        owner = receipt["source_site"]
        if actor_site != owner:
            self._log_interception({
                "stage": "shared_write_permission",
                "actor_site": actor_site,
                "source_site": owner,
                "source_record_id": receipt["source_record_id"],
                "receipt_id": receipt_id,
                "raw_value": receipt["raw_value"],
                "applied_rule": receipt["shared_layer"]["rule"],
                "reason": f"{actor_site}借用{owner}的回执提交共享写入，归属不匹配",
            })
            return None, f"回执归属{owner}，{actor_site}无权占用该共享写入范围"

        shared_layer = receipt["shared_layer"]
        if shared_layer["decision"] == SHARED_BLOCKED:
            self._log_interception({
                "stage": "shared_write_blocked",
                "actor_site": actor_site,
                "source_record_id": receipt["source_record_id"],
                "receipt_id": receipt_id,
                "raw_value": receipt["raw_value"],
                "applied_rule": shared_layer["rule"],
                "reason": shared_layer["reason"],
            })
            return None, f"共享层拦截：{shared_layer['reason']}"

        if shared_layer["decision"] == SHARED_PENDING:
            pending = self._find_pending(owner, receipt["source_record_id"], receipt["field_name"])
            if pending is None or not pending.get("confirmed"):
                self._log_interception({
                    "stage": "shared_write_unconfirmed",
                    "actor_site": actor_site,
                    "source_record_id": receipt["source_record_id"],
                    "receipt_id": receipt_id,
                    "raw_value": receipt["raw_value"],
                    "applied_rule": shared_layer["rule"],
                    "reason": "文本原值尚未完成转换确认，不得进入公共编号列",
                })
                return None, "待转换文本在转换确认前不能进入公共编号列"
            public_value = pending["converted_value"]
        else:
            public_value = shared_layer["converted_value"]

        # 幂等：同一回执只写一次，相同来源重送不增加产出行。
        for row in store.rows(SHARED_TABLE):
            if row["receipt_id"] == receipt_id:
                return row, "该回执已写入共享数据集，重复提交不增加产出行"

        row = {
            "id": len(store.rows(SHARED_TABLE)) + 1,
            "公共编号": public_value,
            "source_site": owner,
            "source_record_id": receipt["source_record_id"],
            "field_name": receipt["field_name"],
            "receipt_id": receipt_id,
            "applied_rule": shared_layer["rule"],
            "written_by": actor_site,
            "written_at": persist.now_iso(),
        }
        store.rows(SHARED_TABLE).append(row)
        persist.append_jsonl("reports", SHARED_WRITE_LOG, row)
        return row, f"公共编号列已写入整数{public_value}"

    def _find_receipt_by_id(self, receipt_id: str) -> dict[str, Any] | None:
        for name in persist.list_files("receipts", ".json"):
            receipt = persist.read_json("receipts", name)
            if receipt and receipt.get("receipt_id") == receipt_id:
                return receipt
        return None

    # ------------------------------------------------------------------
    # 查询视图：本地可接收行 / 共享可用行 / 待转换来源 / 拦截原值。
    # ------------------------------------------------------------------
    def local_accepted_rows(self, site: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(INGESTION_TABLE)
        return [r for r in rows if site is None or r["source_site"] == site]

    def shared_available_rows(self, site: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(SHARED_TABLE)
        return [r for r in rows if site is None or r["source_site"] == site]

    def pending_conversions(self, site: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(PENDING_TABLE)
        return [r for r in rows if site is None or r["source_site"] == site]

    def intercepted_values(self) -> list[dict[str, Any]]:
        return persist.read_jsonl("reports", INTERCEPT_LOG)

    def extract_fields_for_script(self, script_id: str) -> list[dict[str, Any]]:
        """根据脚本编号查询所有关联的抽取模板并返回字段映射。"""
        templates = store.rows(self.MODULE)
        linked = [t for t in templates if t.get("关联脚本") == script_id and t.get("status") == "已生效"]
        return [{"field": t.get("抽取字段"), "method": t.get("提取方式"), "output_type": t.get("输出类型")} for t in linked]

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        templates = store.rows(self.MODULE)
        # 关联的元素锚点统计
        selectors = store.rows("element_selector")
        base["templates_with_selectors"] = sum(
            1 for t in templates if any(
                s.get("关联脚本") == t.get("关联脚本") for s in selectors))
        # 关联的清洗管道
        pipelines = store.rows("clean_pipeline")
        base["templates_in_pipeline"] = sum(
            1 for t in templates if any(
                p.get("输入源") and t.get("关联脚本") in str(p.get("输入源", "")) for p in pipelines))
        # 来源校验采集产物
        base["local_accepted_rows"] = len(store.rows(INGESTION_TABLE))
        base["shared_available_rows"] = len(store.rows(SHARED_TABLE))
        base["pending_conversions"] = len(store.rows(PENDING_TABLE))
        base["interceptions"] = len(self.intercepted_values())
        return base
