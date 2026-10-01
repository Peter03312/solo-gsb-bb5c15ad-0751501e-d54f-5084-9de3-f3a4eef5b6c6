"""清洁更正预演（只读推演）测试。

覆盖：
- 从目标批起跳过作废批次逐批推演残留/实际/未声明与 RELEASE/HOLD，
  返回每批当前与预演结论及变化原因；
- 即使结论不变，目标批及其后有效批次仍属版本影响范围（impacted_batch_nos）；
- 已确认核验单（含影响范围内批次）列为将转待复核并给出关联批次，草稿单不列出；
- 只读：预演不更改清洁记录、裁决/出货资格版本、核验单状态或历史；
- 批号待检锁定不参与过敏原推演；
- 未知批次 404、已作废批次 409、非法过敏原代码 400，整体报错且不留改动；
- 预演结论与正式更正重算结果一致；产线之间相互隔离。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"), seed_demo=False)
    with TestClient(app) as c:
        yield c


def add_lot(client, lot_no, material="原料", allergens=None):
    resp = client.post(
        "/api/material-lots",
        json={"lot_no": lot_no, "material": material, "allergens": allergens or []},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def add_batch(client, batch_no, line_id="L1", lots=None, declared=None, cleaned=None):
    resp = client.post(
        "/api/batches",
        json={
            "batch_no": batch_no,
            "line_id": line_id,
            "material_lots": lots or [],
            "declared_allergens": declared or [],
            "cleaned_allergens": cleaned or [],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def preview(client, batch_no, cleaned):
    return client.post(
        f"/api/batches/{batch_no}/cleaning/preview",
        json={"cleaned_allergens": cleaned},
    )


def create_verification(client, verification_no, batch_nos):
    return client.post(
        "/api/verifications",
        json={"verification_no": verification_no, "batch_nos": batch_nos},
    )


def build_peanut_chain(client):
    """L1：B1 花生（声明 peanut）→ B2 清洁 peanut → B3 无清洁；当前全部 RELEASE。"""
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "B2", lots=[], declared=[], cleaned=["peanut"])
    add_batch(client, "B3", lots=[], declared=[], cleaned=[])


# ---------------------------------------------------------------- 推演与变化原因


def test_preview_chain_simulation_and_change_reasons(client):
    build_peanut_chain(client)

    resp = preview(client, "B2", [])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["batch_no"] == "B2"
    assert body["line_id"] == "L1"
    assert body["seq"] == 2
    assert body["current_cleaned_allergens"] == ["peanut"]
    assert body["proposed_cleaned_allergens"] == []
    assert body["read_only"] is True
    # 版本影响范围：目标批及其后全部有效批次
    assert body["impacted_batch_nos"] == ["B2", "B3"]

    sims = {s["batch_no"]: s for s in body["simulations"]}
    assert list(sims) == ["B2", "B3"]

    b2 = sims["B2"]
    assert b2["current"]["disposition"] == "RELEASE"
    assert b2["current"]["actual_allergens"] == []
    assert b2["simulated"]["carried_in"] == ["peanut"]
    assert b2["simulated"]["residual"] == ["peanut"]
    assert b2["simulated"]["actual_allergens"] == ["peanut"]
    assert b2["simulated"]["undeclared_allergens"] == ["peanut"]
    assert b2["simulated"]["disposition"] == "HOLD"
    assert b2["changed"] is True and b2["disposition_changed"] is True
    codes = {r["code"] for r in b2["change_reasons"]}
    assert "CLEANING_CORRECTION" in codes
    assert "ACTUAL_ALLERGENS_CHANGED" in codes
    assert "UNDECLARED_ALLERGENS_CHANGED" in codes
    assert "DISPOSITION_CHANGED" in codes

    # 下游批次：残留沿有效批次传递，结论联动翻转
    b3 = sims["B3"]
    assert b3["current"]["disposition"] == "RELEASE"
    assert b3["simulated"]["carried_in"] == ["peanut"]
    assert b3["simulated"]["disposition"] == "HOLD"
    assert b3["changed"] is True
    assert "UPSTREAM_RECOMPUTE" in {r["code"] for r in b3["change_reasons"]}


def test_preview_unchanged_conclusions_still_in_version_scope(client):
    build_peanut_chain(client)
    # 拟更正集合与当前相同：结论不变，但后续有效批次仍属版本影响范围
    resp = preview(client, "B2", ["peanut"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["impacted_batch_nos"] == ["B2", "B3"]
    for s in body["simulations"]:
        assert s["changed"] is False
        assert s["disposition_changed"] is False
        assert s["simulated"]["disposition"] == s["current"]["disposition"]
        codes = {r["code"] for r in s["change_reasons"]}
        assert "CONCLUSION_UNCHANGED" in codes


def test_preview_skips_voided_batches(client):
    build_peanut_chain(client)
    # 作废 B2：B3 重算后直接继承 B1 的 peanut -> HOLD
    resp = client.post("/api/batches/B2/void", json={"reason": "误登记未投产"})
    assert resp.status_code == 200, resp.text
    assert client.get("/api/batches/B3").json()["adjudication"]["disposition"] == "HOLD"

    resp = preview(client, "B1", ["peanut"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 作废批次不参与推演，也不占用版本影响范围
    assert body["impacted_batch_nos"] == ["B1", "B3"]
    sims = {s["batch_no"]: s for s in body["simulations"]}
    assert "B2" not in sims
    # B3 的带入直接来自最近有效批次 B1（peanut），与当前结论一致
    assert sims["B3"]["simulated"]["carried_in"] == ["peanut"]
    assert sims["B3"]["simulated"]["disposition"] == "HOLD"
    assert sims["B3"]["changed"] is False


def test_preview_isolated_between_lines(client):
    build_peanut_chain(client)
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "C1", line_id="L2", lots=["RM-M"], declared=["milk"])

    resp = preview(client, "B2", [])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["impacted_batch_nos"] == ["B2", "B3"]
    assert all(s["line_id"] == "L1" for s in body["simulations"])
    # 其他产线裁决不受预演影响
    assert client.get("/api/batches/C1").json()["adjudication"]["version"] == 1


# ---------------------------------------------------------------- 核验单影响


def test_preview_lists_confirmed_verifications_with_batches(client):
    build_peanut_chain(client)
    assert create_verification(client, "SH-C", ["B2", "B3"]).status_code == 201
    resp = client.post("/api/verifications/SH-C/confirm")
    assert resp.status_code == 200 and resp.json()["status"] == "CONFIRMED"
    assert create_verification(client, "SH-D", ["B3"]).status_code == 201  # 草稿

    resp = preview(client, "B2", [])
    assert resp.status_code == 200, resp.text
    affected = resp.json()["affected_verifications"]
    assert [a["verification_no"] for a in affected] == ["SH-C"]
    entry = affected[0]
    assert entry["status"] == "CONFIRMED"
    assert entry["would_be_status"] == "PENDING_REVIEW"
    assert entry["batch_nos"] == ["B2", "B3"]
    assert entry["impacted_batch_nos"] == ["B2", "B3"]

    # 预演不转态：单据仍为已确认
    assert client.get("/api/verifications/SH-C").json()["status"] == "CONFIRMED"
    assert client.get("/api/verifications/SH-D").json()["status"] == "DRAFT"


def test_preview_confirmed_verification_listed_even_when_unchanged(client):
    build_peanut_chain(client)
    assert create_verification(client, "SH-1", ["B3"]).status_code == 201
    assert client.post("/api/verifications/SH-1/confirm").status_code == 200

    # 结论不变的预演：B3 仍属版本影响范围，含 B3 的已确认单据将转待复核
    resp = preview(client, "B2", ["peanut"])
    assert resp.status_code == 200, resp.text
    affected = resp.json()["affected_verifications"]
    assert [a["verification_no"] for a in affected] == ["SH-1"]
    assert affected[0]["impacted_batch_nos"] == ["B3"]


# ---------------------------------------------------------------- 只读保证


def test_preview_does_not_change_any_state(client):
    build_peanut_chain(client)
    assert create_verification(client, "SH-1", ["B2", "B3"]).status_code == 201
    assert client.post("/api/verifications/SH-1/confirm").status_code == 200

    before_batches = {b["batch_no"]: b for b in client.get("/api/batches").json()["batches"]}
    before_histories = {
        bn: client.get(f"/api/batches/{bn}/history").json()["history"]
        for bn in ("B1", "B2", "B3")
    }
    before_elig = {
        bn: client.get(f"/api/batches/{bn}/eligibility").json()["eligibility"]
        for bn in ("B1", "B2", "B3")
    }
    before_doc = client.get("/api/verifications/SH-1").json()

    resp = preview(client, "B2", [])
    assert resp.status_code == 200, resp.text

    after_batches = {b["batch_no"]: b for b in client.get("/api/batches").json()["batches"]}
    # 清洁记录、裁决版本与结论、出货资格均未变化
    for bn in ("B1", "B2", "B3"):
        assert after_batches[bn]["cleaned_allergens"] == before_batches[bn]["cleaned_allergens"]
        assert after_batches[bn]["adjudication"] == before_batches[bn]["adjudication"]
        assert after_batches[bn]["shipment_eligibility"] == before_batches[bn]["shipment_eligibility"]
        assert client.get(f"/api/batches/{bn}/history").json()["history"] == before_histories[bn]
        assert client.get(f"/api/batches/{bn}/eligibility").json()["eligibility"] == before_elig[bn]
    # 核验单状态与历史不变
    assert client.get("/api/verifications/SH-1").json() == before_doc


def test_preview_ignores_lot_locks_in_allergen_simulation(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "B2", lots=["RM-M"], declared=["milk"], cleaned=["peanut"])

    # 锁定 B2 直接使用的批号：出货资格 BLOCKED，但不参与过敏原推演
    resp = client.post("/api/material-lots/RM-M/lock", json={"reason": "待复检"})
    assert resp.status_code == 200, resp.text
    elig_before = client.get("/api/batches/B2/eligibility").json()["eligibility"]
    assert elig_before["status"] == "BLOCKED"

    resp = preview(client, "B2", ["peanut"])  # 与当前清洁相同
    assert resp.status_code == 200, resp.text
    sim = resp.json()["simulations"][0]
    # 待检锁定不影响推演结论：仍按过敏原规则得 RELEASE
    assert sim["simulated"]["disposition"] == "RELEASE"
    assert sim["changed"] is False
    # 预演不触碰出货资格版本
    elig_after = client.get("/api/batches/B2/eligibility").json()["eligibility"]
    assert elig_after == elig_before


# ---------------------------------------------------------------- 拒绝与报错


def test_preview_unknown_batch_404(client):
    resp = preview(client, "NOPE", ["peanut"])
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CORRECTION_TARGET_NOT_FOUND"


def test_preview_voided_batch_409(client):
    build_peanut_chain(client)
    resp = client.post("/api/batches/B2/void", json={"reason": "误登记未投产"})
    assert resp.status_code == 200, resp.text
    resp = preview(client, "B2", ["peanut"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "BATCH_VOIDED"


def test_preview_invalid_allergen_400(client):
    build_peanut_chain(client)
    resp = preview(client, "B2", ["peanut", "plastic"])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ALLERGEN"

    resp = client.post(
        "/api/batches/B2/cleaning/preview", json={"cleaned_allergens": "peanut"}
    )
    assert resp.status_code == 422  # 非数组：模型校验（与更正端点一致）

    resp = client.post("/api/batches/B2/cleaning/preview", json={})
    assert resp.status_code == 422  # 缺字段：模型校验

    # 报错不留任何改动
    b2 = client.get("/api/batches/B2").json()
    assert b2["cleaned_allergens"] == ["peanut"]
    assert b2["adjudication"]["version"] == 1


# ---------------------------------------------------------------- 与正式更正一致


def test_preview_matches_actual_correction(client):
    build_peanut_chain(client)
    resp = preview(client, "B2", [])
    assert resp.status_code == 200, resp.text
    sims = {s["batch_no"]: s for s in resp.json()["simulations"]}

    resp = client.put("/api/batches/B2/cleaning", json={"cleaned_allergens": []})
    assert resp.status_code == 200, resp.text
    recomputed = {r["batch_no"]: r for r in resp.json()["recomputed"]}
    # 正式更正的重算范围与预演版本影响范围一致
    assert set(recomputed) == {"B2", "B3"}
    for bn, sim in sims.items():
        assert recomputed[bn]["actual"] == sim["simulated"]["actual_allergens"]
        assert recomputed[bn]["undeclared"] == sim["simulated"]["undeclared_allergens"]
        assert recomputed[bn]["disposition"] == sim["simulated"]["disposition"]


def test_preview_then_correction_flips_listed_verification(client):
    build_peanut_chain(client)
    assert create_verification(client, "SH-1", ["B2", "B3"]).status_code == 201
    assert client.post("/api/verifications/SH-1/confirm").status_code == 200

    affected = preview(client, "B2", []).json()["affected_verifications"]
    assert [a["verification_no"] for a in affected] == ["SH-1"]

    resp = client.put("/api/batches/B2/cleaning", json={"cleaned_allergens": []})
    assert resp.status_code == 200, resp.text
    # 正式更正后，预演列出的已确认核验单确实转待复核
    doc = client.get("/api/verifications/SH-1").json()
    assert doc["status"] == "PENDING_REVIEW"
    flipped = {
        v["verification_no"]: v["status"] for v in resp.json()["affected_verifications"]
    }
    assert flipped == {"SH-1": "PENDING_REVIEW"}
