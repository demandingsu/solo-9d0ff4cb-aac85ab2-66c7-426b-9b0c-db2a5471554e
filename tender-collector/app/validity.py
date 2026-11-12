"""投标有效期核验（纯函数，结果不依赖页面/输入顺序）。

规则：
1. 以截标前（生效时间 < 截标时间）的投标提交为有效期链起点，起点必须唯一；
   晚于截标时间的提交不参与判定。
2. 延长记录必须引用同一投标人链条上的上一条记录，且生效时间 ≤ 拟发时间
   才计入；拟发时间之后才生效的延长视为尚未生效，直接忽略。
3. 撤回记录（生效时间 ≤ 拟发时间）使其引用的记录及链上后续记录全部失效，
   有效期回到上一条未被撤回的记录；投标提交本身被撤回则该投标不再有效。
4. 引用断链（引用不存在/他人记录/非上一条记录/撤回记录）、同一投标人同一
   时刻出现多条记录等冲突情形，该投标人判定为“无法判定”——绝不猜测结论。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from .timeutil import parse_time

VALID = "valid"
EXPIRED = "expired"
UNDETERMINED = "undetermined"


@dataclass(frozen=True)
class BidderValidity:
    bidder: str
    status: str  # valid | expired | undetermined
    final_valid_until: str | None
    source_record_code: str | None
    source_record_url: str | None
    reason: str | None


def _undetermined(bidder: str, reason: str) -> BidderValidity:
    return BidderValidity(bidder, UNDETERMINED, None, None, None, reason)


def _evaluate_bidder(
    bidder: str,
    records: list[dict[str, Any]],
    deadline: datetime,
    planned: datetime,
) -> BidderValidity:
    # 同一投标人的记录编号必须唯一，否则引用关系无法确定
    by_id: dict[str, dict[str, Any]] = {}
    for r in records:
        if r["record_id"] in by_id:
            return _undetermined(bidder, f"记录编号重复: {r['record_id']}")
        by_id[r["record_id"]] = r

    # 参与判定的记录：提交须早于截标；延长/撤回须在拟发时间前（含）生效
    submissions = sorted(
        (
            r
            for r in records
            if r["kind"] == "submission" and parse_time(r["effective_at"]) < deadline
        ),
        key=lambda r: r["effective_at"],
    )
    extensions = sorted(
        (
            r
            for r in records
            if r["kind"] == "extension" and parse_time(r["effective_at"]) <= planned
        ),
        key=lambda r: r["effective_at"],
    )
    withdrawals = sorted(
        (
            r
            for r in records
            if r["kind"] == "withdrawal" and parse_time(r["effective_at"]) <= planned
        ),
        key=lambda r: r["effective_at"],
    )

    # 同一时刻冲突：参与判定的记录生效时间必须互不相同，否则次序无法确定
    seen: dict[str, str] = {}
    for r in submissions + extensions + withdrawals:
        other = seen.get(r["effective_at"])
        if other is not None:
            return _undetermined(
                bidder,
                f"同一时刻冲突: {other} 与 {r['record_id']} 的生效时间均为 "
                f"{r['effective_at']}，无法确定先后次序",
            )
        seen[r["effective_at"]] = r["record_id"]

    if not submissions:
        return _undetermined(bidder, "缺少截标前的投标提交记录，无法确定有效期起点")
    if len(submissions) > 1:
        return _undetermined(
            bidder, "存在多份截标前的投标提交记录，无法确定有效期起点"
        )
    start = submissions[0]

    # 撤回记录必须引用本投标人已存在的提交/延长记录
    withdrawn_ids: set[str] = set()
    for w in withdrawals:
        target = by_id.get(w["ref"] or "")
        if target is None or target["kind"] == "withdrawal":
            return _undetermined(
                bidder,
                f"撤回记录 {w['record_id']} 引用的记录 {w['ref']} "
                "不存在或不可撤回（引用断链）",
            )
        withdrawn_ids.add(w["ref"])

    # 沿引用构建有效期链：每条延长必须引用当前链尾（同一投标人的上一条记录）
    chain = [start]
    for ext in extensions:
        tip = chain[-1]
        if ext["ref"] != tip["record_id"]:
            return _undetermined(
                bidder,
                f"延长记录 {ext['record_id']} 未引用同一投标人的上一条记录 "
                f"{tip['record_id']}（引用断链）",
            )
        if parse_time(ext["effective_at"]) < parse_time(tip["effective_at"]):
            return _undetermined(
                bidder,
                f"延长记录 {ext['record_id']} 的生效时间早于其引用的记录 "
                f"{tip['record_id']}（时间顺序异常）",
            )
        chain.append(ext)

    # 应用撤回：链上最早被撤回的记录及其后所有延长均不再生效
    cut = next(
        (i for i, r in enumerate(chain) if r["record_id"] in withdrawn_ids),
        len(chain),
    )
    chain = chain[:cut]

    if not chain:
        # 投标提交本身被撤回：该投标不再有效
        w = next(w for w in withdrawals if w["ref"] == start["record_id"])
        return BidderValidity(
            bidder,
            EXPIRED,
            None,
            w["record_id"],
            w["record_url"],
            "投标提交已被撤回，该投标不再有效",
        )

    final = chain[-1]
    valid_until = final["valid_until"]
    status = VALID if parse_time(valid_until) > planned else EXPIRED
    return BidderValidity(
        bidder, status, valid_until, final["record_id"], final["record_url"], None
    )


def verify_validity(
    records: Iterable[dict[str, Any]],
    *,
    project_code: str,
    section: str,
    deadline: str,
    planned_at: str,
) -> list[BidderValidity]:
    """逐投标人核验某项目某标段在拟发时刻的报价有效性。

    records 为记录目录中读取到的全部记录（任意顺序）；仅筛选
    project_code + section 匹配的记录参与判定。返回按投标人排序的结果。
    """
    deadline_dt = parse_time(deadline)
    planned_dt = parse_time(planned_at)

    by_bidder: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        if r["project_code"] == project_code and r["section"] == section:
            by_bidder.setdefault(r["bidder"], []).append(r)

    return [
        _evaluate_bidder(bidder, recs, deadline_dt, planned_dt)
        for bidder, recs in sorted(by_bidder.items())
    ]
