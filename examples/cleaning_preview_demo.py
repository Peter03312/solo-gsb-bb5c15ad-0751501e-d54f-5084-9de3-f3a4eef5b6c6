"""清洁更正预演（只读推演）端到端示例（仅用标准库）。

演示流程（独立产线 LPV，批号 PV-*，避免与其他示例冲突）：
  1. 准备：
       原料批号 PV-RM-P（peanut）
       产线 LPV：PV-B1 用花生酱并声明 peanut（RELEASE，peanut 残留）
                 PV-B2 开工前清洁确认清除 peanut、无用料（RELEASE）
                 PV-B3 无用料无清洁（RELEASE，残留已被 PV-B2 清除）
       核验单 PV-SH-1 含 [PV-B2, PV-B3] 并确认（CONFIRMED）
  2. 预演“把 PV-B2 的清洁确认更正为空”：
       同一次当前数据快照下，从 PV-B2 起逐批推演残留/实际/未声明与 RELEASE/HOLD，
       返回每批当前与预演结论、变化原因；PV-B2/PV-B3 均将转 HOLD；
       已确认核验单 PV-SH-1 列为将转待复核（含关联批次）。
  3. 只读核验：预演后清洁记录、裁决版本、出货资格版本、核验单状态与历史均不变。
  4. 拒绝场景：未知批次 404、已作废批次 409、非法过敏原代码 400（整体报错）。
  5. 正式更正：PUT 清洁更正的重算结果与预演逐批一致，PV-SH-1 转 PENDING_REVIEW。

用法：
    python examples/cleaning_preview_demo.py
    BASE_URL=http://127.0.0.1:8080 python examples/cleaning_preview_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080")
LINE_ID = "LPV"


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(label: str, status: int, body: dict) -> None:
    print(f"[{status}] {label}")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def ensure_lot(lot_no: str, material: str, allergens: list[str]) -> None:
    status, body = call("POST", "/api/material-lots", {
        "lot_no": lot_no, "material": material, "allergens": allergens,
    })
    if status == 201:
        print(f"  已登记原料批号 {lot_no}（{material}，{allergens}）")
    elif status == 409:
        print(f"  原料批号 {lot_no} 已存在，继续")
    else:
        raise RuntimeError(f"登记原料批号失败: {body}")


def ensure_batch(batch_no: str, payload: dict) -> dict:
    status, body = call("POST", "/api/batches", payload)
    if status == 201:
        adj = body["adjudication"]
        print(f"  已登记批次 {batch_no}（{body['line_id']} seq={body['seq']}）"
              f" => {adj['disposition']}（实际={adj['actual_allergens']}）")
        return body
    if status == 409:
        print(f"  批次 {batch_no} 已存在，继续")
        return call("GET", f"/api/batches/{batch_no}")[1]
    raise RuntimeError(f"登记批次失败: {body}")


def main() -> int:
    print(f"目标服务：{BASE_URL}")
    status, _ = call("GET", "/health")
    if status != 200:
        print(f"服务不可用（/health 返回 {status}），请先启动服务。", file=sys.stderr)
        return 1
    print("健康检查通过")

    suffix = str(int(time.time()))
    doc_no = f"PV-SH-{suffix}"

    section("1. 准备：PV-B1(peanut 残留) / PV-B2(清洁 peanut) / PV-B3；核验单确认")
    ensure_lot("PV-RM-P", "预演示例花生酱", ["peanut"])
    ensure_batch("PV-B1", {
        "batch_no": "PV-B1", "line_id": LINE_ID,
        "material_lots": ["PV-RM-P"], "declared_allergens": ["peanut"],
        "cleaned_allergens": [],
    })
    ensure_batch("PV-B2", {
        "batch_no": "PV-B2", "line_id": LINE_ID,
        "material_lots": [], "declared_allergens": [], "cleaned_allergens": ["peanut"],
    })
    ensure_batch("PV-B3", {
        "batch_no": "PV-B3", "line_id": LINE_ID,
        "material_lots": [], "declared_allergens": [], "cleaned_allergens": [],
    })
    status, body = call("POST", "/api/verifications", {
        "verification_no": doc_no, "batch_nos": ["PV-B2", "PV-B3"],
    })
    assert status == 201, body
    status, doc = call("POST", f"/api/verifications/{doc_no}/confirm")
    assert status == 200 and doc["status"] == "CONFIRMED", doc
    print(f"  核验单 {doc_no} 已确认（CONFIRMED）")

    section("2. 预演：把 PV-B2 的清洁确认更正为空集合")
    status, preview = call("POST", "/api/batches/PV-B2/cleaning/preview",
                           {"cleaned_allergens": []})
    show("预演结果（只读）", status, preview)
    assert status == 200, preview
    assert preview["read_only"] is True
    assert preview["impacted_batch_nos"] == ["PV-B2", "PV-B3"]
    sims = {s["batch_no"]: s for s in preview["simulations"]}
    assert sims["PV-B2"]["current"]["disposition"] == "RELEASE"
    assert sims["PV-B2"]["simulated"]["disposition"] == "HOLD"
    assert sims["PV-B2"]["simulated"]["residual"] == ["peanut"]
    assert sims["PV-B3"]["simulated"]["disposition"] == "HOLD"
    assert sims["PV-B3"]["simulated"]["carried_in"] == ["peanut"]
    affected = preview["affected_verifications"]
    assert [a["verification_no"] for a in affected] == [doc_no]
    assert affected[0]["would_be_status"] == "PENDING_REVIEW"
    assert affected[0]["impacted_batch_nos"] == ["PV-B2", "PV-B3"]

    section("3. 只读核验：预演不更改任何记录")
    b2 = call("GET", "/api/batches/PV-B2")[1]
    assert b2["cleaned_allergens"] == ["peanut"]
    assert b2["adjudication"]["version"] == 1
    assert b2["shipment_eligibility"]["version"] == 1
    doc = call("GET", f"/api/verifications/{doc_no}")[1]
    assert doc["status"] == "CONFIRMED"
    assert len(doc["confirmations"]) == 1
    print("  清洁记录 / 裁决版本 / 出货资格版本 / 核验单状态与确认历史均未变化")

    section("4. 拒绝场景：未知批次 / 已作废批次 / 非法过敏原代码")
    status, body = call("POST", "/api/batches/PV-NOPE/cleaning/preview",
                        {"cleaned_allergens": []})
    print(f"[{status}] 未知批次: {body['error']['code']}")
    assert status == 404 and body["error"]["code"] == "CORRECTION_TARGET_NOT_FOUND", body

    ensure_batch("PV-B9", {
        "batch_no": "PV-B9", "line_id": LINE_ID,
        "material_lots": [], "declared_allergens": [], "cleaned_allergens": [],
    })
    status, body = call("POST", "/api/batches/PV-B9/void", {"reason": "误登记未投产"})
    assert status == 200, body
    status, body = call("POST", "/api/batches/PV-B9/cleaning/preview",
                        {"cleaned_allergens": []})
    print(f"[{status}] 已作废批次: {body['error']['code']}")
    assert status == 409 and body["error"]["code"] == "BATCH_VOIDED", body

    status, body = call("POST", "/api/batches/PV-B2/cleaning/preview",
                        {"cleaned_allergens": ["plastic"]})
    print(f"[{status}] 非法过敏原代码: {body['error']['code']}")
    assert status == 400 and body["error"]["code"] == "INVALID_ALLERGEN", body

    section("5. 正式更正：重算结果与预演一致，核验单转待复核")
    status, corrected = call("PUT", "/api/batches/PV-B2/cleaning",
                             {"cleaned_allergens": []})
    assert status == 200, corrected
    recomputed = {r["batch_no"]: r for r in corrected["recomputed"]}
    for bn, sim in sims.items():
        assert recomputed[bn]["disposition"] == sim["simulated"]["disposition"], bn
        assert recomputed[bn]["actual"] == sim["simulated"]["actual_allergens"], bn
    doc = call("GET", f"/api/verifications/{doc_no}")[1]
    assert doc["status"] == "PENDING_REVIEW"
    print(f"  正式更正后 {doc_no} 状态={doc['status']}（与预演一致）")

    section("完成")
    print("预演（POST /api/batches/{batch_no}/cleaning/preview）只读推演同线后续批次")
    print("与已确认核验单影响；正式更正（PUT /api/batches/{batch_no}/cleaning）行为不变。")
    print("可继续在交互式文档中探索： %s/docs" % BASE_URL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
