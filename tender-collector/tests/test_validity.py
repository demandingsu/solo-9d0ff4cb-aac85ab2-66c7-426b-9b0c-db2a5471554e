"""投标有效期核验 API 测试：端到端（静态服务器托管示例站 + TestClient）
与 verify_validity 纯函数单元测试。
"""
from __future__ import annotations

import functools
import http.server
import importlib.util
import random
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = ROOT / "demo_site"

# 加载示例数据生成器（脚本名带连字符，用 importlib）
spec = importlib.util.spec_from_file_location(
    "generate_demo_site", ROOT / "scripts" / "generate_demo_site.py"
)
gen = importlib.util.module_from_spec(spec)
sys.modules["generate_demo_site"] = gen
spec.loader.exec_module(gen)


# ---------------------------------------------------------------------------
# 静态文件服务器（支持可选的故障注入）
# ---------------------------------------------------------------------------
class FlakyHandler(http.server.SimpleHTTPRequestHandler):
    fail_paths: set[str] = set()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DEMO_DIR), **kwargs)

    def do_GET(self):  # noqa: N802
        if self.path in self.fail_paths:
            self.send_error(404, "injected failure")
            return
        return super().do_GET()

    def log_message(self, *args):  # 静默
        pass


@pytest.fixture(scope="session")
def demo_server():
    handler = functools.partial(FlakyHandler)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    yield f"http://{host}:{port}", httpd, FlakyHandler
    httpd.shutdown()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TENDER_DB_PATH", str(tmp_path / "test.db"))
    from app import config, database
    importlib.reload(config)
    import app.main as main
    importlib.reload(database)
    importlib.reload(main)
    database.init_db()
    with TestClient(main.app) as c:
        yield c, main, database


def verify(client, base, **overrides):
    payload = {
        "list_url": f"{base}/list_1.html",
        "project_code": "SZ-2026-002",
        "section": "一标段",
        "records_url": f"{base}/validity/list_1.html",
        "planned_at": "2026-12-25 10:00",
    }
    payload.update(overrides)
    return client.post("/api/verify-validity", json=payload)


def bidder_map(payload):
    return {b["bidder"]: b for b in payload["bidders"]}


# ---------------------------------------------------------------------------
# 端到端：示例站完整核验流程
# ---------------------------------------------------------------------------
def test_validity_check_end_to_end(client, demo_server):
    c, _, _ = client
    base, _, _ = demo_server
    resp = verify(c, base)
    assert resp.status_code == 200, resp.text
    payload = resp.json()

    # 截标时间复用公告链：SZ-2026-002 的生效截止来自第二次更正公告
    assert payload["project_code"] == "SZ-2026-002"
    assert payload["project_name"] == "城北污水处理厂扩建工程"
    assert payload["section"] == "一标段"
    assert payload["deadline"] == "2026-11-30 10:00"
    assert payload["deadline_source_code"] == "SZ-2026-002-G2"
    assert payload["planned_at"] == "2026-12-25 10:00"
    assert payload["records_read"] == 17

    bidders = bidder_map(payload)
    # 其他标段/其他项目的记录不得混入
    assert set(bidders) == {
        "宏远建设集团有限公司",
        "长江路桥工程有限公司",
        "天成市政工程有限公司",
        "中港湾工程局有限公司",
        "北方交通建设工程有限公司",
        "远航基础工程有限公司",
    }

    # 宏远：提交 -> 两次延长，链条完整，最终有效期 2027-02-28 → 有效
    b = bidders["宏远建设集团有限公司"]
    assert b["status"] == "valid"
    assert b["final_valid_until"] == "2027-02-28 10:00"
    assert b["source_record_code"] == "VR-003"
    assert b["source_record_url"].endswith("/validity/VR-003.html")
    assert b["reason"] is None

    # 长江路桥：延长 VR-005 被 VR-006 撤回，回到提交记录 → 已失效
    b = bidders["长江路桥工程有限公司"]
    assert b["status"] == "expired"
    assert b["final_valid_until"] == "2026-12-20 10:00"
    assert b["source_record_code"] == "VR-004"

    # 天成：拟发时间后才生效的延长 VR-008 不计入 → 以提交记录判定，有效
    b = bidders["天成市政工程有限公司"]
    assert b["status"] == "valid"
    assert b["final_valid_until"] == "2027-01-30 10:00"
    assert b["source_record_code"] == "VR-007"

    # 中港湾：延长引用不存在的 VR-099 → 引用断链，无法判定且不猜结论
    b = bidders["中港湾工程局有限公司"]
    assert b["status"] == "undetermined"
    assert b["final_valid_until"] is None
    assert b["source_record_code"] is None
    assert "引用断链" in b["reason"]

    # 北方交通：两条延长同一时刻生效 → 冲突，无法判定
    b = bidders["北方交通建设工程有限公司"]
    assert b["status"] == "undetermined"
    assert b["final_valid_until"] is None
    assert "同一时刻冲突" in b["reason"]

    # 远航：投标提交被撤回 → 已失效，来源为撤回记录
    b = bidders["远航基础工程有限公司"]
    assert b["status"] == "expired"
    assert b["final_valid_until"] is None
    assert b["source_record_code"] == "VR-015"
    assert "撤回" in b["reason"]


