# 内网运维管理平台（ops-platform）

自研内网 NMS，对标北塔（Broadview）轻量版。监控网络/安全设备、Windows/Linux 服务器、数据库、应用与中间件，拓扑自动发现 + 手工连线、链路流量监控、阈值/基线告警、配置备份、Syslog/Trap 日志告警、IPAM、报表与大屏。总体方案见 `docs/总体方案.md`。

## 功能总览（第 1~8 期已完成）

- **设备管理（CMDB）**：设备 CRUD（唯一性 ip+type，支持一机多对象）、网络设备细分类型（交换机/路由器/防火墙，拓扑图标区分）、凭据管理（SNMP v2c/v3、SSH、数据库账号，Fernet 加密存储）、IP 段自动发现入库
- **监控采集**（全部无 Agent）：Ping 在线/时延（60s）、SNMP 接口流量/状态 + 设备 CPU/内存（60s，Cisco/华为/H3C 指纹）、Windows SNMP（60s，HOST-RESOURCES-MIB：CPU/内存/磁盘/进程）、Linux SSH（60s，/proc + df）、数据库探针（300s：MySQL 连接数/QPS/TPS/慢查询/主从延迟；Oracle 会话/表空间；SQLServer 连接数/缓存命中率/批请求速率）、应用拨测（60s：HTTP 状态码+关键字 / DNS 解析 / TCP 连通+banner，Nginx stub_status 与 Redis INFO 专项采集）
- **时序存储**：PostgreSQL + TimescaleDB（hypertable 压缩 7 天/保留 30 天，5 分钟连续聚合保留 180 天，实时聚合已启用）
- **拓扑**：LLDP/CDP 自动发现 + 手工连线，G6 深色画布（自由缩放/平移、编辑模式拖拽摆位/拉线/删链、链路流量标签、链路历史流量曲线、子网/机房分组子视图与独立布局）
- **告警**：阈值 + 动态基线（同比 7 天同时段 Nσ 偏离）规则引擎（去抖/升级/静默窗口），告警时刻指标快照，规则模板批量套用（28 条内置模板），接口 down 只告曾 up 过的端口，28 条内置规则按设备类型覆盖；通知渠道 SMTP/钉钉/企业微信；操作审计日志
- **总线视图**：宿主机-数据库/应用承载关系一屏总览（状态环 + 告警角标，/bus）
- **配置备份**：网络设备 SSH 定时拉取配置（6h，Cisco/华为/H3C 命令指纹），版本管理 + 两版 diff，配置变更自动产生事件
- **日志告警**：Syslog（UDP 1514）/ SNMP Trap（UDP 1162）接收，关键字/级别/源规则匹配后进告警引擎
- **IPAM**：IP-终端-接入端口台账（SNMP ARP/MAC 表 + 扫描回写），子网 /24 网格视图（在线/7 天活跃/未见），新终端接入检测 + 白名单
- **报表**：可用率/接口流量日月报，导出 Excel（openpyxl）
- **大屏**：科技风实时监控屏（/screen，指标大数字/在线率仪表盘/流量趋势/TopN/告警滚动墙）
- **实时推送**：WebSocket（新告警 + 设备状态变化，进程内广播）
- **性能**：压测验证 2000 台设备 60s 采集周期仅需 5.8s（余量 10 倍，报告 `docs/压测报告.md`）
- **权限**：RBAC 三角色 + 用户禁用/改角色（即时生效）

## 快速开始（开发）

```bash
# 0. 数据库（开发也可直接用 SQLite 跳过本步；联调/生产用 PostgreSQL）
cd deploy && cp .env.example .env   # 改 OPS_DB_PASSWORD
docker compose up -d                # TimescaleDB，127.0.0.1:5432

# 后端（Python 3.11+，默认 SQLite 开箱即用）
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# 连 PG 时设：export OPS_DATABASE_URL=postgresql+psycopg://ops:密码@127.0.0.1:5432/ops
.venv/bin/python -m uvicorn app.main:app --port 8100 --reload

# 前端（Node 18+，另开终端）
cd frontend
npm install
npm run dev        # http://localhost:5173，代理 /api → 127.0.0.1:8100
```

