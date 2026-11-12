#!/usr/bin/env python3
"""生成一个本地分页 HTML 示例公告站（市政工程招投标公告）。

生成结果写入 demo_site/，纯静态文件，可用任意 HTTP 服务器托管：
    python3 -m http.server 8001 --directory demo_site

数据特意构造为：
- 列表页按发布时间倒序排列（更正公告排在其引用的原公告之前），
  服务端不能依赖页面顺序来判断截止时间；
- 每个项目的“生效截止时间”由发布时间最新的那份公告（原公告或更正公告）决定；
- 含已过期与未过期两种项目。

另生成 demo_site/validity/ 报价有效期记录目录（投标提交/有效期延长/撤回），
记录列表按生效时间倒序排列，同一投标人的有效期链被打散到不同分页，
用于投标有效期核验 API 的离线演示与测试。
"""
from __future__ import annotations

import html
import os
from dataclasses import dataclass, field
from pathlib import Path

PAGE_SIZE = 4
RECORD_PAGE_SIZE = 6
OUT_DIR = Path(__file__).resolve().parent.parent / "demo_site"
RECORDS_DIR = OUT_DIR / "validity"


@dataclass
class Announcement:
    code: str                      # 公告编号
    title: str                     # 公告标题
    kind: str                      # original | correction
    project_name: str              # 项目名称
    published_at: str              # 发布时间 YYYY-MM-DD HH:MM
    deadline: str | None = None    # 投标截止时间（更正公告可为空）
    corrects_code: str | None = None  # 更正公告引用的原公告编号
    reason: str = ""               # 更正原因（仅更正公告）

    @property
    def filename(self) -> str:
        return f"{self.code}.html"


# ---------------------------------------------------------------------------
# 示例数据：5 个项目，其中 3 个含更正公告，共 10 条公告
# ---------------------------------------------------------------------------
ANNOUNCEMENTS: list[Announcement] = [
    # 项目 A：原公告已过期，更正延期后仍在有效期内
    Announcement(
        code="SZ-2026-001",
        title="滨河路市政改造工程施工招标公告",
        kind="original",
        project_name="滨河路市政改造工程",
        published_at="2026-10-20 09:00",
        deadline="2026-11-05 14:30",
    ),
    Announcement(
        code="SZ-2026-001-G1",
        title="滨河路市政改造工程施工招标更正公告（第一次）",
        kind="correction",
        project_name="滨河路市政改造工程",
        published_at="2026-10-28 16:00",
        deadline="2026-11-25 14:30",
        corrects_code="SZ-2026-001",
        reason="因招标文件图纸调整，投标截止时间顺延。",
    ),
    # 项目 B：两次更正，最终截止时间以发布时间最新的第二次更正为准
    Announcement(
        code="SZ-2026-002",
        title="城北污水处理厂扩建工程招标公告",
        kind="original",
        project_name="城北污水处理厂扩建工程",
        published_at="2026-10-12 10:30",
        deadline="2026-11-10 09:00",
    ),
    Announcement(
        code="SZ-2026-002-G1",
        title="城北污水处理厂扩建工程招标更正公告",
        kind="correction",
        project_name="城北污水处理厂扩建工程",
        published_at="2026-10-22 15:00",
        deadline="2026-11-18 09:00",
        corrects_code="SZ-2026-002",
        reason="答疑澄清，截止时间调整。",
    ),
    Announcement(
        code="SZ-2026-002-G2",
        title="城北污水处理厂扩建工程招标更正公告（第二次）",
        kind="correction",
        project_name="城北污水处理厂扩建工程",
        published_at="2026-11-03 11:20",
        deadline="2026-11-30 10:00",
        corrects_code="SZ-2026-002",
        reason="再次延期，以本公告时间为准。",
    ),
    # 项目 C：无更正，且已过期
    Announcement(
        code="SZ-2026-003",
        title="东风桥加固维修工程招标公告",
        kind="original",
        project_name="东风桥加固维修工程",
        published_at="2026-09-15 08:45",
        deadline="2026-10-30 17:00",
    ),
    # 项目 D：更正公告不修改截止时间（deadline 为空），生效时间仍取原公告
    Announcement(
        code="SZ-2026-004",
        title="高新区雨污分流管网工程招标公告",
        kind="original",
        project_name="高新区雨污分流管网工程",
        published_at="2026-10-25 14:00",
        deadline="2026-11-28 09:30",
    ),
    Announcement(
        code="SZ-2026-004-G1",
        title="高新区雨污分流管网工程招标更正公告",
        kind="correction",
        project_name="高新区雨污分流管网工程",
        published_at="2026-11-01 09:10",
        deadline=None,
        corrects_code="SZ-2026-004",
        reason="仅更正联系人电话，投标截止时间不变。",
    ),
    # 项目 E：原公告在最后一个列表页、更正公告在第一页（跨页乱序）
    Announcement(
        code="SZ-2026-005",
        title="湖滨公园配套市政道路工程招标公告",
        kind="original",
        project_name="湖滨公园配套市政道路工程",
        published_at="2026-10-08 13:00",
        deadline="2026-11-08 15:00",
    ),
    Announcement(
        code="SZ-2026-005-G1",
        title="湖滨公园配套市政道路工程招标更正公告",
        kind="correction",
        project_name="湖滨公园配套市政道路工程",
        published_at="2026-11-05 17:30",
        deadline="2026-11-20 15:00",
        corrects_code="SZ-2026-005",
        reason="工程量清单调整，截止时间顺延。",
    ),
]