def test_section_and_project_filtering(client, demo_server):
    c, _, _ = client
    base, _, _ = demo_server

    # 二标段：只有宏远的二标段记录（有效期至 2026-12-15，拟发时已失效）
    resp = verify(c, base, section="二标段")
    assert resp.status_code == 200, resp.text
    bidders = bidder_map(resp.json())
    assert set(bidders) == {"宏远建设集团有限公司"}
    b = bidders["宏远建设集团有限公司"]
    assert b["status"] == "expired"
    assert b["source_record_code"] == "VR-016"

    # 其他项目：SZ-2026-001 一标段（截标时间来自更正公告 G1）
    resp = verify(c, base, project_code="SZ-2026-001")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["deadline"] == "2026-11-25 14:30"
    assert payload["deadline_source_code"] == "SZ-2026-001-G1"
    bidders = bidder_map(payload)
    assert set(bidders) == {"宏远建设集团有限公司"}
    assert bidders["宏远建设集团有限公司"]["status"] == "valid"
    assert bidders["宏远建设集团有限公司"]["source_record_code"] == "VR-017"


def test_unknown_project_returns_404(client, demo_server):
    c, _, _ = client
    base, _, _ = demo_server
    resp = verify(c, base, project_code="SZ-2099-999")
    assert resp.status_code == 404
    assert "SZ-2099-999" in resp.json()["detail"]


def test_invalid_planned_at_returns_422(client, demo_server):
    c, _, _ = client
    base, _, _ = demo_server
    resp = verify(c, base, planned_at="not-a-time")
    assert resp.status_code == 422


def test_records_list_page_failure_returns_422(client, demo_server):
    c, _, _ = client
    base, _, handler = demo_server
    handler.fail_paths = {"/validity/list_2.html"}
    try:
        resp = verify(c, base)
        assert resp.status_code == 422
        assert "list_2" in resp.json()["detail"]
    finally:
        handler.fail_paths = set()


def test_record_detail_failure_returns_422(client, demo_server):
    c, _, _ = client
    base, _, handler = demo_server
    handler.fail_paths = {"/validity/VR-003.html"}
    try:
        resp = verify(c, base)
        assert resp.status_code == 422
        assert "VR-003" in resp.json()["detail"]
    finally:
        handler.fail_paths = set()


def test_announcement_side_failure_returns_422(client, demo_server):
    c, _, _ = client
    base, _, handler = demo_server
    handler.fail_paths = {"/SZ-2026-002-G2.html"}
    try:
        resp = verify(c, base)
        assert resp.status_code == 422
    finally:
        handler.fail_paths = set()


# ---------------------------------------------------------------------------
# verify_validity 纯函数单元测试
# ---------------------------------------------------------------------------
def rec(
    record_id,
    kind,
    bidder="甲公司",
    project="P-1",
    section="一标段",
    effective="2026-11-30 09:00",
    valid_until=None,
    ref=None,
):
    return {
        "record_id": record_id,
        "kind": kind,
        "project_code": project,
        "section": section,
        "bidder": bidder,
        "effective_at": effective,
        "valid_until": valid_until,
        "ref": ref,
        "record_url": f"http://x/{record_id}.html",
    }


def check(records, **kwargs):
    from app.validity import verify_validity

    args = dict(
        project_code="P-1",
        section="一标段",
        deadline="2026-11-30 10:00",
        planned_at="2026-12-25 10:00",
    )
    args.update(kwargs)
    return {b.bidder: b for b in verify_validity(records, **args)}


def test_order_independence():
    records = [
        rec("R1", "submission", valid_until="2026-12-20 10:00"),
        rec("R2", "extension", effective="2026-12-01 10:00",
            valid_until="2027-01-30 10:00", ref="R1"),
        rec("R3", "withdrawal", effective="2026-12-10 10:00", ref="R2"),
        rec("R4", "submission", bidder="乙公司",
            effective="2026-11-30 08:00", valid_until="2027-01-01 10:00"),
    ]
    baseline = check(records)
    shuffled = list(records)
    random.Random(7).shuffle(shuffled)
    assert check(shuffled) == baseline
    assert baseline["甲公司"].status == "expired"
    assert baseline["甲公司"].final_valid_until == "2026-12-20 10:00"
    assert baseline["甲公司"].source_record_code == "R1"
    assert baseline["乙公司"].status == "valid"


def test_valid_until_exactly_at_planned_is_expired():
    records = [rec("R1", "submission", valid_until="2026-12-25 10:00")]
    assert check(records)["甲公司"].status == "expired"


def test_extension_effective_exactly_at_planned_counts():
    records = [
        rec("R1", "submission", valid_until="2026-12-20 10:00"),
        rec("R2", "extension", effective="2026-12-25 10:00",
            valid_until="2027-01-25 10:00", ref="R1"),
    ]
    b = check(records)["甲公司"]
    assert b.status == "valid"
    assert b.source_record_code == "R2"


