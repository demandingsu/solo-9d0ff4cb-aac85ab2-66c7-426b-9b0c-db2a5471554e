"""公告站采集与 HTML 解析。

公告流程：
1. 从用户提交的列表页开始，沿分页链接抓取全部列表页（去重、防环、限量）；
2. 收集列表页中全部详情页链接（同源、去重）；
3. 逐页抓取详情并解析为结构化公告。

报价有效期记录流程（collect_records）复用同一套分页遍历，从记录目录页
沿同站分页读取投标提交 / 有效期延长 / 撤回记录。

任一列表页或详情页读取失败（网络错误、非 2xx、HTML 缺关键字段）都抛出
CollectionError，由上层整体放弃本次采集，数据库保留上次成功结果。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .config import settings
from .timeutil import parse_time


class CollectionError(Exception):
    """采集过程中任一环节失败。"""


@dataclass
class ParsedAnnouncement:
    code: str
    title: str
    kind: str
    project_name: str
    published_at: str
    deadline: str | None
    corrects_code: str | None
    detail_url: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "title": self.title,
            "kind": self.kind,
            "project_name": self.project_name,
            "published_at": self.published_at,
            "deadline": self.deadline,
            "corrects_code": self.corrects_code,
            "detail_url": self.detail_url,
        }


@dataclass
class ParsedRecord:
    """报价有效期记录（投标提交 / 有效期延长 / 撤回）。"""

    record_id: str
    kind: str  # submission | extension | withdrawal
    project_code: str
    section: str
    bidder: str
    effective_at: str
    valid_until: str | None
    ref: str | None
    record_url: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "kind": self.kind,
            "project_code": self.project_code,
            "section": self.section,
            "bidder": self.bidder,
            "effective_at": self.effective_at,
            "valid_until": self.valid_until,
            "ref": self.ref,
            "record_url": self.record_url,
        }


def _same_origin(url: str, base: str) -> bool:
    u, b = urlparse(url), urlparse(base)
    return (u.scheme, u.netloc) == (b.scheme, b.netloc)


def _get(client: httpx.Client, url: str) -> str:
    try:
        resp = client.get(url)
    except httpx.HTTPError as exc:
        raise CollectionError(f"读取页面失败 {url}: {exc}") from exc
    if resp.status_code // 100 != 2:
        raise CollectionError(f"读取页面失败 {url}: HTTP {resp.status_code}")
    return resp.text


def _parse_list(html_text: str, list_url: str) -> tuple[set[str], set[str]]:
    soup = BeautifulSoup(html_text, "html.parser")
    detail_urls: set[str] = set()
    page_urls: set[str] = set()

    for a in soup.select("ul.announcement-list a.detail-link[href]"):
        href = urljoin(list_url, a["href"])
        if _same_origin(href, list_url):
            detail_urls.add(href)

    for a in soup.select("nav.pagination a[href]"):
        href = urljoin(list_url, a["href"])
        if _same_origin(href, list_url):
            page_urls.add(href)

    if not detail_urls:
        raise CollectionError(f"列表页 {list_url} 未解析到任何详情链接")
    return detail_urls, page_urls


def _field(soup: BeautifulSoup, label: str) -> str | None:
    cell = soup.select_one(f'th[data-field="{label}"] + td')
    if cell is None:
        return None
    text = cell.get_text(strip=True)
    return text or None


def _parse_detail(html_text: str, detail_url: str) -> ParsedAnnouncement:
    soup = BeautifulSoup(html_text, "html.parser")
    article = soup.select_one("article.announcement-detail")
    if article is None:
        raise CollectionError(f"详情页 {detail_url} 缺少 article.announcement-detail")

    code = article.get("data-code") or _field(soup, "公告编号")
    kind_raw = article.get("data-kind")
    published = article.get("data-published-at") or _field(soup, "发布时间")
    corrects = article.get("data-corrects") or _field(soup, "原公告编号")

    title_el = soup.select_one("h1.detail-title")
    title = title_el.get_text(strip=True) if title_el else None
    project_name = _field(soup, "项目名称")
    deadline = _field(soup, "投标截止时间")

    kind = {"original": "original", "correction": "correction"}.get(kind_raw or "", "")
    if not kind:
        label = _field(soup, "公告类型") or ""
        kind = "correction" if "更正" in label else "original"

    missing = [
        name
        for name, val in (
            ("公告编号", code),
            ("公告标题", title),
            ("项目名称", project_name),
            ("发布时间", published),
        )
        if not val
    ]
    if missing:
        raise CollectionError(f"详情页 {detail_url} 缺少必填字段: {', '.join(missing)}")
    if kind == "correction" and not corrects:
        raise CollectionError(f"更正公告 {code}（{detail_url}）缺少原公告编号")

    return ParsedAnnouncement(
        code=code.strip(),
        title=title.strip(),
        kind=kind,
        project_name=project_name.strip(),
        published_at=published.strip(),
        deadline=deadline.strip() if deadline else None,
        corrects_code=corrects.strip() if corrects else None,
        detail_url=detail_url,
    )


# ---------------------------------------------------------------------------
# 报价有效期记录目录
# ---------------------------------------------------------------------------
_RECORD_KINDS = {"submission", "extension", "withdrawal"}

# 记录类型中文标签 -> 规范类型；注意“撤回”优先于“延长”（“撤回延长记录”是撤回）
_RECORD_KIND_LABELS = (
    ("撤回", "withdrawal"),
    ("延长", "extension"),
    ("提交", "submission"),
)


def _record_kind(kind_raw: str | None, label: str | None, record_url: str) -> str:
    if kind_raw:
        kind = kind_raw.strip().lower()
        if kind in _RECORD_KINDS:
            return kind
        raise CollectionError(f"记录页 {record_url} 的记录类型无法识别: {kind_raw}")
    text = label or ""
    for key, kind in _RECORD_KIND_LABELS:
        if key in text:
            return kind
    raise CollectionError(f"记录页 {record_url} 的记录类型无法识别: {label or '(缺失)'}")


def _parse_record_list(html_text: str, list_url: str) -> tuple[set[str], set[str]]:
    soup = BeautifulSoup(html_text, "html.parser")
    record_urls: set[str] = set()
    page_urls: set[str] = set()

    for a in soup.select("ul.record-list a.record-link[href]"):
        href = urljoin(list_url, a["href"])
        if _same_origin(href, list_url):
            record_urls.add(href)

    for a in soup.select("nav.pagination a[href]"):
        href = urljoin(list_url, a["href"])
        if _same_origin(href, list_url):
            page_urls.add(href)

    if not record_urls:
        raise CollectionError(f"记录目录页 {list_url} 未解析到任何记录链接")
    return record_urls, page_urls


def _require_time(text: str, field: str, record_url: str) -> None:
    try:
        parse_time(text)
    except ValueError:
        raise CollectionError(
            f"记录页 {record_url} 的{field}格式无效: {text}"
        ) from None


def _parse_record(html_text: str, record_url: str) -> ParsedRecord:
    soup = BeautifulSoup(html_text, "html.parser")
    article = soup.select_one("article.validity-record")
    if article is None:
        raise CollectionError(f"记录页 {record_url} 缺少 article.validity-record")

    record_id = article.get("data-record-id") or _field(soup, "记录编号")
    project_code = article.get("data-project") or _field(soup, "项目编号")
    section = article.get("data-section") or _field(soup, "标段号")
    bidder = article.get("data-bidder") or _field(soup, "投标人")
    effective = article.get("data-effective-at") or _field(soup, "生效时间")
    valid_until = article.get("data-valid-until") or _field(soup, "有效期至")
    ref = article.get("data-ref") or _field(soup, "引用记录编号")
    kind = _record_kind(article.get("data-kind"), _field(soup, "记录类型"), record_url)

    missing = [
        name
        for name, val in (
            ("记录编号", record_id),
            ("项目编号", project_code),
            ("标段号", section),
            ("投标人", bidder),
            ("生效时间", effective),
        )
        if not val
    ]
    if kind in ("submission", "extension") and not valid_until:
        missing.append("有效期至")
    if kind in ("extension", "withdrawal") and not ref:
        missing.append("引用记录编号")
    if missing:
        raise CollectionError(f"记录页 {record_url} 缺少必填字段: {', '.join(missing)}")

    _require_time(effective, "生效时间", record_url)
    if valid_until:
        _require_time(valid_until, "有效期至", record_url)

    return ParsedRecord(
        record_id=record_id.strip(),
        kind=kind,
        project_code=project_code.strip(),
        section=section.strip(),
        bidder=bidder.strip(),
        effective_at=effective.strip(),
        valid_until=valid_until.strip() if valid_until else None,
        ref=ref.strip() if ref else None,
        record_url=record_url,
    )


def _crawl(
    start_url: str,
    parse_list: Callable[[str, str], tuple[set[str], set[str]]],
    parse_detail: Callable[[str, str], Any],
) -> list[dict[str, Any]]:
    """从 start_url 出发沿分页遍历全部列表页，再逐页抓取详情（任一失败即整体失败）。"""
    headers = {"User-Agent": settings.user_agent}
    with httpx.Client(
        timeout=settings.request_timeout, follow_redirects=True, headers=headers
    ) as client:
        # 1) 遍历全部列表页
        seen_pages: set[str] = set()
        queue: list[str] = [start_url]
        detail_urls: set[str] = set()

        while queue:
            if len(seen_pages) >= settings.max_list_pages:
                raise CollectionError(
                    f"列表页数量超过上限 {settings.max_list_pages}，疑似分页环"
                )
            page_url = queue.pop(0)
            if page_url in seen_pages:
                continue
            seen_pages.add(page_url)

            html_text = _get(client, page_url)
            details, more_pages = parse_list(html_text, page_url)
            detail_urls.update(details)
            for u in more_pages:
                if u not in seen_pages and u not in queue:
                    queue.append(u)

        # 2) 逐页抓取详情 —— 任一失败即整体失败
        items: list[dict[str, Any]] = []
        for detail_url in sorted(detail_urls):  # 排序仅为日志/测试的确定性
            html_text = _get(client, detail_url)
            items.append(parse_detail(html_text, detail_url).as_dict())

    return items


def collect(list_url: str) -> list[dict[str, Any]]:
    """抓取整个公告站并返回结构化公告列表（顺序不保证，归并层不依赖顺序）。"""
    return _crawl(list_url, _parse_list, _parse_detail)


def collect_records(records_url: str) -> list[dict[str, Any]]:
    """抓取报价有效期记录目录，返回投标提交/延长/撤回记录列表。"""
    return _crawl(records_url, _parse_record_list, _parse_record)
