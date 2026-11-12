"""统一的时间解析：公告站使用服务器本地时区的 naive 时间（YYYY-MM-DD HH:MM）。"""
from __future__ import annotations

from datetime import datetime

TIME_FORMAT = "%Y-%m-%d %H:%M"


def parse_time(text: str) -> datetime:
    """解析 ``YYYY-MM-DD HH:MM``，容错带秒或 ISO 形式；无法解析时抛 ValueError。"""
    text = text.strip()
    try:
        return datetime.strptime(text, TIME_FORMAT)
    except ValueError:
        return datetime.fromisoformat(text)