def test_extension_after_planned_ignored():
    records = [
        rec("R1", "submission", valid_until="2026-12-20 10:00"),
        rec("R2", "extension", effective="2026-12-26 10:00",
            valid_until="2027-01-25 10:00", ref="R1"),
    ]
    b = check(records)["甲公司"]
    assert b.status == "expired"
    assert b.final_valid_until == "2026-12-20 10:00"
    assert b.source_record_code == "R1"


def test_late_submission_is_not_a_chain_start():
    records = [
        rec("R1", "submission", effective="2026-11-30 11:00",
            valid_until="2027-01-30 10:00"),
    ]
    b = check(records)["甲公司"]
    assert b.status == "undetermined"
    assert "截标前" in b.reason


def test_multiple_pre_deadline_submissions_conflict():
    records = [
        rec("R1", "submission", effective="2026-11-30 08:00",
            valid_until="2027-01-30 10:00"),
        rec("R2", "submission", effective="2026-11-30 09:00",
            valid_until="2027-01-30 10:00"),
    ]
    assert check(records)["甲公司"].status == "undetermined"


def test_extension_referencing_other_bidder_is_broken():
    records = [
        rec("R1", "submission", valid_until="2026-12-30 10:00"),
        rec("R2", "submission", bidder="乙公司",
            effective="2026-11-30 09:30", valid_until="2027-01-30 10:00"),
        rec("R3", "extension", effective="2026-12-01 10:00",
            valid_until="2027-02-01 10:00", ref="R2"),
    ]
    bidders = check(records)
    assert bidders["甲公司"].status == "undetermined"
    assert "引用断链" in bidders["甲公司"].reason
    # 乙公司自身链条不受影响
    assert bidders["乙公司"].status == "valid"


def test_extension_skipping_previous_record_is_broken():
    records = [
        rec("R1", "submission", valid_until="2026-12-30 10:00"),
        rec("R2", "extension", effective="2026-12-01 10:00",
            valid_until="2027-01-30 10:00", ref="R1"),
        rec("R3", "extension", effective="2026-12-10 10:00",
            valid_until="2027-02-28 10:00", ref="R1"),  # 跳过 R2
    ]
    b = check(records)["甲公司"]
    assert b.status == "undetermined"
    assert "引用断链" in b.reason


def test_withdrawal_of_missing_record_is_broken():
    records = [
        rec("R1", "submission", valid_until="2027-01-30 10:00"),
        rec("R2", "withdrawal", effective="2026-12-10 10:00", ref="R9"),
    ]
    b = check(records)["甲公司"]
    assert b.status == "undetermined"
    assert "引用断链" in b.reason


def test_withdrawn_submission_is_expired():
    records = [
        rec("R1", "submission", valid_until="2027-01-30 10:00"),
        rec("R2", "withdrawal", effective="2026-12-10 10:00", ref="R1"),
    ]
    b = check(records)["甲公司"]
    assert b.status == "expired"
    assert b.final_valid_until is None
    assert b.source_record_code == "R2"


def test_withdrawal_after_planned_ignored():
    records = [
        rec("R1", "submission", valid_until="2026-12-30 10:00"),
        rec("R2", "extension", effective="2026-12-01 10:00",
            valid_until="2027-01-30 10:00", ref="R1"),
        rec("R3", "withdrawal", effective="2026-12-26 10:00", ref="R2"),
    ]
    b = check(records)["甲公司"]
    assert b.status == "valid"
    assert b.source_record_code == "R2"


def test_earliest_withdrawn_record_cuts_chain():
    # 链：R1 -> R2 -> R3；R2 被撤回后 R3 一并失效，回到 R1
    records = [
        rec("R1", "submission", valid_until="2026-12-20 10:00"),
        rec("R2", "extension", effective="2026-12-01 10:00",
            valid_until="2027-01-30 10:00", ref="R1"),
        rec("R3", "extension", effective="2026-12-10 10:00",
            valid_until="2027-02-28 10:00", ref="R2"),
        rec("R4", "withdrawal", effective="2026-12-15 10:00", ref="R2"),
    ]
    b = check(records)["甲公司"]
    assert b.status == "expired"
    assert b.final_valid_until == "2026-12-20 10:00"
    assert b.source_record_code == "R1"


def test_same_time_conflict_between_extension_and_withdrawal():
    records = [
        rec("R1", "submission", valid_until="2026-12-30 10:00"),
        rec("R2", "extension", effective="2026-12-10 10:00",
            valid_until="2027-01-30 10:00", ref="R1"),
        rec("R3", "withdrawal", effective="2026-12-10 10:00", ref="R2"),
    ]
    b = check(records)["甲公司"]
    assert b.status == "undetermined"
    assert "同一时刻冲突" in b.reason


def test_records_of_other_projects_and_sections_ignored():
    records = [
        rec("R1", "submission", valid_until="2027-01-30 10:00"),
        rec("R2", "submission", project="P-2", bidder="乙公司",
            valid_until="2026-12-01 10:00"),
        rec("R3", "submission", section="二标段", bidder="丙公司",
            valid_until="2026-12-01 10:00"),
    ]
    bidders = check(records)
    assert set(bidders) == {"甲公司"}
    assert bidders["甲公司"].status == "valid"