# ---------------------------------------------------------------------------
# 报价有效期记录：投标提交 / 有效期延长 / 撤回
# ---------------------------------------------------------------------------
@dataclass
class ValidityRecord:
    record_id: str                  # 记录编号
    kind: str                       # submission | extension | withdrawal
    project_code: str               # 项目编号（即原公告编号）
    section: str                    # 标段号
    bidder: str                     # 投标人
    effective_at: str               # 生效时间 YYYY-MM-DD HH:MM
    valid_until: str | None = None  # 有效期至（提交/延长记录必填）
    ref: str | None = None          # 引用记录编号（延长/撤回记录必填）
    note: str = ""                  # 备注

    @property
    def filename(self) -> str:
        return f"{self.record_id}.html"


RECORD_KIND_LABELS = {
    "submission": "投标提交",
    "extension": "有效期延长",
    "withdrawal": "撤回记录",
}

# 示例数据：项目 SZ-2026-002（截标时间 2026-11-30 10:00，见更正公告 G2）
# 一标段 6 家投标人，覆盖：有效（延长链/仅提交）、已失效（延长被撤回/提交被
# 撤回）、无法判定（引用断链/同一时刻冲突）；另有其他标段、其他项目的记录
# 用于验证过滤。拟发时间示例取 2026-12-25 10:00。
VALIDITY_RECORDS: list[ValidityRecord] = [
    # 宏远：提交 + 两次延长，链条完整，最终有效期 2027-02-28 → 有效
    ValidityRecord(
        record_id="VR-001", kind="submission", project_code="SZ-2026-002",
        section="一标段", bidder="宏远建设集团有限公司",
        effective_at="2026-11-30 09:10", valid_until="2026-12-30 10:00",
        note="截标前提交，初始报价有效期 30 天。",
    ),
    ValidityRecord(
        record_id="VR-002", kind="extension", project_code="SZ-2026-002",
        section="一标段", bidder="宏远建设集团有限公司",
        effective_at="2026-12-01 10:00", valid_until="2027-01-29 10:00",
        ref="VR-001", note="第一次延长报价有效期。",
    ),
    ValidityRecord(
        record_id="VR-003", kind="extension", project_code="SZ-2026-002",
        section="一标段", bidder="宏远建设集团有限公司",
        effective_at="2026-12-20 10:00", valid_until="2027-02-28 10:00",
        ref="VR-002", note="第二次延长报价有效期。",
    ),
    # 长江路桥：延长被撤回，有效期回到提交记录 2026-12-20 → 已失效
    ValidityRecord(
        record_id="VR-004", kind="submission", project_code="SZ-2026-002",
        section="一标段", bidder="长江路桥工程有限公司",
        effective_at="2026-11-30 09:20", valid_until="2026-12-20 10:00",
    ),
    ValidityRecord(
        record_id="VR-005", kind="extension", project_code="SZ-2026-002",
        section="一标段", bidder="长江路桥工程有限公司",
        effective_at="2026-12-10 10:00", valid_until="2027-01-20 10:00",
        ref="VR-004",
    ),
    ValidityRecord(
        record_id="VR-006", kind="withdrawal", project_code="SZ-2026-002",
        section="一标段", bidder="长江路桥工程有限公司",
        effective_at="2026-12-15 10:00", ref="VR-005",
        note="投标人函告撤回本次有效期延长。",
    ),
    # 天成：仅提交即有效；另有一条拟发时间之后才生效的延长（应被忽略）
    ValidityRecord(
        record_id="VR-007", kind="submission", project_code="SZ-2026-002",
        section="一标段", bidder="天成市政工程有限公司",
        effective_at="2026-11-30 09:40", valid_until="2027-01-30 10:00",
    ),
    ValidityRecord(
        record_id="VR-008", kind="extension", project_code="SZ-2026-002",
        section="一标段", bidder="天成市政工程有限公司",
        effective_at="2026-12-26 10:00", valid_until="2027-03-01 10:00",
        ref="VR-007", note="已签署但尚未生效的延长协议。",
    ),
    # 中港湾：延长记录引用了不存在的记录 VR-099 → 引用断链，无法判定
    ValidityRecord(
        record_id="VR-009", kind="submission", project_code="SZ-2026-002",
        section="一标段", bidder="中港湾工程局有限公司",
        effective_at="2026-11-30 09:50", valid_until="2026-12-30 10:00",
    ),
    ValidityRecord(
        record_id="VR-010", kind="extension", project_code="SZ-2026-002",
        section="一标段", bidder="中港湾工程局有限公司",
        effective_at="2026-12-05 10:00", valid_until="2027-02-01 10:00",
        ref="VR-099", note="引用记录编号有误（源站数据问题）。",
    ),
    # 北方交通：两条延长记录生效时间完全相同 → 同一时刻冲突，无法判定
    ValidityRecord(
        record_id="VR-011", kind="submission", project_code="SZ-2026-002",
        section="一标段", bidder="北方交通建设工程有限公司",
        effective_at="2026-11-30 09:55", valid_until="2026-12-28 10:00",
    ),
    ValidityRecord(
        record_id="VR-012", kind="extension", project_code="SZ-2026-002",
        section="一标段", bidder="北方交通建设工程有限公司",
        effective_at="2026-12-10 10:00", valid_until="2027-01-28 10:00",
        ref="VR-011",
    ),
    ValidityRecord(
        record_id="VR-013", kind="extension", project_code="SZ-2026-002",
        section="一标段", bidder="北方交通建设工程有限公司",
        effective_at="2026-12-10 10:00", valid_until="2027-02-15 10:00",
        ref="VR-012", note="与 VR-012 生效时间相同（源站数据问题）。",
    ),
    # 远航：投标提交被撤回 → 投标不再有效
    ValidityRecord(
        record_id="VR-014", kind="submission", project_code="SZ-2026-002",
        section="一标段", bidder="远航基础工程有限公司",
        effective_at="2026-11-30 09:05", valid_until="2027-01-15 10:00",
    ),
    ValidityRecord(
        record_id="VR-015", kind="withdrawal", project_code="SZ-2026-002",
        section="一标段", bidder="远航基础工程有限公司",
        effective_at="2026-12-20 10:00", ref="VR-014",
        note="投标人撤回其投标。",
    ),
    # 其他标段/其他项目的记录：核验时必须按项目编号 + 标段号过滤
    ValidityRecord(
        record_id="VR-016", kind="submission", project_code="SZ-2026-002",
        section="二标段", bidder="宏远建设集团有限公司",
        effective_at="2026-11-30 09:15", valid_until="2026-12-15 10:00",
    ),
    ValidityRecord(
        record_id="VR-017", kind="submission", project_code="SZ-2026-001",
        section="一标段", bidder="宏远建设集团有限公司",
        effective_at="2026-11-25 09:15", valid_until="2026-12-25 14:30",
    ),
]


