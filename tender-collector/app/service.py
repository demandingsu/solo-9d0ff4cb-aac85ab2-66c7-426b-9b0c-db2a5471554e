"""采集编排服务：抓取 -> 归并校验 -> 单事务落库；失败时保留上次成功结果。

另含投标有效期核验编排：复用公告采集与更正归并得到截标时间，再读取
报价有效期记录目录逐投标人判定拟发时刻的报价有效性（核验为只读操作，
不写入数据库）。
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime

from . import database
from .merge import EffectiveDeadline, MergeError, merge_projects
from .scraper import CollectionError, collect, collect_records
from .timeutil import parse_time
from .validity import verify_validity


class ServiceError(Exception):
    """对外统一的采集错误（带可读原因）。"""


class ProjectNotFoundError(Exception):
    """公告链中找不到指定项目编号对应的项目。"""


def _is_expired(deadline_text: str, now: datetime | None = None) -> bool:
    return parse_time(deadline_text) <= (now or datetime.now())


def _to_project(d: EffectiveDeadline, now: datetime | None = None) -> dict:
    return {
        "project_code": d.project_code,
        "project_name": d.project_name,
        "current_deadline": d.deadline,
        "deadline_source_code": d.source_code,
        "deadline_source_url": d.source_url,
        "is_expired": _is_expired(d.deadline, now),
    }


def build_projects(rows: list[dict], now: datetime | None = None) -> list[dict]:
    return [_to_project(d, now) for d in merge_projects(rows)]


def run_collection(list_url: str) -> dict:
    started_at = database.utcnow_iso()
    try:
        raw = collect(list_url)
        # 先在内存完成归并校验：数据有问题时不写库
        projects = build_projects(raw)
    except (CollectionError, MergeError) as exc:
        # 记录失败采集；announcements 表完全不动，保留上次成功结果
        with database.get_conn() as conn:
            database.record_run(
                conn,
                source_url=list_url,
                status="failed",
                items_collected=0,
                error=str(exc),
                started_at=started_at,
            )
            conn.commit()
        raise ServiceError(str(exc)) from exc

    # 成功：单事务 UPSERT + 运行记录，保证原子性
    with database.get_conn() as conn:
        try:
            database.upsert_announcements(conn, raw)
            database.record_run(
                conn,
                source_url=list_url,
                status="success",
                items_collected=len(raw),
                error=None,
                started_at=started_at,
            )
            conn.commit()
        except Exception as exc:  # pragma: no cover - 兜底回滚
            conn.rollback()
            raise ServiceError(f"写入数据库失败: {exc}") from exc

    return {"source_url": list_url, "collected": len(raw), "projects": projects}


def query_projects(now: datetime | None = None) -> list[dict]:
    with database.get_conn() as conn:
        rows = [dict(r) for r in database.fetch_all_announcements(conn)]
    if not rows:
        return []
    return build_projects(rows, now)


def run_validity_check(
    *,
    list_url: str,
    project_code: str,
    section: str,
    records_url: str,
    planned_at: str,
) -> dict:
    """投标有效期核验：确认某标段各投标报价在拟发时刻是否仍有效。

    1. 复用公告采集 + 更正归并，得到该项目当前生效的截标时间；
    2. 沿记录目录分页读取投标提交/有效期延长/撤回记录；
    3. 逐投标人沿引用链判定，引用断链/同一时刻冲突时不猜结论，标记无法判定。
    """
    try:
        rows = collect(list_url)
        projects = merge_projects(rows)
    except (CollectionError, MergeError) as exc:
        raise ServiceError(str(exc)) from exc

    project = next((p for p in projects if p.project_code == project_code), None)
    if project is None:
        raise ProjectNotFoundError(
            f"公告链中不存在项目编号 {project_code}（原公告编号）对应的项目"
        )

    # 任一记录页面读取失败即整体失败（不猜结论）
    try:
        records = collect_records(records_url)
    except CollectionError as exc:
        raise ServiceError(str(exc)) from exc

    try:
        bidders = verify_validity(
            records,
            project_code=project_code,
            section=section,
            deadline=project.deadline,
            planned_at=planned_at,
        )
    except ValueError as exc:  # 源站时间字段无法解析
        raise ServiceError(f"时间数据无法解析: {exc}") from exc

    return {
        "project_code": project.project_code,
        "project_name": project.project_name,
        "section": section,
        "deadline": project.deadline,
        "deadline_source_code": project.source_code,
        "deadline_source_url": project.source_url,
        "planned_at": planned_at,
        "records_read": len(records),
        "bidders": [asdict(b) for b in bidders],
    }
