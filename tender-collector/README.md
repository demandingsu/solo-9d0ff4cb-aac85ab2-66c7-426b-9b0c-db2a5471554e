# 市政工程投标截止时间采集服务

面向招投标专员的公告采集 API：提交市政工程公告站的**列表页地址**，服务自动遍历
所有分页与详情页，将**更正公告按其引用的原公告编号归并**到同一项目，并**按发布
时间**（而非页面排列顺序）确定当前生效的投标截止时间，标记是否已过期。

另提供**投标有效期核验 API**：投标专员发出中标通知前，提交公告列表地址、项目
编号、标段号、报价有效期记录目录地址与拟发时间，服务复用公告链得到截标时间，
沿记录目录分页读取投标提交/有效期延长/撤回记录，逐投标人判定拟发时刻报价是否
仍有效。

## 核心规则

1. **分页遍历**：从提交的列表页出发，沿 `nav.pagination` 链接抓取全部列表页
   （去重、防环、限量），收集所有详情页链接后逐页抓取。
2. **更正归并**：更正公告通过"原公告编号"归并到对应原公告所在项目；引用悬空
   （原公告不存在/不是原公告）视为数据错误，本次采集整体失败。
3. **生效截止时间**：同一项目内按**发布时间**排序，取发布时间最新且声明了
   截止时间的公告；发布时间相同时按公告编号确定次序，结果与页面顺序完全无关。
   若最新更正公告未给出截止时间（例如只改联系人），沿用上一份带截止时间的公告。
4. **过期判定**：`is_expired = 截止时间 <= 当前时间`（截止时间按服务器本地时区
   的 naive 时间比较，与公告页 `YYYY-MM-DD HH:MM` 格式一致）。
5. **幂等采集**：公告以编号为唯一键 UPSERT，重复采集不产生重复记录。
6. **失败保留**：任一列表页/详情页读取失败（网络错误、非 2xx、字段缺失）或
   归并校验失败时，本次采集不写入任何公告数据，**完整保留上次成功采集结果**；
   失败原因记入 `collection_runs` 表并通过 HTTP 422 返回。

## 投标有效期核验规则

1. **截标时间**：复用上述公告链（采集 + 更正归并）得到该项目当前生效的投标
   截止时间；项目编号即原公告编号。
2. **链起点**：以投标人**截标前**（生效时间 < 截标时间）的投标提交为有效期
   链起点，起点必须唯一；晚于截标时间的提交不参与判定。
3. **延长**：延长记录必须引用**同一投标人**链条上的**上一条记录**，且生效
   时间 ≤ 拟发时间才计入；拟发时间之后才生效的延长视为尚未生效，直接忽略。
4. **撤回**：生效时间 ≤ 拟发时间的撤回记录使其引用的记录及链上后续记录全部
   失效，有效期回到上一条未被撤回的记录；投标提交本身被撤回则该投标不再
   有效（判定为已失效）。
5. **逐投标人结论**：`valid`（最终有效期 > 拟发时间）、`expired`（已失效，
   含投标被撤回）、`undetermined`（无法判定），并给出最终有效期与来源记录。
6. **不猜结论**：引用断链（引用不存在/他人记录/非上一条记录/撤回记录）、
   同一投标人同一时刻出现多条记录等冲突情形，该投标人判定为无法判定并附
   原因；任一公告页/记录页读取失败时整个请求以 422 报错，不返回任何结论。
7. **只读**：核验不写数据库，每次请求实时抓取公告站与记录目录。

## 目录结构

```
tender-collector/
├── app/                    # FastAPI 应用
│   ├── main.py             # 路由：/api/collect、/api/projects、/api/collections、/api/verify-validity
│   ├── service.py          # 采集编排 + 事务边界 + 过期判定 + 有效期核验编排
│   ├── scraper.py          # httpx + BeautifulSoup 抓取/解析（公告 + 有效期记录）
│   ├── merge.py            # 更正归并与生效截止时间判定（纯函数）
│   ├── validity.py         # 投标有效期链判定（纯函数）
│   ├── timeutil.py         # 统一时间解析
│   ├── database.py         # SQLite 连接、建表、UPSERT
│   ├── models.py           # Pydantic 模型
│   └── config.py           # 环境变量配置
├── demo_site/              # 生成的本地分页 HTML 示例公告站（静态文件）
│   └── validity/           # 报价有效期记录目录（投标提交/延长/撤回）
├── scripts/generate_demo_site.py  # 示例站生成器（数据可复现）
├── tests/test_collection.py       # 采集端到端 + 归并单元测试
├── tests/test_validity.py         # 有效期核验端到端 + 链判定单元测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 快速开始（Docker Compose）

```bash
# 1. 生成示例公告站静态文件（已随仓库提供，可重复生成）
python3 scripts/generate_demo_site.py

