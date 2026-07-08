/**
 * Agent API - Agent 相关
 *
 * Agent 领域客户端。亮点：list() 带 10 秒本地缓存 + 并发去重，减少短时间内重复拉取。
 */

import { API_BASE } from "./config";
import { authFetch } from "./fetch";
import { getAccessToken } from "./token";
import type { AgentListResponse } from "../../types";

// Agent 列表缓存：TTL 10 秒。缓存对象同时记录 authScope（当前 access token）与
// 进行中的 promise，用于：1) 命中未过期缓存直接返回；2) 并发时复用同一请求。
const AGENT_LIST_CACHE_TTL_MS = 10_000;
let agentListCache: {
  data?: AgentListResponse;
  expiresAt: number;
  authScope: string | null;
  promise?: Promise<AgentListResponse>;
} | null = null;

// 取（可能缓存的）Agent 列表：
//   1) 有未过期数据且 authScope 未变 -> 直接返回；
//   2) 有进行中的同 scope 请求 -> 复用其 promise（并发去重）；
//   3) 否则发起新请求，成功写缓存、失败清空缓存以便下次重试。
// authScope 参与判定，避免切换账号后读到上一用户的缓存。
function getCachedAgentList(url: string): Promise<AgentListResponse> {
  const now = Date.now();
  const authScope = getAccessToken();
  if (
    agentListCache?.data &&
    agentListCache.expiresAt > now &&
    agentListCache.authScope === authScope
  ) {
    return Promise.resolve(agentListCache.data);
  }
  if (agentListCache?.promise && agentListCache.authScope === authScope) {
    return agentListCache.promise;
  }

  const promise = authFetch<AgentListResponse>(url)
    .then((data) => {
      agentListCache = {
        data,
        expiresAt: Date.now() + AGENT_LIST_CACHE_TTL_MS,
        authScope,
      };
      return data;
    })
    .catch((error) => {
      agentListCache = null;
      throw error;
    });

  agentListCache = {
    promise,
    expiresAt: now + AGENT_LIST_CACHE_TTL_MS,
    authScope,
  };
  return promise;
}

export const agentApi = {
  /**
   * List all agents
   */
  async list(): Promise<AgentListResponse> {
    return getCachedAgentList(`${API_BASE}/api/agents`);
  },

  /**
   * Stream chat endpoint URL
   * 拼接某 Agent 的流式聊天端点 URL（供 SSE/EventSource 或 fetch 流式消费）。
   */
  getStreamUrl(agentId: string) {
    return `${API_BASE}/${agentId}/stream`;
  },

  /**
   * Non-streaming chat
   */
  async chat(agentId: string, message: string, sessionId?: string) {
    return authFetch(`${API_BASE}/${agentId}/chat`, {
      method: "POST",
      body: JSON.stringify({ message, session_id: sessionId }),
    });
  },
};
