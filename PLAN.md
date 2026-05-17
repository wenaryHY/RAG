# RAG 系统优化 — 实施计划

> 创建：2026-05-17
> 状态：执行中
> 基线档案：`C:\Users\Wenary\Desktop\RAG系统-完整项目档案与优化计划.md`

---

## 背景与现状核查（2026-05-17 实测）

档案与磁盘/进程/容器存在严重不同步，已识别问题：

| 项 | 档案声称 | 实测 | 处置 |
|---|---|---|---|
| 项目根 | `D:\RAGFlow\` 等扁平路径 | 实际全部位于 `D:\RAG\` 下 | 全部统一到 `D:\RAG\` |
| Scheduler 进程 | 在跑 | PID 21480 持着已迁走的 `D:\RAG-Scheduler\main.py` inode | Phase 1 切换到 rag-core |
| Audit 进程 | 在跑 | PID 33968 同上 | 同上 |
| VBS 自启 | 指向 `D:\RAG-Scheduler\main.py` | 路径已失效 | Phase 3 用 NSSM 替代 |
| daemon.json 镜像源 | 已配 1ms.run / xuanyuan.me | 实际只有 `builder.gc` | Phase 0 修复 |
| RAGFlow 容器栈 | restart unless-stopped 跑着 | `docker ps` 为空，端口全关 | Phase 0 重启 |
| LM Studio | 按需启动 | 已 `--run-as-service` 常驻 | 保持，路由层降级为 fallback-only |
| 密钥文件 | `D:\private\密钥.txt` | 存在但解析靠模糊匹配，Audit 源码里路径 mojibake | Phase 1 迁 `keys.ini` |

**镜像已就绪**：ragflow:v0.25.4 / infinity:v0.7.0-dev7 / mysql:8.0.39 / minio / valkey 都在本地 docker 缓存中。

---

## 决策（用户已确认）

1. 先恢复 RAGFlow 现状再谈优化
2. 服务合并为 **rag-core + rag-tray** 两个进程
3. 启动方式全部迁到 **NSSM Windows 服务**
4. 审计成本控制：**三级漏斗 embedding → Flash → Opus**
5. AI 文件夹建议砍掉，改为**元数据驱动检索**
6. 输出可执行实施计划（本文档）

---

## 总体目标架构

```
Windows 启动
 ├─ Docker Desktop service (auto)
 │    └─ RAGFlow stack (compose, restart: unless-stopped)
 ├─ NSSM service: rag-core      → 127.0.0.1:8840
 │    ├─ /scheduler/*  (原 8850)
 │    ├─ /audit/*      (原 8860)
 │    ├─ /sync/*       (新 FileSync)
 │    └─ /api/*, /ui   (Panel)
 └─ Task Scheduler: rag-tray (用户登录时启动，pystray，调 rag-core)

D:\RAG\
 ├─ PLAN.md                  本文档
 ├─ config.toml              单一配置源
 ├─ state.sqlite             同步 + 查询日志 + 审计缓存
 ├─ rag-core\                合并后的服务
 ├─ rag-tray\                托盘
 ├─ RAGFlow\docker\          原栈
 ├─ RAGfiles\<library>\      文件投递目录
 ├─ logs\
 └─ reports\                 审计报告
```

**端口表（精简）**

| 端口 | 用途 |
|---|---|
| 80 / 443 / 9380 | RAGFlow (Web/HTTP API) |
| 1234 | LM Studio (按需) |
| 8840 | rag-core 唯一入口 |
| ~~8850 / 8860~~ | 退役 |

---

## Phase 0 — 现状归一

**目标**：让 RAGFlow 重新跑起来，建立单一配置源；不动现有 Scheduler/Audit 代码。

### P0-1 写入 PLAN.md 留档（本文档）

### P0-2 git snapshot
```powershell
cd D:\RAG
git add -A
git commit -m "phase-0: snapshot before refactor"
```
**验收**：`git log --oneline -1` 看到提交。

### P0-3 修复 daemon.json 镜像源
追加 `registry-mirrors`，保留原 `builder.gc`。

### P0-4 重启 Docker engine
让 daemon.json 生效。重启后等待 com.docker.service Running。

### P0-5 docker compose up RAGFlow
```powershell
cd D:\RAG\RAGFlow\docker
docker compose --profile infinity --profile cpu up -d
```
**验收**：
- `docker compose ps` 五个容器全部 `Up`
- `curl http://127.0.0.1:9380/v1/health` 200
- `curl http://127.0.0.1/` 返回 HTML

### P0-6 创建 D:\RAG\config.toml
单一配置源（现阶段先放进去，rag-core 实现后再读）。

### P0-7 端到端验收
- [ ] PLAN.md 存在
- [ ] git 提交存在
- [ ] daemon.json 含 registry-mirrors
- [ ] 5 个 RAGFlow 容器全 Up
- [ ] 9380 / 80 / 23820 / 9001 端口监听
- [ ] config.toml 存在

---

## Phase 1 — rag-core（合并服务）

### 1.1 目录结构
```
D:\RAG\rag-core\
 ├─ main.py                uvicorn entrypoint
 ├─ config.py              读 D:\RAG\config.toml
 ├─ db.py                  sqlite + sqlmodel
 ├─ ragflow_client.py      封装 9380 REST API
 ├─ scheduler\             /scheduler/*
 ├─ audit\                 /audit/*（funel 留 Phase 4）
 ├─ sync\                  /sync/*（新增）
 ├─ panel\                 /api/*, /ui
 ├─ requirements.txt
 └─ tests\
```

### 1.2 关键设计
- **配置统一**：所有路径、端口、阈值从 `config.toml` 读
- **状态持久化**：`state.sqlite` 表 `files / query_logs / audit_runs / audit_pair_cache`
- **密钥迁移**：`D:\private\密钥.txt` → `D:\private\keys.ini`（标准 INI）
- **FileSync 单向 + 对账**：watchdog + 启动期 reconcile + 30s 轮询；防 split-brain
- **Provider 标准化**：scheduler/providers.py 统一 deepseek / openrouter / xstx / lmstudio 客户端
- **新建知识库强制中文**：调用 RAGFlow 建库时 `parser_config.language="Chinese"`

### 1.3 RAGFlow API 路径校对
实施前先 `curl http://127.0.0.1:9380/v1/openapi.json` 抓真实路径，覆盖档案不准的 3.2 节端点表。

### 1.4 验收
- `curl http://127.0.0.1:8840/scheduler/health` 200
- `curl http://127.0.0.1:8840/audit/health` 200
- `curl http://127.0.0.1:8840/sync/status` watcher running
- 投递 `D:\RAG\RAGfiles\dev\test.txt` 60s 内出现在 dev 库
- 在 RAGFlow Web UI 创建 `test-lib`，60s 内本地出现 `D:\RAG\RAGfiles\test-lib\`

---

## Phase 2 — rag-tray + Web Panel

### 2.1 rag-tray
- pystray + winrt-toast
- 15s 轮询 `/api/services/health`，绿/黄/红切换
- 右键菜单：打开面板 / 立即同步 / 运行审计 / 服务状态 / 退出
- 双击：打开 `http://127.0.0.1:8840/`

### 2.2 Web Panel（单文件 HTML）
- /dashboard：服务卡片 + 最近同步 + 各库文档数 + 快捷按钮
- /sync：SSE 实时日志 + 拖拽上传 + 队列
- /datasets：表格 + 内嵌检索 + 触发重解析
- /audit：报告列表 + 详情抽屉 + 立即运行 + 趋势 sparkline
- /settings：模型配置只读 + 开关 + 链接到 config.toml

### 2.3 验收
- 浏览器打开仪表盘正常
- 托盘图标颜色反映健康
- 拖拽 PDF 看到 upload→parse→done 完整流

---

## Phase 3 — NSSM 服务化

### 3.1 注册
- `rag-core` → NSSM Windows Service（auto，DependOnService=com.docker.service）
- `rag-tray` → Task Scheduler（At log on of Wenary，run-as user，**不能用 NSSM**，原因：托盘图标必须在用户会话）

### 3.2 下线旧机制
- 删 `Startup\RAGScheduler.vbs`
- 删桌面 `RAG-Manager.bat` `RAG-Shutdown.ps1`
- 桌面新建快捷方式：`http://127.0.0.1:8840/`

### 3.3 验收
- `Get-Service rag-core` Running / Auto
- 重启 Windows 后登录 30s 内全绿可访问

---

## Phase 4 — 审计三级漏斗

### 4.1 流程
```
全量 chunks
  ─ Stage1 SiliconFlow bge-m3 embed (FREE)
            cosine ≥ 0.82 的 cross-doc pair 保留
  ─ Stage2 Top-K pair → DeepSeek Flash
            "yes/no/maybe" 二分类（≈¥0.001/对）
  ─ Stage3 yes/maybe → Claude Opus 4.7
            最终冲突分析（含原文、严重度、建议）

单文档错误检测：每文档采样 3 个 chunk → Opus
```

### 4.2 成本上限
- `audit.opus_calls_per_run_max = 100` 硬截断
- `audit_pair_cache (sha_a, sha_b)` 缓存，未变文件跨周复用

### 4.3 自动调度
- APScheduler cron `0 2 * * 0`（周日 02:00）
- 完成 SSE → 托盘 winrt-toast
- 仪表盘红点 = 最近一次 `findings_count > 0` 且 `seen=false`

### 4.4 验收
- 手工 `POST /audit/run` 全库：
  - Stage1 几千对 → Stage2 ≤ 200 → Stage3 ≤ 100
  - 报告 `cost_estimate < ¥5`
- 第二次跑：缓存命中，Opus 调用接近 0

---

## Phase 5 — 元数据驱动检索 + 收尾

### 5.1 替代档案 3.5（AI 文件夹建议）
FileSync 上传时塞 metadata：
```json
{
  "source_path": "D:\\RAG\\RAGfiles\\pharmacy\\2024\\foo.pdf",
  "ingest_dir": "pharmacy/2024",
  "filename_tokens": ["药二星", "试卷3", "2024"],
  "ingested_at": "...",
  "sha256": "..."
}
```
调度层 `/scheduler/query` 支持 `filters` 参数按 metadata 过滤。

### 5.2 档案对齐
重写 `RAG系统-完整项目档案与优化计划.md`：
- 第二部分路径全改 `D:\RAG\...`
- 加入 rag-core / rag-tray
- 标记 4.x（AI 文件夹）已废
- 第七部分痛点 1 / 5 / 8 已解决，删除

### 5.3 端到端回归
- 冷启动 10 分钟内全绿
- 投递 PDF 2 分钟内入库
- Panel 提问路由到合适模型，带引用
- 立即审计 5 分钟内出报告，Opus < 50
- 断网 OpenRouter → 自动 fallback LM Studio
- 重启系统：服务全部自动恢复，state.sqlite 历史完整

---

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| watchdog 漏事件（NTFS 大目录、休眠期） | 启动期对账 + 30s 轮询双保险 |
| RAGFlow v0.25 API 路径不准 | 实施前 curl /openapi.json 校对 |
| 单进程内 watchdog/APScheduler/uvicorn 共存 | uvicorn 单 worker；watchdog 在 startup 事件起线程；NSSM 拉起 |
| 32GB 内存压力 | RAGFlow MEM_LIMIT=3GB；rag-core ≈200MB；可控 |
| Opus 成本失控 | 三级漏斗 + 硬上限 + 缓存三重防护 |
| 中文路径 mojibake | ASCII 路径；Python 文件 UTF-8 BOMless |
| 托盘需用户会话 | rag-tray 走 Task Scheduler 而非 NSSM |

---

## 时间估算

| Phase | 估时 | 依赖 |
|---|---|---|
| 0 | 1h | - |
| 1 | 8-12h | 0 |
| 2 | 4-6h | 1 |
| 3 | 1-2h | 1, 2 |
| 4 | 4-6h | 1 |
| 5 | 2-3h | 1, 4 |

执行顺序：0 → 1 → 2 → 3 → 4 → 5
