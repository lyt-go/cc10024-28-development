"""来源字段校验端到端测试：站点层/共享层两层结论、回执、采集分流与受控共享写入。

场景（题述）：
- 甲站登记专属规则：产品编号按文本（str）录入；共享数据集公共编号列沿用整数定义。
- 乙站没有专属规则，继续按共用定义（int）采集。
- 甲送文本 "007"、乙送整数 7、乙再以自身来源送文本 "007"，并尝试借用甲的回执写共享。
- 旧口径 validate_record 保留：文本 "007" 仍通过整数格式检验。
- 公共编号列只接收确定整数；待转换文本在转换确认前不得进入。
- 相同来源重送幂等；回执不可变；规则更新后复查旧回执不改原结果。
"""
from __future__ import annotations

import os
import sys

backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app import persistence as persist

# 测试隔离：清空运行期产物目录（含本特性新增的 receipts）。
for sub in ["runs", "snapshots", "reports", "exports", "receipts"]:
    p = persist.data_path(sub, "")
    if os.path.isdir(p):
        for f in os.listdir(p):
            os.remove(os.path.join(p, f))

from app.store import store
from app.services.schema_check import (
    SchemaCheckService,
    SHARED_SCOPE,
    SHARED_ACCEPTED,
    SHARED_PENDING,
    SHARED_BLOCKED,
    SITE_PASS,
    SITE_BLOCKED,
)
from app.services.data_extract import (
    DataExtractService,
    INGESTION_TABLE,
    PENDING_TABLE,
    SHARED_TABLE,
)

TARGET = "product_catalog"
FIELD = "产品编号"
SITE_A = "site_a"
SITE_B = "site_b"


def _register_rules() -> None:
    svc = SchemaCheckService()
    svc.register_rule(
        rule_code="T-SCHE-A-001", target_data=TARGET, field_name=FIELD,
        type_constraint="str", owner_site=SITE_A,
        format_constraint="保留前导零文本", error_category="甲站来源校验",
    )
    svc.register_rule(
        rule_code="T-SCHE-SHARED-001", target_data=TARGET, field_name=FIELD,
        type_constraint="int", owner_site=SHARED_SCOPE,
        format_constraint="公共编号列仅接收确定整数", error_category="共享数据集校验",
    )


def test_01_rules_registered_with_site_ownership():
    _register_rules()
    svc = SchemaCheckService()
    rules = svc._active_rules(TARGET, FIELD)
    owners = {r["归属站点"]: r for r in rules}
    assert SITE_A in owners and SHARED_SCOPE in owners
    # 乙站没有专属规则：为乙站选规则时必须回落到共用定义。
    picked_for_b = svc._pick_rule(rules, SITE_B)
    assert picked_for_b["归属站点"] == SHARED_SCOPE
    picked_for_a = svc._pick_rule(rules, SITE_A)
    assert picked_for_a["归属站点"] == SITE_A


def test_02_legacy_validation_still_accepts_text_007():
    """旧口径保留：int 规则下文本 007 仍通过整数格式检验。"""
    svc = SchemaCheckService()
    violations = svc.validate_record({FIELD: "007"}, TARGET)
    assert violations == [], f"旧口径应放行文本007，实际违规：{violations}"


def test_03_site_a_text_007_receipt_two_layers():
    schema_svc = SchemaCheckService()
    receipt = schema_svc.issue_source_receipt(
        source_site=SITE_A, source_record_id="rec-a-007",
        target_data=TARGET, field_name=FIELD, raw_value="007",
    )
    # 站点层：甲站专属 str 规则，文本通过。
    assert receipt["site_layer"]["decision"] == SITE_PASS
    assert receipt["site_layer"]["rule"]["归属站点"] == SITE_A
    assert receipt["site_layer"]["rule"]["类型约束"] == "str"
    # 共享层：文本可解析但非确定整数 → 待转换；原值与前导零保留。
    assert receipt["shared_layer"]["decision"] == SHARED_PENDING
    assert receipt["raw_value"] == "007"
    assert receipt["leading_zeros"] == "00"
    assert receipt["shared_layer"]["converted_value"] == 7
    assert receipt["shared_layer"]["rule"]["归属站点"] == SHARED_SCOPE


def test_04_site_b_int_7_receipt_accepted():
    schema_svc = SchemaCheckService()
    receipt = schema_svc.issue_source_receipt(
        source_site=SITE_B, source_record_id="rec-b-007",
        target_data=TARGET, field_name=FIELD, raw_value=7,
    )
    # 乙站无专属规则，站点层应用共用 int 定义。
    assert receipt["site_layer"]["decision"] == SITE_PASS
    assert receipt["site_layer"]["rule"]["归属站点"] == SHARED_SCOPE
    assert receipt["shared_layer"]["decision"] == SHARED_ACCEPTED
    assert receipt["shared_layer"]["converted_value"] == 7


