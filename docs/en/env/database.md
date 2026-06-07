# Database Configuration

LambChat uses MongoDB as the primary database and Redis for caching, SSE events, pub/sub, and optional task queues. PostgreSQL is optional for checkpoint storage.

## Redis

| Variable | Default | Sensitive | Description |
|----------|---------|-----------|-------------|
| `REDIS_URL` | `redis://localhost:6379/0` | Yes | Redis connection URL. |
| `REDIS_PASSWORD` | _(empty)_ | Yes | Redis authentication password. |

## Task Execution

Use `TASK_BACKEND=arq` to run default chat tasks through a Redis-backed arq queue. This is useful when you want task execution to be handled by workers instead of the local in-process task runner.

| Variable | Default | Sensitive | Description |
|----------|---------|-----------|-------------|
| `TASK_BACKEND` | `arq` | No | Task execution backend: `local` or `arq`. |
| `ARQ_EMBEDDED_WORKER` | `true` | No | Start an embedded arq worker inside each FastAPI process when `TASK_BACKEND=arq`. |
| `ARQ_QUEUE_NAME` | `lambchat:arq` | No | Redis queue name used by arq for LambChat task jobs. |
| `ARQ_WORKER_MAX_JOBS` | `128` | No | Maximum concurrent arq jobs per FastAPI process. |
| `ARQ_JOB_TIMEOUT_SECONDS` | `86400` | No | Maximum runtime for one arq job in seconds. |

## MongoDB

| Variable | Default | Sensitive | Description |
|----------|---------|-----------|-------------|
| `MONGODB_URL` | `mongodb://localhost:27017` | Yes | MongoDB connection URL. |
| `MONGODB_DB` | `agent_state` | No | Database name. |
| `MONGODB_USERNAME` | _(empty)_ | No | Authentication username. |
| `MONGODB_PASSWORD` | _(empty)_ | Yes | Authentication password. |
| `MONGODB_AUTH_SOURCE` | `admin` | No | Authentication source database. |
| `MONGODB_SESSIONS_COLLECTION` | `sessions` | No | Collection name for sessions. |
| `MONGODB_TRACES_COLLECTION` | `traces` | No | Collection name for traces. |

## PostgreSQL (Optional)

Used for LangGraph checkpoint store to avoid MongoDB's 16MB BSON limit.

| Variable | Default | Sensitive | Description |
|----------|---------|-----------|-------------|
| `ENABLE_POSTGRES_STORAGE` | `false` | No | Enable PostgreSQL storage backend. |
| `POSTGRES_HOST` | `localhost` | No | PostgreSQL host. |
| `POSTGRES_PORT` | `5432` | No | PostgreSQL port. |
| `POSTGRES_USER` | `postgres` | No | Username. |
| `POSTGRES_PASSWORD` | `postgres` | Yes | Password. |
| `POSTGRES_DB` | `langgraph` | No | Database name. |
| `POSTGRES_POOL_MIN_SIZE` | `2` | No | Connection pool minimum size. |
| `POSTGRES_POOL_MAX_SIZE` | `10` | No | Connection pool maximum size. |

## Checkpoint Backend

Choose where agent checkpoints are stored.

| Variable | Default | Description |
|----------|---------|-------------|
| `CHECKPOINT_BACKEND` | `mongodb` | Checkpoint storage: `mongodb` or `postgres`. |
| `CHECKPOINT_PG_HOST` | _(falls back to `POSTGRES_HOST`)_ | Checkpoint-specific PostgreSQL host. |
| `CHECKPOINT_PG_PORT` | `5432` | Checkpoint-specific PostgreSQL port. |
| `CHECKPOINT_PG_USER` | _(falls back to `POSTGRES_USER`)_ | Checkpoint-specific PostgreSQL user. |
| `CHECKPOINT_PG_PASSWORD` | _(falls back to `POSTGRES_PASSWORD`)_ | Checkpoint-specific PostgreSQL password. **Sensitive.** |
| `CHECKPOINT_PG_DB` | _(falls back to `POSTGRES_DB`)_ | Checkpoint-specific PostgreSQL database. |
| `CHECKPOINT_PG_POOL_MIN_SIZE` | `2` | Checkpoint PG pool minimum size. |
| `CHECKPOINT_PG_POOL_MAX_SIZE` | `10` | Checkpoint PG pool maximum size. |

::: warning
MongoDB has a 16MB BSON document limit. For long-running agents with large state, use `CHECKPOINT_BACKEND=postgres` to avoid hitting this limit.
:::

## Example

```bash
# Redis
REDIS_URL=redis://localhost:6379/0
REDIS_PASSWORD=your_redis_password

# Task execution
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

# PostgreSQL (optional)
ENABLE_POSTGRES_STORAGE=true
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_pg_password
POSTGRES_DB=langgraph
CHECKPOINT_BACKEND=postgres
```