# 2. 启动：API 在 :8000，示例公告站在 :8001
docker compose up --build -d

# 3. 提交列表页地址触发采集
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://demo-site/list_1.html"}'

# 4. 查询项目当前截止时间
curl http://localhost:8000/api/projects

# 5. 核验某标段各投标报价在拟发时刻是否仍有效
curl -X POST http://localhost:8000/api/verify-validity \
  -H 'Content-Type: application/json' \
  -d '{
        "list_url": "http://demo-site/list_1.html",
        "project_code": "SZ-2026-002",
        "section": "一标段",
        "records_url": "http://demo-site/validity/list_1.html",
        "planned_at": "2026-12-25 10:00"
      }'
```

容器内 API 通过 compose 网络访问示例站（主机名 `demo-site`）；从宿主机浏览
示例站则打开 <http://localhost:8001/list_1.html>。SQLite 数据存于命名卷
`tender-data`（容器内 `/data/tenders.db`）。

停止与清理：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除采集数据
```

## 本地开发运行（不使用 Docker）

```bash
python3 -m pip install -r requirements-dev.txt
python3 scripts/generate_demo_site.py

# 终端 A：示例公告站
python3 -m http.server 8001 --directory demo_site

# 终端 B：API（数据库默认在 ./data/tenders.db）
uvicorn app.main:app --reload --port 8000

# 终端 C：测试
pytest -q

# 采集
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://127.0.0.1:8001/list_1.html"}'

# 投标有效期核验
curl -X POST http://localhost:8000/api/verify-validity \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://127.0.0.1:8001/list_1.html",
       "project_code": "SZ-2026-002", "section": "一标段",
       "records_url": "http://127.0.0.1:8001/validity/list_1.html",
       "planned_at": "2026-12-25 10:00"}'
```

交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>

## API 说明

### `POST /api/collect` — 提交列表页，触发采集

请求：

```json
{ "list_url": "http://demo-site/list_1.html" }
```

成功响应（200）：

```json
{
  "status": "success",
  "source_url": "http://demo-site/list_1.html",
  "collected": 10,
  "projects": [
    {
      "project_name": "城北污水处理厂扩建工程",
      "current_deadline": "2026-11-30 10:00",
      "deadline_source_code": "SZ-2026-002-G2",
      "deadline_source_url": "http://demo-site/SZ-2026-002-G2.html",
      "is_expired": false
    }
  ]
}
```

失败响应（422，任一页面不可读/数据无法归并）：

```json
{ "detail": "读取页面失败 http://demo-site/SZ-2026-002-G2.html: HTTP 404" }
```

### `GET /api/projects` — 查询全部项目的当前截止时间

返回字段：项目名称、当前生效截止时间、截止时间来源公告编号、来源链接、是否过期。

### `GET /api/collections?limit=20` — 采集运行历史

包含每次采集的成功/失败状态、采集条数与错误信息。

### `POST /api/verify-validity` — 投标有效期核验

发出中标通知前，确认某标段各投标报价在拟发时刻是否仍有效。服务复用公告链
得到截标时间，沿记录目录分页读取全部投标提交/有效期延长/撤回记录后逐
投标人判定（只读，不写库）。

请求：

```json
{
  "list_url": "http://demo-site/list_1.html",
  "project_code": "SZ-2026-002",
  "section": "一标段",
  "records_url": "http://demo-site/validity/list_1.html",
  "planned_at": "2026-12-25 10:00"
}
```

- `project_code`：项目编号，即该项目原公告的公告编号；
- `records_url`：报价有效期记录目录的某一列表页地址，服务自动跟随同站分页；
- `planned_at`：拟发中标通知时间，格式 `YYYY-MM-DD HH:MM`。

成功响应（200，节选）：

```json
{
  "project_code": "SZ-2026-002",
  "project_name": "城北污水处理厂扩建工程",
  "section": "一标段",
  "deadline": "2026-11-30 10:00",
  "deadline_source_code": "SZ-2026-002-G2",
  "deadline_source_url": "http://demo-site/SZ-2026-002-G2.html",
  "planned_at": "2026-12-25 10:00",
  "records_read": 17,
  "bidders": [
    {
      "bidder": "宏远建设集团有限公司",
      "status": "valid",
      "final_valid_until": "2027-02-28 10:00",
      "source_record_code": "VR-003",
      "source_record_url": "http://demo-site/validity/VR-003.html",
      "reason": null
    },
    {
      "bidder": "中港湾工程局有限公司",
      "status": "undetermined",
      "final_valid_until": null,
      "source_record_code": null,
      "source_record_url": null,
      "reason": "延长记录 VR-010 未引用同一投标人的上一条记录 VR-009（引用断链）"
    }
  ]
}
```