def test_05_site_b_text_007_legacy_pass_but_pending_for_shared():
    schema_svc = SchemaCheckService()
    receipt = schema_svc.issue_source_receipt(
        source_site=SITE_B, source_record_id="rec-b-text007",
        target_data=TARGET, field_name=FIELD, raw_value="007",
    )
    # 乙站沿用共用 int 定义，旧口径 int("007") 可解析 → 站点层仍通过。
    assert receipt["site_layer"]["decision"] == SITE_PASS
    assert receipt["site_layer"]["rule"]["归属站点"] == SHARED_SCOPE
    # 公共编号列：文本只能待转换，且前导零保留。
    assert receipt["shared_layer"]["decision"] == SHARED_PENDING
    assert receipt["leading_zeros"] == "00"


def test_06_ingest_three_sources_and_views():
    extract_svc = DataExtractService()
    out_a = extract_svc.ingest_source(
        source_site=SITE_A, source_record_id="rec-a-007",
        target_data=TARGET, field_name=FIELD, raw_value="007")
    out_b_int = extract_svc.ingest_source(
        source_site=SITE_B, source_record_id="rec-b-007",
        target_data=TARGET, field_name=FIELD, raw_value=7)
    out_b_text = extract_svc.ingest_source(
        source_site=SITE_B, source_record_id="rec-b-text007",
        target_data=TARGET, field_name=FIELD, raw_value="007")

    # 三个来源站点层都通过 → 本地可接收行各 1 行（甲1、乙2）。
    assert out_a["structured_row"] is not None
    assert out_b_int["structured_row"] is not None
    assert out_b_text["structured_row"] is not None
    local = extract_svc.local_accepted_rows()
    assert len(local) == 3
    assert len(extract_svc.local_accepted_rows(SITE_A)) == 1
    assert len(extract_svc.local_accepted_rows(SITE_B)) == 2

    # 待转换来源：甲文本007、乙文本007；乙整数7不在其中。
    pending = extract_svc.pending_conversions()
    pending_keys = {(r["source_site"], r["source_record_id"]) for r in pending}
    assert pending_keys == {(SITE_A, "rec-a-007"), (SITE_B, "rec-b-text007")}
    for row in pending:
        assert row["raw_value"] == "007"
        assert row["leading_zeros"] == "00"
        assert row["confirmed"] is False

    # 共享数据集此时还没有任何行。
    assert extract_svc.shared_available_rows() == []


def test_07_idempotent_reingest_does_not_duplicate():
    extract_svc = DataExtractService()
    before_local = len(extract_svc.local_accepted_rows())
    before_pending = len(extract_svc.pending_conversions())
    replay = extract_svc.ingest_source(
        source_site=SITE_A, source_record_id="rec-a-007",
        target_data=TARGET, field_name=FIELD, raw_value="007")
    assert replay["idempotent_replay"] is True
    assert len(extract_svc.local_accepted_rows()) == before_local
    assert len(extract_svc.pending_conversions()) == before_pending


def test_08_shared_write_success_for_confirmed_integer():
    extract_svc = DataExtractService()
    receipt_b = SchemaCheckService().get_source_receipt(SITE_B, "rec-b-007", FIELD)
    row, message = extract_svc.write_shared_dataset(
        actor_site=SITE_B, receipt_id=receipt_b["receipt_id"])
    assert row is not None, message
    assert row["公共编号"] == 7 and isinstance(row["公共编号"], int)
    # 重复提交不增加产出行。
    again, _ = extract_svc.write_shared_dataset(
        actor_site=SITE_B, receipt_id=receipt_b["receipt_id"])
    assert again["id"] == row["id"]
    shared = extract_svc.shared_available_rows()
    assert len(shared) == 1


def test_09_borrowed_receipt_shared_write_fails():
    """乙借用甲的回执提交共享写入：归属不匹配，必须失败且不产出行。"""
    extract_svc = DataExtractService()
    receipt_a = SchemaCheckService().get_source_receipt(SITE_A, "rec-a-007", FIELD)
    shared_before = len(extract_svc.shared_available_rows())
    row, message = extract_svc.write_shared_dataset(
        actor_site=SITE_B, receipt_id=receipt_a["receipt_id"])
    assert row is None
    assert "无权" in message
    assert len(extract_svc.shared_available_rows()) == shared_before

    # 甲自己写也不行：文本 007 尚未转换确认。
    row2, message2 = extract_svc.write_shared_dataset(
        actor_site=SITE_A, receipt_id=receipt_a["receipt_id"])
    assert row2 is None
    assert "转换确认" in message2