浏览器打开 http://localhost:5173 ，默认账号 `admin / admin123`（首次登录后请改密）。

> 注意：本机 8000 端口常被其他服务占用，后端开发统一用 **8100**。
> WSL + /mnt/d（DrvFs）开发时 vite 已配置 `watch.usePolling`，无需额外处理。

## 测试

```bash
# 必须 python -m pytest（pytest 二进制 sys.path 不含 backend）
cd backend && .venv/bin/python -m pytest -q
# 对 PostgreSQL 做方言回归（需先起库，测试库自动建/重建，勿指生产库）：
OPS_TEST_DATABASE_URL=postgresql+psycopg://ops:密码@127.0.0.1:5432/ops_test .venv/bin/python -m pytest -q
```

当前 225 个用例双跑全过。压测：`python scripts/bench.py --devices 2000 --cycles 3`（独立 bench 库，不碰业务数据）。

## 部署（内网无外网）

```bash
# 有网机器上打离线包（产出 deploy/offline/ops-platform-offline-<日期>.tar.gz）
bash deploy/offline-pack.sh
# 内网机器：解包 → docker load → 配 .env → 起栈
docker compose -f docker-compose.full.yml up -d    # db + api + web，默认 web 18080 端口
```

详见 `docs/部署手册.md`（含备份/升级/常见坑）。生产必须显式配置 `OPS_FERNET_KEY` / `OPS_SECRET_KEY` / `OPS_DB_PASSWORD`。

## 环境变量（前缀 OPS_）

| 变量 | 默认 | 说明 |
|---|---|---|
| OPS_DATABASE_URL | sqlite:///backend/data/ops_platform.db | 生产：`postgresql+psycopg://user:pass@host/ops` |
| OPS_SECRET_KEY | 开发默认值 | JWT 签名密钥，生产必须改 |
| OPS_FERNET_KEY | 自动生成存 data/fernet.key | 凭据加密密钥，**更换=旧凭据全部失效** |
| OPS_MONITOR_INTERVAL | 60 | 在线状态轮询周期（秒） |
| OPS_DB_POOL_SIZE / OPS_DB_MAX_OVERFLOW | 20 / 40 | PG 连接池（非 SQLite 生效） |
| OPS_ADMIN_USERNAME / OPS_ADMIN_PASSWORD | admin / admin123 | 首次启动种子管理员 |

## 目录结构

```
backend/app/
├── api/         # 路由：auth/users/credentials/devices/discovery/monitor/metrics/alerts/audits/reports/topology/ws
├── core/        # 配置、数据库、安全、Fernet、jobrunner（后台任务常驻事件循环）、broadcast、audit、timescale
├── models/      # User/Credential/Device/Metric/Alert/Topology/Audit/DiscoveryJob
├── collectors/  # scanner/snmp（pysnmp 7.1 封装）/snmp_metrics/windows/linux_ssh/db_probe/rate
├── scheduler/   # scheduler.py（任务注册表+协程池）、monitor_loop.py（ping 监控循环）
├── alerting/    # engine.py（规则引擎）、escalation.py（升级）、notify.py（SMTP/钉钉/企微）
└── topology/    # discovery.py（LLDP/CDP 邻居解析）
frontend/src/    # Vue 3 + Element Plus + G6 v5 + ECharts（懒加载）
deploy/          # docker-compose（dev 库）/ docker-compose.full.yml（三服务全栈）/ offline-pack.sh / 部署相关
scripts/         # bench.py（压测）、resolve_dead_ifdown.py（一次性治理脚本）
docs/            # 总体方案、各期计划、压测报告、部署手册
```

## 关键设计决策

- 完全自研，不基于 Zabbix/LibreNMS；Python 全栈（FastAPI + asyncio / Vue 3）
- 全部无 Agent：SNMP（网络/安全/Windows）+ SSH（Linux）+ 只读账号直连（数据库）
- **Redis 不引入**（压测评估结论，见 `docs/压测报告.md`）：单进程 asyncio 足够覆盖 2000 台
- pysnmp 锁 7.1.27（4.4 不支持 Python 3.12+，且 pyasn1≥0.5 不兼容）
- 网络拓扑只放 network/security 设备；服务器/数据库在设备管理页维护
