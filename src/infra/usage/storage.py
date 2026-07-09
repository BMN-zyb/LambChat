"""
Usage storage layer.

独立的 usage_logs 集合，在 trace 完成时写入扁平化的 token 消耗记录。
查询时直接从该集合读取，避免对 traces 集合做复杂聚合。
"""

# ---------------------------------------------------------------------------
# 模块说明：用量统计存储（独立 usage_logs 集合 + 扁平化写入 + 仪表盘聚合）
#
# 设计动机：原始 trace 文档结构复杂（含 events 数组、嵌套 metadata），直接在其上
# 做统计聚合既慢又难写。因此本模块在「trace 完成时」把本次 run 的最终用量抽取出来，
# 拍平成一条维度齐全的记录（用户/团队/模型/来源/定时任务 + input/output/cache token、
# 时长、状态等）写入独立的 usage_logs 集合——这就是「扁平化」，查询侧只需对这张扁平表
# 聚合即可。要点：
#   - upsert_usage_log 从 events 里倒序取最后一条 token:usage 作为最终累计用量；
#   - 以 trace_id 唯一索引 + upsert 保证幂等（重复完成/补写不产生重复记录）；
#   - 字段来源按「trace metadata 优先、session metadata 兜底」合并，team_name 缺失时
#     再按 team_id 反查；_as_int/_as_float/_as_datetime 对历史脏数据做防御式转换；
#   - get_usage_dashboard 用单个 $facet 在一次匹配上并行算出 summary/daily 及多个
#     Top N 排行榜，避免多次查库；所有派生比率都对分母为 0 做了保护。
# ---------------------------------------------------------------------------

from datetime import datetime
from typing import Any, Dict, List, Optional

from src.infra.logging import get_logger
from src.infra.storage.mongodb import get_mongo_client
from src.infra.utils.datetime import parse_iso
from src.kernel.config import settings

logger = get_logger(__name__)

# 列表查询单页返回上限(防止一次拉取过多)。
USAGE_LOG_LIMIT_MAX = 200
# 各类排行榜(agents/teams/models 等)取前 N 名。
USAGE_RANKING_LIMIT = 8
# 判定「一条用量记录是否属于定时任务」的聚合表达式:source 为 scheduled_task,
# 或 scheduled_task_id 非空。用于仪表盘里统计定时任务占比。
SCHEDULED_USAGE_CONDITION = {
    "$or": [
        {"$eq": ["$source", "scheduled_task"]},
        {
            "$and": [
                {"$ne": ["$scheduled_task_id", None]},
                {"$ne": ["$scheduled_task_id", ""]},
            ]
        },
    ]
}


def _as_int(value: Any) -> int:
    # 防御式转 int(用于外部/历史脏数据):bool 转 0/1,数值取非负,字符串尽力解析,失败则 0。
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        return max(int(value), 0)
    if isinstance(value, str):
        try:
            return max(int(float(value)), 0)
        except ValueError:
            return 0
    return 0


def _as_float(value: Any) -> float:
    # 防御式转 float,取非负;无法解析则 0.0。
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return max(float(value), 0.0)
    if isinstance(value, str):
        try:
            return max(float(value), 0.0)
        except ValueError:
            return 0.0
    return 0.0


def _as_datetime(value: Any) -> datetime | None:
    # 尽力把值转成 datetime:已是 datetime 直接返回,ISO 字符串解析,其他/失败返回 None。
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return parse_iso(value)
        except ValueError:
            return None
    return None


def _as_str(value: Any) -> str:
    # 仅当是字符串才返回,否则返回空串(统一 None/非字符串为 "")。
    return value if isinstance(value, str) else ""


def _merge_metadata_value(
    metadata: Dict[str, Any], session_metadata: Dict[str, Any], key: str
) -> str:
    # 取某字段:优先 trace 自身 metadata,缺失则回退到 session 级 metadata。
    return _as_str(metadata.get(key)) or _as_str(session_metadata.get(key))


