/**
 * Session management hooks
 */
// 【会话管理相关 hooks】提供三类能力：
// 1) 分页/无限滚动的会话列表（按项目或收藏过滤）及其增删改与刷新；
// 2) 单个会话的加载/删除/切换；3) 消息历史加载。
// 其中 reconcileSessionList 负责把「最新拉取结果」与「本地已有列表」智能合并，兼顾软刷新体验。

import { useState, useCallback, useEffect, useRef } from "react";
import { useInView } from "react-intersection-observer";
import i18n from "i18next";
import { sessionApi, type BackendSession } from "../services/api";

// 每页拉取的会话数量
const PAGE_SIZE = 20;

// 按会话 ID 去重，保留首次出现的顺序。
function dedup(sessions: BackendSession[]): BackendSession[] {
  const seen = new Set<string>();
  return sessions.filter((s) => {
    if (seen.has(s.id)) return false;
    seen.add(s.id);
    return true;
  });
}

// 合并会话列表（软刷新时使用）：
// - removeMissing=true：以最新结果为准（过滤掉被排除项后直接返回，用于按项目/收藏的严格视图）；
// - removeMissing=false：保留最新结果，并把本地存在、但最新结果里没有的会话补回（避免误删本地新建项）。
// excludedSessionIds 用于跳过已被本地删除但服务端可能仍返回的会话。
export function reconcileSessionList(input: {
  previous: BackendSession[];
  latest: BackendSession[];
  removeMissing: boolean;
  excludedSessionIds?: ReadonlySet<string>;
}): BackendSession[] {
  const { previous, latest, removeMissing, excludedSessionIds } = input;
  const isExcluded = (session: BackendSession) =>
    excludedSessionIds?.has(session.id) ?? false;
  const visibleLatest = latest.filter((session) => !isExcluded(session));
  const latestIds = new Set(visibleLatest.map((session) => session.id));
  const merged = visibleLatest.map((session) => session);

  if (removeMissing) {
    return dedup(merged);
  }

  for (const session of previous) {
    if (!latestIds.has(session.id) && !isExcluded(session)) {
      merged.push(session);
    }
  }

  return dedup(merged);
}

// ─── Per-project paginated session list ─────────────────────────────

// 会话列表 hook 的返回：列表数据、各类加载态、无限滚动哨兵 ref，以及刷新/软刷新与本地增删改方法。
interface UseProjectSessionListReturn {
  sessions: BackendSession[];
  isLoading: boolean;
  isLoadingMore: boolean;
  hasMore: boolean;
  error: string | null;
  loadMoreRef: React.RefCallback<HTMLElement>;
  refresh: () => Promise<void>;
  softRefresh: () => Promise<void>;
  prependSession: (session: BackendSession) => void;
  removeSession: (sessionId: string) => void;
  updateSession: (session: BackendSession) => void;
}

// 列表过滤条件：按项目 ID，或仅看收藏。
interface SessionListFilter {
  projectId?: string;
  favoritesOnly?: boolean;
}

