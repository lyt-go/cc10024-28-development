# 网页行为采集与内容清洗平台

面向网页行为录制、脚本化采集、内容清洗与结构化输出的数据采集与加工平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 行为脚本 | `behavior_script` | 行为脚本 | 脚本编号、目标站点、录制步骤 |
| 元素定位 | `element_selector` | 元素锚点 | 锚点编号、关联脚本、选择器类型 |
| 数据抽取 | `data_extract` | 抽取模板 | 模板编号、关联脚本、抽取字段 |
| 清洗管道 | `clean_pipeline` | 清洗工序 | 工序编号、输入源、清洗规则 |
| 转换规则 | `transform_rule` | 转换规则 | 规则编号、关联工序、源字段 |
| 质量防护 | `quality_guard` | 质量规则 | 规则编号、检核对象、检核维度 |
| 脚本调度 | `script_schedule` | 调度计划 | 计划编号、关联脚本、触发条件 |
| 登录凭证 | `login_mgr` | 登录凭证 | 凭证编号、目标站点、认证方式 |
| 页面快照 | `page_snapshot` | 页面快照 | 快照编号、采集来源、页面版本 |
| 错误恢复 | `error_recover` | 恢复任务 | 任务编号、关联计划、失败步骤 |
| 页面去重 | `page_dedup` | 去重规则 | 规则编号、去重维度、特征字段 |
| 字段归一化 | `field_normalize` | 归一规则 | 规则编号、关联字段、归一类型 |
| 批次队列 | `batch_queue` | 采集批次 | 批次编号、关联计划、任务数量 |
| 画布配置 | `canvas_config` | 画布配置 | 配置编号、关联站点、分辨率 |
| 导出通道 | `export_channel` | 导出配置 | 通道编号、输出格式、目标路径 |
| 运行历史 | `run_history` | 运行记录 | 记录编号、关联计划、开始时间 |
| Schema校验 | `schema_check` | Schema规则 | 规则编号、目标数据、字段名 |
| 内容缓存 | `content_cache` | 缓存配置 | 配置编号、缓存类型、过期策略 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 来源字段校验与受控共享写入

数据抽取在“按目标模块取规则”的旧 Schema 校验之外，新增按来源站点执行的
来源字段校验（`backend/app/routers/ingestion.py`，前缀 `/api/ingestion`）：

- 规则归属：`schema_check` 规则带「归属站点」，站点值为专属规则（仅限录入时
  登记的站点维护），`shared` 为共享数据集的共用定义；站点之间写入权限独立，
  不能改动别站的共享范围。
- 站点字段校验回执：同一来源一次出具两层结论，各自带实用规则与原值——
  - 站点层：有专属规则用专属规则，否则回落到共用定义；
  - 共享层：公共编号列只接收确定整数，可解析的文本（如 `"007"`）判为
    “待转换”，判定前保留原值与前导零，转换确认前不得进入该列。
- 数据抽取据回执分流：站点层通过 → 站点结构化行（本地可接收行）；待转换 →
  待转换来源；越权写入与拦截 → 拦截清单（保留原值与适用规则）。
- 相同来源重送幂等，不增加产出行；回执不可变，规则更新只影响新回执，
  复查旧回执（`/receipts/recheck`）不会改动原结果。
- 旧口径 `validate_record` 完整保留：`int` 约束仅检验可解析性，文本 `"007"`
  在旧校验中仍通过。

| 接口 | 说明 |
| --- | --- |
| `POST /api/ingestion/rules` | 登记来源校验规则（专属/共用） |
| `POST /api/ingestion/rules/{id}` | 归属站点更新专属规则 |
| `POST /api/ingestion/receipts` | 仅出具两层结论的站点字段校验回执 |
| `POST /api/ingestion/receipts/recheck` | 复查旧回执（原结果不变，附现行规则评估） |
| `POST /api/ingestion/sources` | 采集来源并分流为结构化行/待转换/拦截 |
| `POST /api/ingestion/conversions/confirm` | 归属站点确认待转换文本 |
| `POST /api/ingestion/shared-writes` | 按回执受控写入共享数据集（校验归属） |
| `GET /api/ingestion/views/{local-rows,shared-rows,pending,interceptions}` | 采集产物视图 |

领域：数据工程
子方向：数据采集、爬虫与RPA