# 用量存储：延迟持有 usage_logs 集合；写侧提供 upsert（扁平化落库），
# 读侧提供列表查询、运营仪表盘聚合与单用户汇总
class UsageStorage:
    """使用日志存储 — 独立的 usage_logs 集合"""

    def __init__(self):
        # usage_logs 集合(惰性加载)。
        self._collection = None

    @property
    def collection(self):
        """延迟加载 usage_logs 集合"""
        # 首次访问才连库取集合,避免导入期建立连接。
        if self._collection is None:
            client = get_mongo_client()
            db = client[settings.MONGODB_DB]
            self._collection = db[settings.MONGODB_USAGE_LOGS_COLLECTION]
        return self._collection

    async def ensure_indexes(self) -> None:
        """创建索引"""
        # trace_id 唯一索引:配合 upsert 保证每个 trace 只有一条用量记录(幂等写入)。
        # 其余为「过滤字段 + started_at 倒序」的复合索引,支撑按用户/模型/团队/来源等维度的时间序查询。
        try:
            await self.collection.create_index(
                "trace_id",
                unique=True,
                name="trace_id_unique_idx",
            )
            await self.collection.create_index([("user_id", 1), ("started_at", -1)])
            await self.collection.create_index([("started_at", -1)])
            await self.collection.create_index([("model", 1), ("started_at", -1)])
            await self.collection.create_index([("team_id", 1), ("started_at", -1)])
            await self.collection.create_index([("persona_preset_id", 1), ("started_at", -1)])
            await self.collection.create_index([("source", 1), ("started_at", -1)])
            logger.info("Usage logs indexes ensured")
        except Exception as e:
            logger.error(f"Failed to create usage logs indexes: {e}")

    async def _get_session_metadata(self, session_id: str) -> Dict[str, Any]:
        # 取会话级 metadata,作为 trace metadata 缺失字段的回退来源;任何异常都降级为空 dict。
        if not session_id:
            return {}
        try:
            from src.infra.session.storage import SessionStorage

            doc = await SessionStorage().collection.find_one(
                {"session_id": session_id},
                {"_id": 0, "metadata": 1},
            )
            metadata = (doc or {}).get("metadata") or {}
            return metadata if isinstance(metadata, dict) else {}
        except Exception as e:
            logger.debug("Failed to load session metadata for usage log %s: %s", session_id, e)
            return {}

    async def _resolve_team_name(self, team_id: str) -> str:
        # 由 team_id 反查团队名(当 metadata 里没有 team_name 时的兜底)。
        if not team_id:
            return ""
        try:
            from bson import ObjectId

            from src.infra.team.storage import TeamStorage

            # team_id 可能是 ObjectId 的字符串形式,先尝试转 ObjectId,失败则按原字符串查询。
            query_id: ObjectId | str
            try:
                query_id = ObjectId(team_id)
            except Exception:
                query_id = team_id
            doc = await TeamStorage().collection.find_one({"_id": query_id}, {"_id": 0, "name": 1})
            return _as_str((doc or {}).get("name"))
        except Exception as e:
            logger.debug("Failed to resolve team name for usage log %s: %s", team_id, e)
            return ""

    async def upsert_usage_log(self, trace_doc: Dict[str, Any]) -> bool:
        """
        从 trace 文档提取 token:usage 数据，写入 usage_logs 集合。

        在 trace 完成时调用，将扁平化的使用记录存入独立集合。

        Args:
            trace_doc: trace 完整文档（包含 events 数组和 metadata）

        Returns:
            是否写入成功
        """
        trace_id = trace_doc.get("trace_id")
        if not trace_id:
            return False

        # 从 events 中找到最后一个 token:usage 事件
        # 倒序遍历取「最后一条」token:usage(通常代表本次 run 的最终累计用量)。
        usage_event = None
        for event in reversed(trace_doc.get("events", [])):
            if event.get("event_type") == "token:usage":
                usage_event = event.get("data", {})
                break

        return await self.upsert_usage_log_from_trace_metadata(trace_doc, usage_event)

    async def upsert_usage_log_from_trace_metadata(
        self,
        trace_doc: Dict[str, Any],
        usage_data: Optional[Dict[str, Any]],
    ) -> bool:
        """
        使用 trace 元数据和已解析的 token:usage 数据写入 usage_logs。

        Args:
            trace_doc: trace 元数据（不需要包含完整 events）
            usage_data: 最后一条 token:usage 事件的 data；缺失时按 0 处理

        Returns:
            是否写入成功
        """
        trace_id = trace_doc.get("trace_id")
        if not trace_id:
            return False

        # 合并 trace 与 session 两级 metadata 作为字段来源。
        metadata = trace_doc.get("metadata", {}) or {}
        session_metadata = await self._get_session_metadata(str(trace_doc.get("session_id") or ""))
        usage_data = usage_data or {}
        input_tokens = _as_int(usage_data.get("input_tokens", 0))
        output_tokens = _as_int(usage_data.get("output_tokens", 0))
        total_tokens = _as_int(usage_data.get("total_tokens", 0))
        # 若上游未给 total,则用 输入+输出 兜底计算。
        if total_tokens <= 0:
            total_tokens = input_tokens + output_tokens

        # team_name 优先取 metadata,缺失时按 team_id 反查团队集合。
        team_id = _merge_metadata_value(metadata, session_metadata, "team_id")
        team_name = _merge_metadata_value(
            metadata, session_metadata, "team_name"
        ) or await self._resolve_team_name(team_id)

        # 组装一条「扁平化」的用量记录:把维度字段与 token/时长/状态等指标拍平,便于后续直接聚合查询。
        doc = {
            "trace_id": trace_id,
            "session_id": trace_doc.get("session_id", ""),
            "run_id": trace_doc.get("run_id", ""),
            "user_id": trace_doc.get("user_id", ""),
            "username": metadata.get("username", ""),
            "agent_id": trace_doc.get("agent_id", ""),
            "agent_name": metadata.get("agent_name", ""),
            "team_id": team_id,
            "team_name": team_name,
            "persona_preset_id": _merge_metadata_value(
                metadata, session_metadata, "persona_preset_id"
            ),
            "persona_preset_name": _merge_metadata_value(
                metadata, session_metadata, "persona_preset_name"
            ),
            "source": _merge_metadata_value(metadata, session_metadata, "source") or "chat",
            "scheduled_task_id": _merge_metadata_value(
                metadata, session_metadata, "scheduled_task_id"
            ),
            "scheduled_task_run_id": _merge_metadata_value(
                metadata, session_metadata, "scheduled_task_run_id"
            ),
            "scheduled_task_trigger_type": _merge_metadata_value(
                metadata, session_metadata, "scheduled_task_trigger_type"
            )
            or _merge_metadata_value(metadata, session_metadata, "trigger_type"),
            "model": usage_data.get("model", ""),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cache_creation_tokens": _as_int(usage_data.get("cache_creation_tokens", 0)),
            "cache_read_tokens": _as_int(usage_data.get("cache_read_tokens", 0)),
            "duration": _as_float(usage_data.get("duration", 0.0)),
            "started_at": _as_datetime(trace_doc.get("started_at")),
            "completed_at": _as_datetime(trace_doc.get("completed_at")),
            "status": trace_doc.get("status", "unknown"),
            "step_count": _as_int(metadata.get("step_count", 0)),
            "tool_calls": _as_int(metadata.get("tool_calls", 0)),
        }

        try:
            # 按 trace_id upsert:存在则覆盖(重复完成/补写不会产生重复记录),不存在则插入。
            await self.collection.update_one(
                {"trace_id": trace_id},
                {"$set": doc},
                upsert=True,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to upsert usage log for trace {trace_id}: {e}")
            return False

    async def list_usage_logs(
        self,
        *,
        user_id: Optional[str] = None,
        model: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        search: Optional[str] = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[List[Dict[str, Any]], int, Dict[str, Any]]:
        """
        查询使用日志列表。

        Args:
            user_id: 按用户过滤
            model: 按模型过滤
            start_date: 开始日期 (ISO string)
            end_date: 结束日期 (ISO string)
            search: 搜索用户名
            skip: 跳过数量
            limit: 返回数量

        Returns:
            (items, total, stats_dict)
        """
        limit = max(1, min(limit, USAGE_LOG_LIMIT_MAX))
        skip = max(0, skip)

        query = self._build_query(
            user_id=user_id,
            model=model,
            start_date=start_date,
            end_date=end_date,
            search=search,
        )

        try:
            # 并行执行 count + stats + items
            # 计数/聚合统计 与 分页取数 相互独立,用两个 task 并发执行以降低总延迟。
            import asyncio

            count_task = asyncio.create_task(self._count_and_stats(query))
            items_task = asyncio.create_task(self._fetch_items(query, skip, limit))

            total, stats = await count_task
            items = await items_task

            return items, total, stats
        except Exception as e:
            logger.error(f"Failed to list usage logs: {e}")
            return [], 0, _empty_stats()

    def _build_query(
        self,
        *,
        user_id: Optional[str] = None,
        model: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        # 根据可选过滤条件拼装 Mongo 查询;时间范围用 started_at 的 $gte/$lt,搜索按用户名做不区分大小写正则。
        query: Dict[str, Any] = {}

        if user_id:
            query["user_id"] = user_id
        if model:
            query["model"] = model
        if start_date or end_date:
            date_filter: Dict[str, Any] = {}
            if start_date:
                date_filter["$gte"] = parse_iso(start_date)
            if end_date:
                date_filter["$lt"] = parse_iso(end_date)
            query["started_at"] = date_filter
        if search:
            query["username"] = {"$regex": search, "$options": "i"}
        return query

    async def get_usage_dashboard(
        self,
        *,
        user_id: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        model: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Aggregate usage logs into an operations dashboard."""
        query = self._build_query(
            user_id=user_id,
            model=model,
            start_date=start_date,
            end_date=end_date,
            search=search,
        )
        # 用一个 $facet 在「同一次匹配」上并行算出多个视图:总览 summary、按天 daily、
        # 以及 agents/teams/personas/models/users/sources/triggers 等多个排行榜,避免多次查库。
        pipeline = [
            {"$match": query},
            {
                "$facet": {
                    "summary": [
                        {
                            "$group": {
                                "_id": None,
                                "total_requests": {"$sum": 1},
                                "total_tokens": {"$sum": "$total_tokens"},
                                "total_input_tokens": {"$sum": "$input_tokens"},
                                "total_output_tokens": {"$sum": "$output_tokens"},
                                "total_cache_read_tokens": {"$sum": "$cache_read_tokens"},
                                "total_duration": {"$sum": "$duration"},
                                "total_tool_calls": {"$sum": "$tool_calls"},
                                "max_duration": {"$max": "$duration"},
                                "scheduled_runs": {
                                    "$sum": {"$cond": [SCHEDULED_USAGE_CONDITION, 1, 0]}
                                },
                                "successful_requests": {
                                    "$sum": {"$cond": [{"$eq": ["$status", "completed"]}, 1, 0]}
                                },
                                "failed_requests": {
                                    "$sum": {"$cond": [{"$ne": ["$status", "completed"]}, 1, 0]}
                                },
                            }
                        }
                    ],
                    "daily": [
                        {
                            "$group": {
                                "_id": {
                                    "$dateToString": {
                                        "format": "%Y-%m-%d",
                                        "date": "$started_at",
                                    }
                                },
                                "requests": {"$sum": 1},
                                "tokens": {"$sum": "$total_tokens"},
                                "duration": {"$sum": "$duration"},
                                "scheduled_runs": {
                                    "$sum": {"$cond": [SCHEDULED_USAGE_CONDITION, 1, 0]}
                                },
                                "failed_requests": {
                                    "$sum": {"$cond": [{"$ne": ["$status", "completed"]}, 1, 0]}
                                },
                                "tool_calls": {"$sum": "$tool_calls"},
                            }
                        },
                        {"$sort": {"_id": 1}},
                    ],
                    "agents": self._ranking_pipeline("agent_name"),
                    "teams": self._ranking_pipeline("team_id", name_field="team_name"),
                    "personas": self._ranking_pipeline(
                        "persona_preset_id", name_field="persona_preset_name"
                    ),
                    "models": self._ranking_pipeline("model"),
                    "users": self._ranking_pipeline("user_id", name_field="username"),
                    "sources": self._ranking_pipeline(
                        "source",
                        fallback_id="chat",
                        include_empty=True,
                    ),
                    "triggers": self._ranking_pipeline("scheduled_task_trigger_type"),
                }
            },
        ]

        try:
            async for doc in self.collection.aggregate(pipeline):
                return _format_dashboard(doc)
        except Exception as e:
            logger.error(f"Failed to aggregate usage dashboard: {e}")
        return _empty_dashboard()

    def _ranking_pipeline(
        self,
        field: str,
        *,
        name_field: str | None = None,
        limit: int = USAGE_RANKING_LIMIT,
        fallback_id: str | None = None,
        include_empty: bool = False,
    ) -> list[Dict[str, Any]]:
        # 生成一个「按某维度分组取 Top N」的子管道,供 $facet 复用。
        # field: 分组字段; name_field: 附带展示名; fallback_id: 空值时的兜底分组键;
        # include_empty: 是否保留空值分组(默认过滤掉 None/"")。
        group_id: Any = f"${field}"
        if fallback_id:
            group_id = {
                "$cond": [
                    {"$in": [f"${field}", [None, ""]]},
                    fallback_id,
                    f"${field}",
                ]
            }
        group: Dict[str, Any] = {
            "_id": group_id,
            "requests": {"$sum": 1},
            "tokens": {"$sum": "$total_tokens"},
            "duration": {"$sum": "$duration"},
        }
        if name_field:
            # $first 取分组内首条记录的展示名。
            group["name"] = {"$first": f"${name_field}"}
        pipeline: list[Dict[str, Any]] = []
        if not include_empty:
            pipeline.append({"$match": {field: {"$nin": [None, ""]}}})
        # 分组 -> 按 tokens、requests 降序 -> 取前 limit 名。
        pipeline.extend(
            [
                {"$group": group},
                {"$sort": {"tokens": -1, "requests": -1}},
                {"$limit": limit},
            ]
        )
        return pipeline

    async def _count_and_stats(self, query: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
        """计算总数和聚合统计"""
        # 先取总条数,再用聚合求各类 token 与时长的合计;聚合只有一个 _id=None 分组,取到即 break。
        total = await self.collection.count_documents(query)

        pipeline = [
            {"$match": query},
            {
                "$group": {
                    "_id": None,
                    "total_input_tokens": {"$sum": "$input_tokens"},
                    "total_output_tokens": {"$sum": "$output_tokens"},
                    "total_tokens": {"$sum": "$total_tokens"},
                    "total_cache_creation_tokens": {"$sum": "$cache_creation_tokens"},
                    "total_cache_read_tokens": {"$sum": "$cache_read_tokens"},
                    "total_duration": {"$sum": "$duration"},
                }
            },
        ]

        stats = _empty_stats()
        stats["total_requests"] = total

        try:
            async for doc in self.collection.aggregate(pipeline):
                stats.update(
                    {
                        "total_input_tokens": doc.get("total_input_tokens", 0),
                        "total_output_tokens": doc.get("total_output_tokens", 0),
                        "total_tokens": doc.get("total_tokens", 0),
                        "total_cache_creation_tokens": doc.get("total_cache_creation_tokens", 0),
                        "total_cache_read_tokens": doc.get("total_cache_read_tokens", 0),
                        "total_duration": doc.get("total_duration", 0.0),
                    }
                )
                break  # only one group result
        except Exception as e:
            logger.error(f"Failed to aggregate usage stats: {e}")

        return total, stats

    async def _fetch_items(
        self, query: Dict[str, Any], skip: int, limit: int
    ) -> List[Dict[str, Any]]:
        """获取分页数据"""
        # 按 started_at 倒序分页;投影排除 _id;失败返回空列表。
        try:
            cursor = (
                self.collection.find(query, {"_id": 0})
                .sort("started_at", -1)
                .skip(skip)
                .limit(limit)
            )
            return await cursor.to_list(length=limit)
        except Exception as e:
            logger.error(f"Failed to fetch usage items: {e}")
            return []

    async def get_user_usage_summary(self, user_id: str) -> Dict[str, Any]:
        """获取单个用户的用量汇总"""
        # 单用户维度的合计(请求数 + 各类 token + 时长),用于用户个人的用量概览。
        query = {"user_id": user_id}
        total = await self.collection.count_documents(query)

        pipeline = [
            {"$match": query},
            {
                "$group": {
                    "_id": None,
                    "total_input_tokens": {"$sum": "$input_tokens"},
                    "total_output_tokens": {"$sum": "$output_tokens"},
                    "total_tokens": {"$sum": "$total_tokens"},
                    "total_duration": {"$sum": "$duration"},
                }
            },
        ]

        summary: Dict[str, Any] = {"total_requests": total}
        try:
            async for doc in self.collection.aggregate(pipeline):
                summary.update(
                    {
                        "total_input_tokens": doc.get("total_input_tokens", 0),
                        "total_output_tokens": doc.get("total_output_tokens", 0),
                        "total_tokens": doc.get("total_tokens", 0),
                        "total_duration": doc.get("total_duration", 0.0),
                    }
                )
                break
        except Exception as e:
            logger.error(f"Failed to get user usage summary: {e}")

        return summary


def _empty_stats() -> Dict[str, Any]:
    # 统计的空结构(查询失败或无数据时返回),字段与 _count_and_stats 输出保持一致。
    return {
        "total_requests": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_tokens": 0,
        "total_cache_creation_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_duration": 0.0,
    }


def _format_ranking_item(doc: Dict[str, Any]) -> Dict[str, Any]:
    # 把聚合出的排行分组文档格式化为对外条目;无 name 时回退到 id,再回退到 "Unknown"。
    item_id = str(doc.get("_id") or "")
    return {
        "id": item_id,
        "name": str(doc.get("name") or item_id or "Unknown"),
        "requests": _as_int(doc.get("requests")),
        "tokens": _as_int(doc.get("tokens")),
        "duration": _as_float(doc.get("duration")),
    }


def _format_dashboard(doc: Dict[str, Any]) -> Dict[str, Any]:
    # 把 $facet 聚合结果整理成前端仪表盘结构:提取 summary、逐日 daily,并派生若干比率指标。
    # summary 分组结果是「至多一个元素的数组」,取第一个(空则用 {})。
    summary_doc = (doc.get("summary") or [{}])[0] if isinstance(doc.get("summary"), list) else {}
    total_requests = _as_int(summary_doc.get("total_requests"))
    successful_requests = _as_int(summary_doc.get("successful_requests"))
    total_tokens = _as_int(summary_doc.get("total_tokens"))
    total_input_tokens = _as_int(summary_doc.get("total_input_tokens"))
    total_cache_read_tokens = _as_int(summary_doc.get("total_cache_read_tokens"))
    total_duration = _as_float(summary_doc.get("total_duration"))
    scheduled_runs = _as_int(summary_doc.get("scheduled_runs"))
    total_tool_calls = _as_int(summary_doc.get("total_tool_calls"))
    failed_requests = _as_int(summary_doc.get("failed_requests"))
    # 过滤掉无日期的分组后,组装逐日明细。
    daily_items = [
        {
            "date": str(item.get("_id") or ""),
            "requests": _as_int(item.get("requests")),
            "tokens": _as_int(item.get("tokens")),
            "duration": _as_float(item.get("duration")),
            "scheduled_runs": _as_int(item.get("scheduled_runs")),
            "failed_requests": _as_int(item.get("failed_requests")),
            "tool_calls": _as_int(item.get("tool_calls")),
        }
        for item in doc.get("daily", [])
        if item.get("_id")
    ]
    # 峰值日:按 (tokens, requests, duration) 取最大的一天。
    peak_day = max(
        daily_items,
        key=lambda item: (item["tokens"], item["requests"], item["duration"]),
        default=None,
    )
    # 各派生比率均对分母为 0 做了保护(total_requests / total_input_tokens 为 0 时取 0.0)。
    summary = {
        "total_requests": total_requests,
        "total_tokens": total_tokens,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": _as_int(summary_doc.get("total_output_tokens")),
        "total_cache_read_tokens": total_cache_read_tokens,
        "total_duration": total_duration,
        "total_tool_calls": total_tool_calls,
        "scheduled_runs": scheduled_runs,
        "failed_requests": failed_requests,
        "success_rate": (successful_requests / total_requests) if total_requests else 0.0,
        "avg_tokens_per_request": (total_tokens / total_requests) if total_requests else 0.0,
        "avg_duration_per_request": (total_duration / total_requests) if total_requests else 0.0,
        "scheduled_share": (scheduled_runs / total_requests) if total_requests else 0.0,
        "cache_read_share": (
            (total_cache_read_tokens / total_input_tokens) if total_input_tokens else 0.0
        ),
        "tool_calls_per_request": ((total_tool_calls / total_requests) if total_requests else 0.0),
        "max_duration": _as_float(summary_doc.get("max_duration")),
        "peak_day": peak_day,
    }
    return {
        "summary": summary,
        "daily": daily_items,
        "top_agents": [_format_ranking_item(item) for item in doc.get("agents", [])],
        "top_teams": [_format_ranking_item(item) for item in doc.get("teams", [])],
        "top_personas": [_format_ranking_item(item) for item in doc.get("personas", [])],
        "top_models": [_format_ranking_item(item) for item in doc.get("models", [])],
        "top_users": [_format_ranking_item(item) for item in doc.get("users", [])],
        "sources": [_format_ranking_item(item) for item in doc.get("sources", [])],
        "triggers": [_format_ranking_item(item) for item in doc.get("triggers", [])],
    }


def _empty_dashboard() -> Dict[str, Any]:
    # 仪表盘空结构(聚合失败或无数据时返回),字段与 _format_dashboard 输出严格对应,保证前端契约稳定。
    return {
        "summary": {
            "total_requests": 0,
            "total_tokens": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cache_read_tokens": 0,
            "total_duration": 0.0,
            "total_tool_calls": 0,
            "scheduled_runs": 0,
            "failed_requests": 0,
            "success_rate": 0.0,
            "avg_tokens_per_request": 0.0,
            "avg_duration_per_request": 0.0,
            "scheduled_share": 0.0,
            "cache_read_share": 0.0,
            "tool_calls_per_request": 0.0,
            "max_duration": 0.0,
            "peak_day": None,
        },
        "daily": [],
        "top_agents": [],
        "top_teams": [],
        "top_personas": [],
        "top_models": [],
        "top_users": [],
        "sources": [],
        "triggers": [],
    }


def get_usage_storage() -> UsageStorage:
    """获取 UsageStorage 实例"""
    # 每次返回新实例;集合是惰性加载的,不同实例最终共享同一 Mongo 客户端连接池。
    return UsageStorage()