def _detail_page(a: Announcement) -> str:
    rows = [
        ("公告编号", a.code),
        ("公告类型", "更正公告" if a.kind == "correction" else "招标公告"),
        ("项目名称", a.project_name),
        ("发布时间", a.published_at),
    ]
    if a.deadline is not None:
        rows.append(("投标截止时间", a.deadline))
    if a.kind == "correction":
        rows.append(("原公告编号", a.corrects_code or ""))
        rows.append(("更正事项", a.reason))

    trs = "\n".join(
        f'      <tr><th data-field="{html.escape(label)}">{html.escape(label)}</th>'
        f'<td data-value="{html.escape(label)}">{html.escape(value)}</td></tr>'
        for label, value in rows
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{html.escape(a.title)}</title>
</head>
<body>
  <article class="announcement-detail" data-code="{html.escape(a.code)}"
           data-kind="{a.kind}"
           data-published-at="{html.escape(a.published_at)}"
           {f'data-corrects="{html.escape(a.corrects_code)}"' if a.corrects_code else ""}>
    <h1 class="detail-title">{html.escape(a.title)}</h1>
    <table class="detail-table">
{trs}
    </table>
    <p><a href="list_1.html">返回列表</a></p>
  </article>
</body>
</html>
"""


def _list_page(items: list[Announcement], page: int, total_pages: int) -> str:
    lis = []
    for a in items:
        lis.append(
            f'      <li class="announcement-item" data-code="{html.escape(a.code)}">\n'
            f'        <a class="detail-link" href="{html.escape(a.filename)}">{html.escape(a.title)}</a>\n'
            f'        <span class="published-at">{html.escape(a.published_at)}</span>\n'
            f'      </li>'
        )
    nav_parts = []
    for p in range(1, total_pages + 1):
        if p == page:
            nav_parts.append(f'<span class="page-current">{p}</span>')
        else:
            nav_parts.append(f'<a class="page-link" href="list_{p}.html">{p}</a>')
    next_link = (
        f'<a class="page-next" href="list_{page + 1}.html">下一页</a>'
        if page < total_pages
        else '<span class="page-next-disabled">下一页</span>'
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>市政工程公告站 - 第 {page} 页</title>
</head>
<body>
  <h1>市政工程招投标公告列表（第 {page}/{total_pages} 页）</h1>
  <ul class="announcement-list" data-page="{page}">
{chr(10).join(lis)}
  </ul>
  <nav class="pagination">
    {''.join(nav_parts)}
    {next_link}
  </nav>
</body>
</html>
"""


def _record_page(r: ValidityRecord) -> str:
    label = RECORD_KIND_LABELS[r.kind]
    rows = [
        ("记录编号", r.record_id),
        ("记录类型", label),
        ("项目编号", r.project_code),
        ("标段号", r.section),
        ("投标人", r.bidder),
        ("生效时间", r.effective_at),
    ]
    if r.valid_until is not None:
        rows.append(("有效期至", r.valid_until))
    if r.ref is not None:
        rows.append(("引用记录编号", r.ref))
    if r.note:
        rows.append(("备注", r.note))

    trs = "\n".join(
        f'      <tr><th data-field="{html.escape(k)}">{html.escape(k)}</th>'
        f'<td data-value="{html.escape(k)}">{html.escape(v)}</td></tr>'
        for k, v in rows
    )
    attrs = (
        f'data-record-id="{html.escape(r.record_id)}" data-kind="{r.kind}"\n'
        f'           data-project="{html.escape(r.project_code)}" '
        f'data-section="{html.escape(r.section)}"\n'
        f'           data-bidder="{html.escape(r.bidder)}" '
        f'data-effective-at="{html.escape(r.effective_at)}"'
    )
    if r.valid_until is not None:
        attrs += f'\n           data-valid-until="{html.escape(r.valid_until)}"'
    if r.ref is not None:
        attrs += f'\n           data-ref="{html.escape(r.ref)}"'

    title = f"{label} {r.record_id}"
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)}</title>
</head>
<body>
  <article class="validity-record" {attrs}>
    <h1 class="record-title">{html.escape(title)}</h1>
    <table class="record-table">
{trs}
    </table>
    <p><a href="list_1.html">返回记录目录</a></p>
  </article>