def test_10_pending_text_blocked_until_owner_confirms():
    extract_svc = DataExtractService()
    receipt_b_text = SchemaCheckService().get_source_receipt(SITE_B, "rec-b-text007", FIELD)
    # 甲不能替乙确认。
    pending, message = extract_svc.confirm_conversion(
        actor_site=SITE_A, source_site=SITE_B,
        source_record_id="rec-b-text007", field_name=FIELD)
    assert pending is None and "无权" in message
    # 乙确认后才拿到共享写入范围，公共编号写入整数 7（不是文本 "007"）。
    pending, message = extract_svc.confirm_conversion(
        actor_site=SITE_B, source_site=SITE_B,
        source_record_id="rec-b-text007", field_name=FIELD)
    assert pending is not None and pending["confirmed"] is True
    row, message = extract_svc.write_shared_dataset(
        actor_site=SITE_B, receipt_id=receipt_b_text["receipt_id"])
    assert row is not None, message
    assert row["公共编号"] == 7 and isinstance(row["公共编号"], int)
    # 共享集现有乙的两行（整数7来源 + 文本007转换来源），甲仍为 0 行。
    assert len(extract_svc.shared_available_rows()) == 2
    assert extract_svc.shared_available_rows(SITE_A) == []


def test_11_interceptions_keep_raw_values_and_rules():
    extract_svc = DataExtractService()
    intercepted = extract_svc.intercepted_values()
    # 至少包含：甲/乙文本007的待转换拦截、乙借用甲回执越权、甲未确认即写入。
    stages = [item["stage"] for item in intercepted]
    assert "shared_pending_conversion" in stages
    assert "shared_write_permission" in stages
    assert "shared_write_unconfirmed" in stages
    borrowed = next(i for i in intercepted if i["stage"] == "shared_write_permission")
    assert borrowed["raw_value"] == "007"
    assert borrowed["actor_site"] == SITE_B and borrowed["source_site"] == SITE_A
    assert borrowed["applied_rule"]["归属站点"] == SHARED_SCOPE


def test_12_unparsable_text_blocked_at_both_layers():
    """额外覆盖：无法解析为整数的文本在站点共用定义与共享列均被拦截，不产生任何行。"""
    extract_svc = DataExtractService()
    outcome = extract_svc.ingest_source(
        source_site=SITE_B, source_record_id="rec-b-bad",
        target_data=TARGET, field_name=FIELD, raw_value="abc")
    assert outcome["blocked"] is True
    assert outcome["structured_row"] is None
    assert len(extract_svc.local_accepted_rows(SITE_B)) == 2  # 仍是此前两个来源
    blocked = [i for i in extract_svc.intercepted_values()
               if i.get("source_record_id") == "rec-b-bad"]
    assert any(i["stage"] == "site_layer" for i in blocked)


def test_13_rule_update_keeps_old_receipt_immutable():
    schema_svc = SchemaCheckService()
    # 定位甲站专属规则并推进版本（改格式约束）。
    rule = next(r for r in store.rows("schema_check")
                if r.get("归属站点") == SITE_A and r.get("目标数据") == TARGET)
    rule_id = rule["id"]
    # 乙站无权更新甲站规则。
    denied, msg = schema_svc.update_site_rule(
        rule_id, actor_site=SITE_B, changes={"格式约束": "越权修改"})
    assert denied is None and "无权" in msg
    # 甲站自行更新成功。
    updated, msg = schema_svc.update_site_rule(
        rule_id, actor_site=SITE_A, changes={"格式约束": "v2-新口径"})
    assert updated is not None and updated["规则版本"].endswith("v2")

    # 复查旧回执：存储结论原样保留，规则版本快照仍是 v1。
    recheck = schema_svc.recheck_source_receipt(
        source_site=SITE_A, source_record_id="rec-a-007",
        target_data=TARGET, field_name=FIELD, raw_value="007")
    stored = recheck["stored_receipt"]
    assert stored["site_layer"]["rule"]["规则版本"].endswith("v1")
    assert stored["shared_layer"]["decision"] == SHARED_PENDING
    assert stored["raw_value"] == "007"
    assert recheck["stored_unchanged"] is True
    # 现行规则评估独立返回，不回写旧回执。
    again = schema_svc.get_source_receipt(SITE_A, "rec-a-007", FIELD)
    assert again["site_layer"]["rule"]["规则版本"].endswith("v1")


def _run_all() -> None:
    tests = [obj for name, obj in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"\nALL {len(tests)} TESTS PASSED")


if __name__ == "__main__":
    _run_all()
