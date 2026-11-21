# 松门纪念园·入园凭条分区

真实 PostgreSQL 范围分区分流、迟到凭条回补并核验保留期边界。面向演示数据和本地复现，场景、操作剧本和业务边界见 [BUSINESS.md](BUSINESS.md)。

## 业务模块

- 凭条账簿
- 分区边界
- 生命周期执行
- 回收核验
- 在册账簿

## 在册账簿

`slip_ledger` 是持久的 PostgreSQL 范围分区账簿（`DEFAULT` 分区即待核池），两处闸机的补传都记入同一本账：

- `POST /ledger/ingest` 闸机分批补传。凭条编号全账唯一，重复补传原样忽略，日期不一致的编号标记冲突待更正，已月结月份的凭条逐张拒收。
- `POST /ledger/corrections` 按凭条编号和更正单改入园日。跨月迁移在同一事务内删除并插入，查账只能看到迁移前或迁移后的那一张；未建分区的月份仍进待核池。更正单号是幂等键：回执丢失后重试同一笔不覆盖后来的更正，原样返回当时的回执（HTTP 200）。
- `POST /ledger/partitions` 挂载月分区，并把待核池中属于该月的凭条同事务回补。
- `POST /ledger/close` 月结。按月加咨询锁，月结照常办理，无关月份的更正并发继续；月结完成后该月凭条不得迁入或迁出。
- `GET /ledger/handover` 交班核对：从账簿物理分区读取每张凭条的当前入园日与去向。

补传、更正、挂载与月结需经办员角色，交班核对与更正单历史所有角色可读。业务说明见 [BUSINESS.md](BUSINESS.md)。

## 测试

```sh
docker compose exec backend python -m pytest tests -q
```

测试覆盖两闸机去重、跨月迁移原子可见、待核池回补、月结与更正并发、月结封存及更正幂等重试。

## 本地运行

```sh
cp -n .env.example .env
docker compose up -d --build --wait
```

- 工作台 `http://127.0.0.1:18304`
- FastAPI 文档 `http://127.0.0.1:19304/docs`
- PostgreSQL 16 端口 `127.0.0.1:17804`，账号 `app`，演示库 `workflow`
- `docker compose exec -T backend python -m scripts.seed_demo` 手动重跑演示数据
- `docker compose logs -f backend worker` 查看运行情况
- `docker compose down` 停容器并保留演示卷

### 演示账号

统一密码 `Demo-Walkthrough-2026`。

| 账号 | 角色 | 能执行的操作 |
|---|---|---|
| `operator` | 经办员 | 读取档案、创建实验输入、提交执行、导出结果 |
| `reviewer` | 复核员 | 读取、导出和签署结果 |
| `viewer` | 观察员 | 读取、导出已有结果 |

`SEED_DEMO=true` 启动时生成账号与一个业务样例。工作台新建档案会预填样例。可编辑表单中的数值、文本和结构化 JSON 后提交。业务说明中的原始文件是上游示例，演示数据通过表单录入。角色枚举位于后端 `src/constants/workflow.py` 与前端 `src/constants/workflow.js`。执行状态枚举也位于这两个文件，使用于队列、列表和执行详情。证据校验码是报告 JSON 按 UTF-8、键排序且省略多余空格序列化后的 SHA-256。

## 工程结构

| 服务 | 实现 |
|---|---|
| frontend | Nginx 与原生 HTML、CSS、JavaScript |
| backend | Python 3.11、FastAPI、SQLAlchemy |
| worker | Python 持久队列执行与 Redis 唤醒信号 |
| db | PostgreSQL 16，持久卷保存档案、执行、证据及审计 |
| redis | Redis 7，追加日志卷保存队列唤醒信号 |

编辑 `backend/src/services/domain.py` 可修改试算方法与业务表单说明。API 路由、数据定义和页面位于 `backend/src/routes/`、`backend/src/models/` 与 `frontend/src/pages/`。开发时更新源文件后重新构建对应服务。

`.env.example` 提供 `FRONTEND_PORT`、`BACKEND_PORT`、`DB_PORT`、`DB_PASSWORD`、`AUTH_KEY` 、`SEED_DEMO` 与 `NETWORK_SUBNET`。修改 `NETWORK_SUBNET` 可避开本机现有容器网段。所有发布的端口绑定本机回环地址。接入独立部署前需配置自己的凭据并按业务范围做独立验收。

[MIT License](LICENSE)
