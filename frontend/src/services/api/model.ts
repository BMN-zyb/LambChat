/**
 * Model API - 模型配置 CRUD
 *
 * 模型配置领域客户端。读多用缓存（按 URL 维度、10 秒 TTL、并发去重、随 authScope 失效），
 * 任何写操作(create/update/delete/toggle/reorder/import…)完成后都会 clearModelListCache()
 * 使缓存整体失效，保证下次读到最新数据。
 */

import { API_BASE } from "./config";
import { authFetch } from "./fetch";
import { getAccessToken } from "./token";

const MODEL_LIST_CACHE_TTL_MS = 10_000;

interface ModelListCacheEntry<T> {
  data?: T;
  expiresAt: number;
  authScope: string | null;
  promise?: Promise<T>;
}

// 与 agent.ts 单例缓存不同，这里用 Map 按不同列表 URL（含查询参数）分别缓存。
const modelListCache = new Map<string, ModelListCacheEntry<unknown>>();

// 写操作后调用：清空全部模型列表缓存，避免读到过期数据。
function clearModelListCache(): void {
  modelListCache.clear();
}

// 取（可能缓存的）模型列表：命中未过期且同 authScope 的缓存直接返回；
// 有进行中的同 scope 请求则复用；否则发起新请求并在成功后写缓存、失败时删除该项。
function getCachedModelList<T>(url: string): Promise<T> {
  const now = Date.now();
  const authScope = getAccessToken();
  const cached = modelListCache.get(url) as ModelListCacheEntry<T> | undefined;

  if (
    cached?.data &&
    cached.expiresAt > now &&
    cached.authScope === authScope
  ) {
    return Promise.resolve(cached.data);
  }

  if (cached?.promise && cached.authScope === authScope) {
    return cached.promise;
  }

  const promise = authFetch<T>(url)
    .then((data) => {
      modelListCache.set(url, {
        data,
        expiresAt: Date.now() + MODEL_LIST_CACHE_TTL_MS,
        authScope,
      });
      return data;
    })
    .catch((error) => {
      modelListCache.delete(url);
      throw error;
    });

  modelListCache.set(url, {
    promise,
    expiresAt: now + MODEL_LIST_CACHE_TTL_MS,
    authScope,
  });

  return promise;
}

// ============================================
// API Types
// ============================================

export interface ModelProfile {
  max_input_tokens?: number;
  supports_vision?: boolean;
  image_url_to_base64?: boolean;
}

/** LLM API provider type (dynamic, from backend PROVIDER_REGISTRY) */
export type ProviderType = string;

/** Shared model option used in selectors and role config */
export interface ModelOption {
  id: string;
  value: string;
  provider?: string;
  icon?: string;
  label: string;
  description?: string;
  profile?: ModelProfile;
}

export interface ModelConfig {
  id?: string;
  value: string;
  provider?: ProviderType;
  icon?: string;
  label: string;
  description?: string;
  api_key?: string;
  api_base?: string;
  temperature?: number;
  max_tokens?: number;
  profile?: ModelProfile;
  fallback_model?: string;
  enabled: boolean;
  order: number;
  created_at?: string;
  updated_at?: string;
}

export interface ModelConfigCreate {
  value: string;
  provider?: ProviderType;
  icon?: string;
  label: string;
  description?: string;
  api_key?: string;
  api_base?: string;
  temperature?: number;
  max_tokens?: number;
  profile?: ModelProfile;
  fallback_model?: string;
  enabled?: boolean;
  order?: number;
}

export interface ModelConfigUpdate {
  provider?: ProviderType;
  icon?: string;
  label?: string;
  description?: string;
  api_key?: string;
  api_base?: string;
  temperature?: number;
  max_tokens?: number;
  profile?: ModelProfile;
  fallback_model?: string;
  enabled?: boolean;
  order?: number;
}

export interface ModelListResponse {
  models: ModelConfig[];
  count: number;
  enabled_count: number;
}

export interface AvailableModelListResponse {
  models: ModelOption[];
  count: number;
  enabled_count: number;
  default_model_id?: string | null;
}

export interface ModelResponse {
  model: ModelConfig;
  message?: string;
}

// ============================================
// API Methods
// ============================================

