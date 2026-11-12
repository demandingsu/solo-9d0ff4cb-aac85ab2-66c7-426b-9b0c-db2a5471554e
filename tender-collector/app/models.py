from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from .timeutil import parse_time


class CollectRequest(BaseModel):
    list_url: str = Field(
        ..., min_length=1, description="公告列表页地址（服务会自动跟随分页链接）"
    )


class ProjectOut(BaseModel):
    project_code: str
    project_name: str
    current_deadline: str
    deadline_source_code: str
    deadline_source_url: str
    is_expired: bool


class CollectResponse(BaseModel):
    status: str
    source_url: str
    collected: int
    projects: list[ProjectOut]


class RunOut(BaseModel):
    id: int
    source_url: str
    status: str
    items_collected: int
    error: str | None
    started_at: str
    finished_at: str


class ValidityCheckRequest(BaseModel):
    list_url: str = Field(
        ..., min_length=1, description="公告列表页地址（复用公告链得到截标时间）"
    )
    project_code: str = Field(
        ..., min_length=1, description="项目编号（即原公告编号）"
    )
    section: str = Field(..., min_length=1, description="标段号")
    records_url: str = Field(
        ..., min_length=1, description="报价有效期记录目录页地址（服务会自动跟随分页）"
    )
    planned_at: str = Field(
        ..., min_length=1, description="拟发中标通知时间，格式 YYYY-MM-DD HH:MM"
    )

    @field_validator("planned_at")
    @classmethod
    def _planned_at_parseable(cls, v: str) -> str:
        try:
            parse_time(v)
        except ValueError:
            raise ValueError(
                f"planned_at 时间格式无效（应为 YYYY-MM-DD HH:MM）: {v}"
            ) from None
        return v


class BidderValidityOut(BaseModel):
    bidder: str
    status: str = Field(
        ..., description="valid=有效 / expired=已失效 / undetermined=无法判定"
    )
    final_valid_until: str | None
    source_record_code: str | None
    source_record_url: str | None
    reason: str | None


class ValidityCheckResponse(BaseModel):
    project_code: str
    project_name: str
    section: str
    deadline: str
    deadline_source_code: str
    deadline_source_url: str
    planned_at: str
    records_read: int
    bidders: list[BidderValidityOut]