</body>
</html>
"""


def _record_list_page(items: list[ValidityRecord], page: int, total_pages: int) -> str:
    lis = []
    for r in items:
        label = RECORD_KIND_LABELS[r.kind]
        lis.append(
            f'      <li class="record-item" data-record-id="{html.escape(r.record_id)}">\n'
            f'        <a class="record-link" href="{html.escape(r.filename)}">'
            f'{label} {html.escape(r.record_id)}（{html.escape(r.bidder)}）</a>\n'
            f'        <span class="effective-at">{html.escape(r.effective_at)}</span>\n'
            f'      </li>'
        )
    nav_parts = []
    for p in range(1, total_pages + 1):
        if p == page:
            nav_parts.append(f'<span class="page-current">{p}</span>')
        else:
            nav_parts.append(f'<a class="page-link" href="list_{p}.html">{p}</a>')
    next_link = (
        f'<a class="page-next" href="list_{page + 1}.html">下一页</a>'
        if page < total_pages
        else '<span class="page-next-disabled">下一页</span>'
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>报价有效期记录目录 - 第 {page} 页</title>
</head>
<body>
  <h1>报价有效期记录目录（第 {page}/{total_pages} 页）</h1>
  <ul class="record-list" data-page="{page}">
{chr(10).join(lis)}
  </ul>
  <nav class="pagination">
    {''.join(nav_parts)}
    {next_link}
  </nav>
</body>
</html>
"""


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 列表按发布时间倒序 —— 更正公告天然排在原公告前面，制造页面顺序干扰
    ordered = sorted(ANNOUNCEMENTS, key=lambda a: a.published_at, reverse=True)
    total_pages = (len(ordered) + PAGE_SIZE - 1) // PAGE_SIZE

    for a in ordered:
        (OUT_DIR / a.filename).write_text(_detail_page(a), encoding="utf-8")

    for page in range(1, total_pages + 1):
        chunk = ordered[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        (OUT_DIR / f"list_{page}.html").write_text(
            _list_page(chunk, page, total_pages), encoding="utf-8"
        )

    (OUT_DIR / "index.html").write_text(
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta http-equiv="refresh" content="0; url=list_1.html">'
        "<title>公告站</title></head><body>跳转中…</body></html>\n",
        encoding="utf-8",
    )

    # 报价有效期记录目录（独立分页目录，公告采集不会进入）
    RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    # 记录列表按生效时间倒序 —— 同一投标人的链条被打散到不同页，
    # 服务端不能依赖页面顺序构建有效期链
    records = sorted(VALIDITY_RECORDS, key=lambda r: r.effective_at, reverse=True)
    total_record_pages = (len(records) + RECORD_PAGE_SIZE - 1) // RECORD_PAGE_SIZE

    for r in records:
        (RECORDS_DIR / r.filename).write_text(_record_page(r), encoding="utf-8")

    for page in range(1, total_record_pages + 1):
        chunk = records[(page - 1) * RECORD_PAGE_SIZE: page * RECORD_PAGE_SIZE]
        (RECORDS_DIR / f"list_{page}.html").write_text(
            _record_list_page(chunk, page, total_record_pages), encoding="utf-8"
        )

    print(f"generated {len(ordered)} detail pages, {total_pages} list pages in {OUT_DIR}")
    print(
        f"generated {len(records)} validity records, "
        f"{total_record_pages} record list pages in {RECORDS_DIR}"
    )


if __name__ == "__main__":
    main()