export const modelApi = {
  /** 列出所有模型 */
  async list(includeDisabled = false): Promise<ModelListResponse> {
    return getCachedModelList<ModelListResponse>(
      `${API_BASE}/api/agent/models/?include_disabled=${includeDisabled}`,
    );
  },

  /** 列出所有可用的模型（任何已认证用户） */
  // 与 list() 的区别：list 是管理端全量（可含禁用），available 面向普通用户，
  // 只返回启用模型并带 default_model_id，供聊天页模型选择器使用。
  async listAvailable(): Promise<AvailableModelListResponse> {
    return getCachedModelList<AvailableModelListResponse>(
      `${API_BASE}/api/agent/models/available`,
    );
  },

  /** 获取单个模型 */
  async get(modelId: string): Promise<ModelResponse> {
    return authFetch<ModelResponse>(`${API_BASE}/api/agent/models/${modelId}`);
  },

  /** 创建模型 */
  async create(model: ModelConfigCreate): Promise<ModelResponse> {
    const response = await authFetch<ModelResponse>(
      `${API_BASE}/api/agent/models/`,
      {
        method: "POST",
        body: JSON.stringify(model),
      },
    );
    clearModelListCache();
    return response;
  },

  /** 更新模型 */
  async update(
    modelId: string,
    update: ModelConfigUpdate,
  ): Promise<ModelResponse> {
    const response = await authFetch<ModelResponse>(
      `${API_BASE}/api/agent/models/${modelId}`,
      {
        method: "PUT",
        body: JSON.stringify(update),
      },
    );
    clearModelListCache();
    return response;
  },

  /** 删除模型 */
  async delete(modelId: string): Promise<void> {
    await authFetch<void>(`${API_BASE}/api/agent/models/${modelId}`, {
      method: "DELETE",
    });
    clearModelListCache();
  },

  /** 启用/禁用模型 */
  async toggle(modelId: string, enabled: boolean): Promise<ModelResponse> {
    const response = await authFetch<ModelResponse>(
      `${API_BASE}/api/agent/models/${modelId}/toggle?enabled=${enabled}`,
      {
        method: "POST",
      },
    );
    clearModelListCache();
    return response;
  },

  /** 批量更新顺序 */
  async reorder(modelIds: string[]): Promise<ModelListResponse> {
    const response = await authFetch<ModelListResponse>(
      `${API_BASE}/api/agent/models/reorder`,
      {
        method: "PUT",
        body: JSON.stringify(modelIds),
      },
    );
    clearModelListCache();
    return response;
  },

  /** 批量导入模型 (upsert) */
  async importModels(models: ModelConfigCreate[]): Promise<ModelListResponse> {
    const response = await authFetch<ModelListResponse>(
      `${API_BASE}/api/agent/models/import`,
      {
        method: "POST",
        body: JSON.stringify(models),
      },
    );
    clearModelListCache();
    return response;
  },

  /** 批量创建模型（共享配置） */
  async batchCreate(
    shared: Record<string, unknown>,
    models: { value: string; label: string; description?: string }[],
  ): Promise<ModelListResponse> {
    const response = await authFetch<ModelListResponse>(
      `${API_BASE}/api/agent/models/batch-create`,
      {
        method: "POST",
        body: JSON.stringify({ shared, models }),
      },
    );
    clearModelListCache();
    return response;
  },

  /** 删除所有模型 */
  async deleteAll(): Promise<void> {
    await authFetch<void>(`${API_BASE}/api/agent/models/`, {
      method: "DELETE",
    });
    clearModelListCache();
  },

  /** 列出所有支持的 LLM 供应商 */
  async listProviders(): Promise<
    { value: string; protocol: string; prefixes: string[] }[]
  > {
    return authFetch(`${API_BASE}/api/agent/models/providers/list`);
  },

  /** 获取当前用户的置顶模型 ID 列表 */
  // 置顶模型不是独立资源，而是存放在用户 profile 的 metadata.pinned_model_ids 里。
  async getPinnedModelIds(): Promise<string[]> {
    const user = await authFetch<{
      metadata?: { pinned_model_ids?: string[] };
    }>(`${API_BASE}/api/auth/profile`);
    return user.metadata?.pinned_model_ids ?? [];
  },

  /** 更新当前用户的置顶模型 ID 列表 */
  async updatePinnedModelIds(ids: string[]): Promise<string[]> {
    const user = await authFetch<{
      metadata?: { pinned_model_ids?: string[] };
    }>(`${API_BASE}/api/auth/profile/metadata`, {
      method: "PUT",
      body: JSON.stringify({ metadata: { pinned_model_ids: ids } }),
    });
    return user.metadata?.pinned_model_ids ?? [];
  },
};