// 带分页与无限滚动的会话列表 hook。scrollRoot 指定滚动容器（用于交叉观察哨兵元素）。
export function useFilteredSessionList(
  filter: SessionListFilter,
  scrollRoot?: Element | null,
): UseProjectSessionListReturn {
  const [sessions, setSessions] = useState<BackendSession[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [skip, setSkip] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const loadedCountRef = useRef(PAGE_SIZE);
  const excludedSessionIdsRef = useRef<Set<string>>(new Set());

  const { ref: loadMoreRef, inView } = useInView({
    threshold: 0.1,
    root: scrollRoot ?? undefined,
  });

  // 拉取会话：reset=true 从头加载（重置分页），否则加载下一页并追加。
  const fetchSessions = async (reset = false) => {
    const targetSkip = reset ? 0 : skip;
    if (!reset && (isLoadingMore || !hasMore)) return;

    if (reset) {
      setIsLoading(true);
      setSkip(0);
    } else {
      setIsLoadingMore(true);
    }
    setError(null);

    try {
      const response = await sessionApi.list({
        project_id: filter.projectId,
        limit: PAGE_SIZE,
        skip: targetSkip,
        status: "active",
        favorites_only: filter.favoritesOnly,
      });

      const fetchedSessions =
        "sessions" in response
          ? response.sessions
          : Array.isArray(response)
            ? response
            : [];
      const newHasMore = "has_more" in response ? response.has_more : false;
      const newSessions = fetchedSessions.filter(
        (session) => !excludedSessionIdsRef.current.has(session.id),
      );

      if (reset) {
        setSessions(dedup(newSessions));
        setSkip(fetchedSessions.length);
        loadedCountRef.current = Math.max(PAGE_SIZE, newSessions.length);
      } else {
        setSessions((prev) => dedup([...prev, ...newSessions]));
        setSkip(targetSkip + fetchedSessions.length);
        loadedCountRef.current = Math.max(
          loadedCountRef.current,
          targetSkip + newSessions.length,
        );
      }
      setHasMore(fetchedSessions.length > 0 ? newHasMore : false);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : i18n.t("session.loadFailed", "加载会话失败"),
      );
    } finally {
      setIsLoading(false);
      setIsLoadingMore(false);
    }
  };

  // Infinite scroll
  // 无限滚动：哨兵进入视口且还有更多、且当前不在加载中时，加载下一页
  useEffect(() => {
    if (inView && hasMore && !isLoadingMore && !isLoading) {
      fetchSessions(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inView, hasMore, isLoadingMore, isLoading]);

  // Re-fetch when projectId changes
  // 过滤条件（项目/收藏）变化时清空并重新从头加载
  useEffect(() => {
    setSessions([]);
    setSkip(0);
    setHasMore(false);
    loadedCountRef.current = PAGE_SIZE;
    fetchSessions(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter.favoritesOnly, filter.projectId]);

  // 硬刷新：从头重新加载列表
  const refresh = useCallback(async () => {
    await fetchSessions(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter.favoritesOnly, filter.projectId]);

  // 软刷新（尽力而为）：一次性拉取已加载数量的会话并与本地列表智能合并，失败时静默忽略，
  // 用于后台更新列表（如收到通知）而不打断用户滚动位置
  const softRefresh = useCallback(async () => {
    try {
      const requestLimit = Math.min(
        100,
        Math.max(PAGE_SIZE, loadedCountRef.current),
      );
      const response = await sessionApi.list({
        project_id: filter.projectId,
        limit: requestLimit,
        skip: 0,
        status: "active",
        favorites_only: filter.favoritesOnly,
      });
      const newSessions =
        "sessions" in response
          ? response.sessions
          : Array.isArray(response)
            ? response
            : [];
      setSessions((prev) =>
        reconcileSessionList({
          previous: prev,
          latest: newSessions,
          removeMissing: filter.favoritesOnly || filter.projectId !== undefined,
          excludedSessionIds: excludedSessionIdsRef.current,
        }),
      );
      loadedCountRef.current = Math.max(PAGE_SIZE, newSessions.length);
      setSkip(newSessions.length);
      setHasMore("has_more" in response ? response.has_more : false);
    } catch {
      // silent — soft refresh is best-effort
    }
  }, [filter.favoritesOnly, filter.projectId]);

  // 本地插入到列表顶部（如新建会话）：解除排除标记并避免重复
  const prependSession = useCallback((session: BackendSession) => {
    excludedSessionIdsRef.current.delete(session.id);
    setSessions((prev) => {
      if (prev.some((s) => s.id === session.id)) return prev;
      return [session, ...prev];
    });
  }, []);

  // 本地移除会话：加入排除集合（防止软刷新把它带回）并从列表删除
  const removeSession = useCallback((sessionId: string) => {
    excludedSessionIdsRef.current.add(sessionId);
    setSessions((prev) => prev.filter((s) => s.id !== sessionId));
  }, []);

  // 本地更新会话（如改名/收藏）：解除排除标记并就地替换
  const updateSession = useCallback((session: BackendSession) => {
    excludedSessionIdsRef.current.delete(session.id);
    setSessions((prev) => prev.map((s) => (s.id === session.id ? session : s)));
  }, []);

  return {
    sessions,
    isLoading,
    isLoadingMore,
    hasMore,
    error,
    loadMoreRef,
    refresh,
    softRefresh,
    prependSession,
    removeSession,
    updateSession,
  };
}

// 便捷封装：按项目 ID 获取会话列表。
export function useProjectSessionList(
  projectId: string,
  scrollRoot?: Element | null,
): UseProjectSessionListReturn {
  return useFilteredSessionList({ projectId }, scrollRoot);
}

// 便捷封装：获取收藏的会话列表。
export function useFavoriteSessionList(
  scrollRoot?: Element | null,
): UseProjectSessionListReturn {
  return useFilteredSessionList({ favoritesOnly: true }, scrollRoot);
}

// ─── Single session operations ──────────────────────────────────────

// 单会话操作 hook 的返回：当前会话、加载态、错误，以及加载/删除/切换/清错方法。
interface UseSessionReturn {
  currentSession: BackendSession | null;
  isLoading: boolean;
  error: string | null;
  loadSession: (sessionId: string) => Promise<BackendSession | null>;
  deleteSession: (sessionId: string) => Promise<void>;
  switchSession: (sessionId: string | null) => void;
  clearError: () => void;
}

// 管理「当前选中会话」的加载与生命周期。
export function useSession(): UseSessionReturn {
  const [currentSession, setCurrentSession] = useState<BackendSession | null>(
    null,
  );
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 按 ID 加载会话详情并设为当前会话；返回会话对象或 null。
  const loadSession = useCallback(
    async (sessionId: string): Promise<BackendSession | null> => {
      setIsLoading(true);
      setError(null);

      try {
        const session = await sessionApi.get(sessionId);
        if (session) {
          setCurrentSession(session);
        }
        return session;
      } catch (err) {
        setError(
          err instanceof Error
            ? err.message
            : i18n.t("session.loadFailed", "加载会话失败"),
        );
        return null;
      } finally {
        setIsLoading(false);
      }
    },
    [],
  );

  // 删除会话；若删除的是当前会话则清空当前会话。
  const deleteSession = useCallback(
    async (sessionId: string) => {
      try {
        await sessionApi.delete(sessionId);
        if (currentSession?.id === sessionId) {
          setCurrentSession(null);
        }
      } catch (err) {
        setError(
          err instanceof Error
            ? err.message
            : i18n.t("session.deleteFailed", "删除会话失败"),
        );
      }
    },
    [currentSession],
  );

  // 切换会话：传入 ID 则加载，传入 null 则清空当前会话。
  const switchSession = useCallback(
    (sessionId: string | null) => {
      if (sessionId) {
        loadSession(sessionId);
      } else {
        setCurrentSession(null);
      }
    },
    [loadSession],
  );

  const clearError = useCallback(() => {
    setError(null);
  }, []);

  return {
    currentSession,
    isLoading,
    error,
    loadSession,
    deleteSession,
    switchSession,
    clearError,
  };
}

// ─── Message history loader ─────────────────────────────────────────

// 历史加载 hook 的返回：加载函数、加载态与错误。
interface UseMessageHistoryReturn {
  loadHistory: (sessionId: string) => Promise<void>;
  isLoading: boolean;
  error: string | null;
}

// 加载指定会话详情并通过 onHistoryLoaded 回调交给外层（用于恢复历史消息/配置）。
export function useMessageHistory(
  onHistoryLoaded: (session: BackendSession) => void,
): UseMessageHistoryReturn {
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadHistory = useCallback(
    async (sessionId: string) => {
      setIsLoading(true);
      setError(null);

      try {
        const session = await sessionApi.get(sessionId);
        if (session) {
          onHistoryLoaded(session);
        }
      } catch (err) {
        setError(
          err instanceof Error
            ? err.message
            : i18n.t("session.loadHistoryFailed", "加载历史记录失败"),
        );
      } finally {
        setIsLoading(false);
      }
    },
    [onHistoryLoaded],
  );

  return {
    loadHistory,
    isLoading,
    error,
  };
}