`status` 取值：`valid`（拟发时刻仍有效）/ `expired`（已失效，含投标被
撤回）/ `undetermined`（无法判定，`reason` 说明原因）。`final_valid_until`
为链上最后一条生效记录的有效期至；`source_record_code` / `source_record_url`
为得出该结论所依据的记录。

失败响应：

- 404：公告链中不存在该项目编号；
- 422：公告页/记录页读取失败、记录缺字段或时间格式非法、`planned_at`
  格式非法。此时不返回任何投标人结论。

## 示例站数据设计

`demo_site/` 含 5 个项目、10 条公告、3 个列表页，列表按发布时间**倒序**排列
（更正公告排在原公告之前），专门用于验证归并不受页面顺序影响：

| 项目 | 场景 | 生效截止时间来源 |
| --- | --- | --- |
| 滨河路市政改造工程 | 一次更正延期 | 更正公告 SZ-2026-001-G1 |
| 城北污水处理厂扩建工程 | 两次更正，取最新一次 | 第二次更正 SZ-2026-002-G2 |
| 东风桥加固维修工程 | 无更正，已过期 | 原公告 SZ-2026-003 |
| 高新区雨污分流管网工程 | 更正不改截止时间 | 原公告 SZ-2026-004 |
| 湖滨公园配套市政道路工程 | 原公告在末页、更正在首页（跨页乱序） | 更正公告 SZ-2026-005-G1 |

`demo_site/validity/` 含 17 条报价有效期记录、3 个目录页，列表按生效时间
**倒序**排列（同一投标人的有效期链被打散到不同分页）。以项目 SZ-2026-002
一标段、拟发时间 `2026-12-25 10:00` 为例：

| 投标人 | 场景 | 判定 |
| --- | --- | --- |
| 宏远建设集团有限公司 | 提交 + 两次延长，链条完整 | 有效（至 2027-02-28，来源 VR-003） |
| 长江路桥工程有限公司 | 延长 VR-005 被 VR-006 撤回 | 已失效（回到 VR-004 的 2026-12-20） |
| 天成市政工程有限公司 | 延长 VR-008 拟发时间后才生效，忽略 | 有效（至 2027-01-30，来源 VR-007） |
| 中港湾工程局有限公司 | 延长引用不存在的 VR-099 | 无法判定（引用断链） |
| 北方交通建设工程有限公司 | 两条延长同一时刻生效 | 无法判定（同一时刻冲突） |
| 远航基础工程有限公司 | 投标提交被 VR-015 撤回 | 已失效（来源 VR-015） |

另有 VR-016（同项目二标段）、VR-017（其他项目）用于验证项目编号 + 标段号
过滤。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TENDER_DB_PATH` | 本地 `./data/tenders.db`；容器内 `/data/tenders.db` | SQLite 文件路径 |
| `TENDER_HTTP_TIMEOUT` | `15` | 单个页面请求超时（秒） |
| `TENDER_MAX_LIST_PAGES` | `200` | 单次采集最多遍历的列表页数（防环） |
| `TENDER_USER_AGENT` | `TenderCollector/1.0 ...` | 请求 UA |

## 解析约定（接入真实站点时）

解析器依赖以下稳定选择器，适配其他站点时调整 `app/scraper.py` 即可：

- 列表页：`ul.announcement-list a.detail-link` 为详情链接，
  `nav.pagination a` 为分页链接；
- 详情页：`article.announcement-detail` 容器，支持
  `data-code` / `data-kind` / `data-published-at` / `data-corrects` 属性，
  以及 `table.detail-table` 内 `th[data-field="字段名"] + td` 表格字段
  （公告编号、公告类型、项目名称、发布时间、投标截止时间、原公告编号）；
- 记录目录页：`ul.record-list a.record-link` 为记录链接，
  `nav.pagination a` 为分页链接；
- 记录页：`article.validity-record` 容器，支持
  `data-record-id` / `data-kind`（`submission` / `extension` / `withdrawal`）/
  `data-project` / `data-section` / `data-bidder` / `data-effective-at` /
  `data-valid-until` / `data-ref` 属性，以及 `table.record-table` 内
  `th[data-field="字段名"] + td` 表格字段（记录编号、记录类型、项目编号、
  标段号、投标人、生效时间、有效期至、引用记录编号）。
