# 数据库配置

LambChat 使用 MongoDB 作为主数据库，Redis 用于缓存、SSE 事件、发布/订阅和可选任务队列。PostgreSQL 可选用于检查点存储。

## Redis

| 变量名 | 默认值 | 敏感 | 说明 |
|--------|--------|------|------|
| `REDIS_URL` | `redis://localhost:6379/0` | 是 | Redis 连接 URL。 |
| `REDIS_PASSWORD` | _(空)_ | 是 | Redis 认证密码。 |

## 任务执行

使用 `TASK_BACKEND=arq` 可将默认聊天任务交给基于 Redis 的 arq 队列执行，适合希望由 worker 处理任务而不是本地进程内任务执行器的部署方式。

| 变量名 | 默认值 | 敏感 | 说明 |
|--------|--------|------|------|
| `TASK_BACKEND` | `arq` | 否 | 任务执行后端：`local` 或 `arq`。 |
| `ARQ_EMBEDDED_WORKER` | `true` | 否 | 当 `TASK_BACKEND=arq` 时，在每个 FastAPI 进程内启动内嵌 arq worker。 |
| `ARQ_QUEUE_NAME` | `lambchat:arq` | 否 | arq 用于 LambChat 任务 job 的 Redis 队列名。 |
| `ARQ_WORKER_MAX_JOBS` | `128` | 否 | 每个 FastAPI 进程内 arq job 的最大并发数。 |
| `ARQ_JOB_TIMEOUT_SECONDS` | `86400` | 否 | 单个 arq job 的最长运行时间（秒）。 |

## MongoDB

| 变量名 | 默认值 | 敏感 | 说明 |
|--------|--------|------|------|
| `MONGODB_URL` | `mongodb://localhost:27017` | 是 | MongoDB 连接 URL。 |
| `MONGODB_DB` | `agent_state` | 否 | 数据库名称。 |
| `MONGODB_USERNAME` | _(空)_ | 否 | 认证用户名。 |
| `MONGODB_PASSWORD` | _(空)_ | 是 | 认证密码。 |
| `MONGODB_AUTH_SOURCE` | `admin` | 否 | 认证源数据库。 |
| `MONGODB_SESSIONS_COLLECTION` | `sessions` | 否 | 会话集合名称。 |
| `MONGODB_TRACES_COLLECTION` | `traces` | 否 | Trace 集合名称。 |

## PostgreSQL（可选）

用于 LangGraph 检查点存储，以避免 MongoDB 16MB BSON 文档限制。

| 变量名 | 默认值 | 敏感 | 说明 |
|--------|--------|------|------|
| `ENABLE_POSTGRES_STORAGE` | `false` | 否 | 启用 PostgreSQL 存储后端。 |
| `POSTGRES_HOST` | `localhost` | 否 | PostgreSQL 主机。 |
| `POSTGRES_PORT` | `5432` | 否 | PostgreSQL 端口。 |
| `POSTGRES_USER` | `postgres` | 否 | 用户名。 |
| `POSTGRES_PASSWORD` | `postgres` | 是 | 密码。 |
| `POSTGRES_DB` | `langgraph` | 否 | 数据库名称。 |
| `POSTGRES_POOL_MIN_SIZE` | `2` | 否 | 连接池最小大小。 |
| `POSTGRES_POOL_MAX_SIZE` | `10` | 否 | 连接池最大大小。 |

## 检查点后端

选择 Agent 检查点的存储位置。

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `CHECKPOINT_BACKEND` | `mongodb` | 检查点存储：`mongodb` 或 `postgres`。 |
| `CHECKPOINT_PG_HOST` | _(回退到 `POSTGRES_HOST`)_ | 检查点专用 PostgreSQL 主机。 |
| `CHECKPOINT_PG_PORT` | `5432` | 检查点专用 PostgreSQL 端口。 |
| `CHECKPOINT_PG_USER` | _(回退到 `POSTGRES_USER`)_ | 检查点专用 PostgreSQL 用户。 |
| `CHECKPOINT_PG_PASSWORD` | _(回退到 `POSTGRES_PASSWORD`)_ | 检查点专用 PostgreSQL 密码。**敏感。** |
| `CHECKPOINT_PG_DB` | _(回退到 `POSTGRES_DB`)_ | 检查点专用 PostgreSQL 数据库。 |
| `CHECKPOINT_PG_POOL_MIN_SIZE` | `2` | 检查点 PG 连接池最小大小。 |
| `CHECKPOINT_PG_POOL_MAX_SIZE` | `10` | 检查点 PG 连接池最大大小。 |

::: warning
MongoDB 有 16MB BSON 文档限制。对于长时间运行且状态较大的 Agent，建议使用 `CHECKPOINT_BACKEND=postgres` 来避免达到此限制。
:::

## 示例

```bash
# Redis
REDIS_URL=redis://localhost:6379/0
REDIS_PASSWORD=your_redis_password

# 任务执行
TASK_BACKEND=arq
ARQ_EMBEDDED_WORKER=true
ARQ_QUEUE_NAME=lambchat:arq
ARQ_WORKER_MAX_JOBS=128
ARQ_JOB_TIMEOUT_SECONDS=86400

# MongoDB
MONGODB_URL=mongodb://localhost:27017
MONGODB_DB=agent_state
MONGODB_USERNAME=admin
MONGODB_PASSWORD=your_mongo_password

# PostgreSQL（可选）
ENABLE_POSTGRES_STORAGE=true
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_pg_password
POSTGRES_DB=langgraph
CHECKPOINT_BACKEND=postgres
```
