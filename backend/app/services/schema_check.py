"""Schema校验领域服务：数据结构、字段类型、空值策略校验，在run_history确认完成和导出前触发。

在原有“按目标模块取规则”的 validate_record 之外，本服务新增按来源记录（站点）
执行的来源字段校验：
- 站点层：应用该来源站点录入时登记的专属规则；无专属规则时回落到共用定义。
- 共享层：始终应用共享数据集的公共定义（公共编号列只接收确定的整数）。
两层结论连同各自实际应用的规则与原值一起写入“站点字段校验回执”。
回执一经出具即不可变，规则后续更新只影响新回执；旧回执复查不会改动原结果。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services.base import BaseModuleService
from app import persistence as persist

# 共享规则/共享数据集归属标识：没有任何站点可以占用该归属。
SHARED_SCOPE = "shared"
# 站点层判定枚举
SITE_PASS = "站点通过"
SITE_BLOCKED = "站点拦截"
# 共享层判定枚举
SHARED_ACCEPTED = "确定整数可写入"
SHARED_PENDING = "待转换"
SHARED_BLOCKED = "共享拦截"


class SchemaCheckService(BaseModuleService):
    MODULE = "schema_check"
    ENTITY = "Schema规则"
    OPERATION_TERM = "校验"
    # 归属站点：专属规则登记其站点归属；共用规则取 SHARED_SCOPE。
    LIST_FIELDS = ["规则编号", "目标数据", "字段名", "类型约束", "格式约束", "空值策略", "异常归类", "归属站点", "规则版本"]
    REQUIRED_FIELDS = ["规则编号", "目标数据", "字段名"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    # ------------------------------------------------------------------
    # 旧口径：按目标模块取规则，保持原行为不变。
    # ------------------------------------------------------------------
    def validate_record(self, record: dict[str, Any], target_module: str) -> list[str]:
        """对单条记录执行所有已生效的Schema校验规则，返回违规列表。

        旧口径完整保留：int 约束只做 int(value) 可解析性检验，因此文本 "007"
        这类带前导零的字符串仍能通过整数格式检验。
        """
        rules = [r for r in store.rows(self.MODULE) if r.get("目标数据") == target_module and r.get("status") == "已生效"]
        violations: list[str] = []
        for rule in rules:
            field = rule.get("字段名", "")
            expected_type = rule.get("类型约束", "")
            format_constraint = rule.get("格式约束", "")
            null_policy = rule.get("空值策略", "")
            value = record.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                if null_policy == "禁止":
                    violations.append(f"字段{field}不允许为空（规则{rule.get('规则编号')}）")
                continue
            if expected_type == "int":
                try:
                    int(value)
                except (ValueError, TypeError):
                    violations.append(f"字段{field}期望int类型，实际为{type(value).__name__}")
            elif expected_type == "float":
                try:
                    float(value)
                except (ValueError, TypeError):
                    violations.append(f"字段{field}期望float类型")
            elif expected_type == "date" and format_constraint:
                if not isinstance(value, str) or len(value) < 8:
                    violations.append(f"字段{field}日期格式不符合{format_constraint}")
        if violations:
            persist.append_jsonl("reports", "schema_violations.jsonl", {
                "target_module": target_module,
                "record_summary": str(record)[:100],
                "violations": violations,
                "timestamp": persist.now_iso(),
            })
        return violations

    # ------------------------------------------------------------------
    # 规则登记：专属规则必须带站点归属，且只能由归属站点维护。
    # ------------------------------------------------------------------
    def register_rule(
        self,
        *,
        rule_code: str,
        target_data: str,
        field_name: str,
        type_constraint: str,
        owner_site: str,
        format_constraint: str = "",
        null_policy: str = "允许",
        error_category: str = "",
    ) -> dict[str, Any]:
        """登记一条已生效的来源校验规则。

        owner_site 为具体站点时是该站点的专属规则，其他站点无权维护；
        取 SHARED_SCOPE 时为所有站点共用的共享数据集定义。
        """
        values = {
            "规则编号": rule_code,
            "目标数据": target_data,
            "字段名": field_name,
            "类型约束": type_constraint,
            "格式约束": format_constraint,
            "空值策略": null_policy,
            "异常归类": error_category or f"{owner_site}-来源校验",
            "归属站点": owner_site,
            "规则版本": f"{rule_code}-v1",
        }
        entry, missing = self.create_entry(values)
        if missing:  # 理论上不会发生，留作防御
            raise ValueError(f"登记规则缺少必填字段：{'、'.join(missing)}")
        entry, _ = self.run_action(entry["id"], "标记生效")
        return entry

    def update_site_rule(self, rule_id: int, *, actor_site: str, changes: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """更新站点专属规则。

        仅归属站点自身可以更新；共享规则与其他站点的专属规则一律拒绝。
        更新只对之后出具的回执生效，历史回执保持不变。
        """
        rule = store.find(self.MODULE, rule_id)
        if rule is None:
            return None, f"规则 {rule_id} 不存在或已归档"
        owner = rule.get("归属站点", "")
        if owner == SHARED_SCOPE:
            return None, "共用定义由共享数据集维护，站点无权改动"
        if owner != actor_site:
            return None, f"规则归属站点为{owner}，{actor_site}无权占用该归属进行修改"
        editable = {"类型约束", "格式约束", "空值策略", "异常归类"}
        for key, value in changes.items():
            if key in editable:
                rule[key] = value
        # 版本推进，便于回执区分规则快照与现行规则。
        rule["规则版本"] = self._bump_version(rule.get("规则版本", f"RULE-{rule_id}"))
        rule["updated_at"] = persist.now_iso()
        return rule, f"规则{rule.get('规则编号')}已更新至{rule['规则版本']}"

    @staticmethod
    def _bump_version(version: str) -> str:
        if "-v" in version:
            head, _, tail = version.rpartition("-v")
            if tail.isdigit():
                return f"{head}-v{int(tail) + 1}"
        return f"{version}-v2"

    # ------------------------------------------------------------------
    # 来源字段校验：站点层 + 共享层，两层结论写入同一份回执。
    # ------------------------------------------------------------------
    def _active_rules(self, target_data: str, field_name: str) -> list[dict[str, Any]]:
        return [
            r for r in store.rows(self.MODULE)
            if r.get("目标数据") == target_data
            and r.get("字段名") == field_name
            and r.get("status") == "已生效"
        ]

    def _pick_rule(self, rules: list[dict[str, Any]], owner: str) -> dict[str, Any] | None:
        """取归属为 owner 的规则；站点专属缺失时回落到共享共用定义。"""
        owned = [r for r in rules if r.get("归属站点") == owner]
        if owned:
            return max(owned, key=lambda r: int(r.get("id", 0)))
        shared = [r for r in rules if r.get("归属站点") == SHARED_SCOPE]
        if shared:
            return max(shared, key=lambda r: int(r.get("id", 0)))
        return None

    @staticmethod
    def _leading_zeros(raw_text: str) -> str:
        stripped = raw_text.lstrip("-")
        zeros = len(stripped) - len(stripped.lstrip("0"))
        return "0" * zeros

    def _apply_rule(self, rule: dict[str, Any] | None, value: Any) -> dict[str, Any]:
        """按单条规则评估原值，返回结论、原值保留信息与违规原因。"""
        result: dict[str, Any] = {
            "rule": self._rule_snapshot(rule),
            "passed": False,
            "decision": SITE_BLOCKED,
            "reason": "",
            "raw_value": value,
            "raw_type": type(value).__name__,
            "leading_zeros": "",
        }
        if rule is None:
            result["reason"] = "无适用规则"
            return result
        if value is None or (isinstance(value, str) and not str(value).strip()):
            if rule.get("空值策略") == "禁止":
                result["reason"] = f"字段{rule.get('字段名')}不允许为空"
                return result
            result.update(passed=True, decision=SITE_PASS, reason="空值且空值策略为允许")
            return result

        expected = rule.get("类型约束", "")
        if expected == "int":
            # 旧口径：可被 int() 解析即通过整数格式检验（文本 007 仍通过）。
            try:
                int(value)
            except (ValueError, TypeError):
                result["reason"] = f"原值{value!r}无法按整数格式解析"
                return result
        elif expected == "str":
            if not isinstance(value, str):
                result["reason"] = f"期望文本类型，实际为{type(value).__name__}"
                return result
        elif expected == "float":
            try:
                float(value)
            except (ValueError, TypeError):
                result["reason"] = f"原值{value!r}无法按浮点格式解析"
                return result
        result["passed"] = True
        result["decision"] = SITE_PASS
        result["reason"] = f"按规则{rule.get('规则编号')}（{expected or '未限定类型'}）检验通过"
        if isinstance(value, str):
            result["leading_zeros"] = self._leading_zeros(value)
        return result

    @staticmethod
    def _rule_snapshot(rule: dict[str, Any] | None) -> dict[str, Any] | None:
        if rule is None:
            return None
        return {key: rule.get(key) for key in (
            "规则编号", "目标数据", "字段名", "类型约束", "格式约束",
            "空值策略", "异常归类", "归属站点", "规则版本",
        )}

    def _shared_decision(self, shared_rule: dict[str, Any] | None, value: Any) -> dict[str, Any]:
        """共享层结论：公共编号列只接收确定的整数数据。

        - 真正的 int（非 bool）：确定整数，可直接写入公共编号列；
        - 可解析为整数的文本（如 "007"）：判定前保留原值与前导零，列为待转换，
          在转换确认前不得进入公共编号列；
        - 不可解析：共享拦截。
        """
        result: dict[str, Any] = {
            "rule": self._rule_snapshot(shared_rule),
            "decision": SHARED_BLOCKED,
            "passed": False,
            "raw_value": value,
            "raw_type": type(value).__name__,
            "leading_zeros": "",
            "converted_value": None,
            "reason": "",
        }
        if shared_rule is None:
            result["reason"] = "共享数据集缺少公共编号定义"
            return result
        if value is None or (isinstance(value, str) and not str(value).strip()):
            result["reason"] = "公共编号列不接收空值"
            return result
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, str):
                try:
                    parsed = int(value.strip())
                except (ValueError, TypeError):
                    result["reason"] = f"文本原值{value!r}无法转换为整数，公共编号列拦截"
                    return result
                result.update(
                    decision=SHARED_PENDING,
                    leading_zeros=self._leading_zeros(value.strip()),
                    converted_value=parsed,
                    reason="文本原值可解析为整数，须经转换确认后方可写入公共编号列",
                )
                return result
            result["reason"] = f"公共编号列只接收整数，实际为{type(value).__name__}"
            return result
        result.update(
            decision=SHARED_ACCEPTED,
            passed=True,
            converted_value=value,
            reason="原值为确定整数，可直接写入公共编号列",
        )
        return result

    @staticmethod
    def _receipt_filename(source_site: str, source_record_id: str, field_name: str) -> str:
        safe_site = "".join(ch for ch in source_site if ch.isalnum() or ch in ("-", "_"))
        safe_id = "".join(ch for ch in str(source_record_id) if ch.isalnum() or ch in ("-", "_"))
        safe_field = "".join(ch for ch in field_name if ch.isalnum() or ch in ("-", "_"))
        return f"{safe_site}__{safe_id}__{safe_field}.json"

    def issue_source_receipt(
        self,
        *,
        source_site: str,
        source_record_id: str,
        target_data: str,
        field_name: str,
        raw_value: Any,
    ) -> dict[str, Any]:
        """出具站点字段校验回执；相同来源重送幂等，不重复产出。"""
        filename = self._receipt_filename(source_site, source_record_id, field_name)
        existing = persist.read_json("receipts", filename)
        if existing is not None:
            existing["idempotent_replay"] = True
            return existing

        rules = self._active_rules(target_data, field_name)
        site_rule = self._pick_rule(rules, source_site)
        shared_rule = self._pick_rule(rules, SHARED_SCOPE)
        site_layer = self._apply_rule(site_rule, raw_value)
        shared_layer = self._shared_decision(shared_rule, raw_value)

        receipt = {
            "receipt_id": f"RCP-{source_site}-{source_record_id}-{field_name}",
            "source_site": source_site,
            "source_record_id": str(source_record_id),
            "target_data": target_data,
            "field_name": field_name,
            "raw_value": raw_value,
            "raw_type": type(raw_value).__name__,
            "leading_zeros": shared_layer["leading_zeros"],
            "site_layer": site_layer,
            "shared_layer": shared_layer,
            "site_rule_owner": (site_rule or {}).get("归属站点") if site_rule else None,
            "issued_at": persist.now_iso(),
            "rule_versions": {
                "site_rule": (site_rule or {}).get("规则版本") if site_rule else None,
                "shared_rule": (shared_rule or {}).get("规则版本") if shared_rule else None,
            },
            "immutable": True,
        }
        persist.write_json("receipts", filename, receipt)
        return receipt

    def get_source_receipt(self, source_site: str, source_record_id: str, field_name: str) -> dict[str, Any] | None:
        return persist.read_json(
            "receipts", self._receipt_filename(source_site, source_record_id, field_name))

    def recheck_source_receipt(
        self,
        *,
        source_site: str,
        source_record_id: str,
        target_data: str,
        field_name: str,
        raw_value: Any,
    ) -> dict[str, Any]:
        """复查回执：返回不可变的原回执，并附按现行规则重新评估的结果。

        原回执不会被覆写；若现行规则已更新，current_eval 会与历史结论不同，
        但历史结论保持出具时的结果。
        """
        stored = self.get_source_receipt(source_site, source_record_id, field_name)
        rules = self._active_rules(target_data, field_name)
        current_site = self._apply_rule(self._pick_rule(rules, source_site), raw_value)
        current_shared = self._shared_decision(self._pick_rule(rules, SHARED_SCOPE), raw_value)
        return {
            "stored_receipt": stored,
            "stored_unchanged": True,
            "current_eval": {
                "site_layer": current_site,
                "shared_layer": current_shared,
                "evaluated_at": persist.now_iso(),
            },
        }

    # ------------------------------------------------------------------
    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        # Schema违规统计
        violations = persist.read_jsonl("reports", "schema_violations.jsonl")
        base["total_violations"] = len(violations)
        # 来源校验回执统计（站点字段校验回执）
        receipt_files = persist.list_files("receipts", ".json")
        base["source_receipts"] = len(receipt_files)
        rows = store.rows(self.MODULE)
        base["site_specific_rules"] = sum(
            1 for r in rows if r.get("归属站点") and r.get("归属站点") != SHARED_SCOPE)
        base["shared_rules"] = sum(1 for r in rows if r.get("归属站点") == SHARED_SCOPE)
        # 关联的质量防护规则
        guards = store.rows("quality_guard")
        base["linked_quality_rules"] = sum(1 for g in guards if g.get("status") == "已生效")
        # 关联的导出通道
        channels = store.rows("export_channel")
        base["linked_export_channels"] = len(channels)
        return base
