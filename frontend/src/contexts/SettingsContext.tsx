// 全局设置 Context：在 useSettings（基础设置读写）之上，额外聚合「可用模型列表」
// 与「置顶模型」等需要跨页面共享的状态，统一通过 Provider 下发。
// 消费方用 useSettingsContext（强制在 Provider 内）或 useOptionalSettingsContext
// （可选，返回 undefined 而不抛错）获取。
import {
  createContext,
  useContext,
  ReactNode,
  useMemo,
  useState,
  useEffect,
  useCallback,
} from "react";
import { useSettings } from "../hooks/useSettings";
import { useAuth } from "../hooks/useAuth";
import { modelApi } from "../services/api";
import type { SettingsResponse } from "../types";
import type { ModelProfile } from "../services/api/model";

export interface AvailableModel {
  id: string;
  value: string;
  provider?: string;
  icon?: string;
  label: string;
  description?: string;
  profile?: ModelProfile;
}

interface SettingsContextValue {
  settings: SettingsResponse | null;
  enableSkills: boolean;
  enableMemory: boolean;
  isLoading: boolean;
  error: string | null;
  savingKeys: Set<string>;
  availableModels: AvailableModel[] | null;
  defaultModel: string;
  pinnedModelIds: string[];
  togglePinnedModel: (modelId: string) => void;
  updateSetting: (
    key: string,
    value: string | number | boolean | object,
  ) => Promise<boolean>;
  resetSetting: (key: string) => Promise<boolean>;
  resetAllSettings: () => Promise<boolean>;
  clearError: () => void;
  exportSettings: () => void;
  importSettings: (
    file: File,
  ) => Promise<{ success: boolean; updatedCount: number; errors: string[] }>;
}

const SettingsContext = createContext<SettingsContextValue | undefined>(
  undefined,
);

export function SettingsProvider({ children }: { children: ReactNode }) {
  const {
    settings,
    isLoading,
    error,
    savingKeys,
    getBooleanSetting,
    updateSetting,
    resetSetting,
    resetAllSettings,
    clearError,
    exportSettings,
    importSettings,
  } = useSettings();

  const { isAuthenticated } = useAuth();

  // 可用模型列表：管理员在后端配置的模型清单，供全局选择器使用
  // 从 DB 的 model_configs 读取可用模型
  const [dbModels, setDbModels] = useState<AvailableModel[] | null>(null);
  // 管理员设定的默认模型 ID（用于推导 defaultModel）
  const [adminDefaultModelId, setAdminDefaultModelId] = useState<string>("");

  // 用户置顶的模型（置顶后在选择器中优先展示）
  // 置顶模型 ID
  const [pinnedModelIds, setPinnedModelIds] = useState<string[]>([]);

  // 拉取可用模型：把后端返回结构映射为前端 AvailableModel；无数据或出错时置空。
  const fetchModels = useCallback(() => {
    modelApi
      .listAvailable()
      .then((data) => {
        setAdminDefaultModelId(data.default_model_id || "");
        if (data.models && data.models.length > 0) {
          setDbModels(
            data.models.map((m) => ({
              id: m.id || "",
              value: m.value,
              provider: m.provider,
              icon: m.icon,
              label: m.label,
              description: m.description,
              profile: m.profile,
            })),
          );
        } else {
          setDbModels(null);
        }
      })
      .catch(() => {
        setAdminDefaultModelId("");
        setDbModels(null);
      });
  }, []);

  const fetchPinnedModels = useCallback(() => {
    modelApi
      .getPinnedModelIds()
      .then(setPinnedModelIds)
      .catch(() => {});
  }, []);

  // 仅在已登录时拉取模型与置顶列表（这些接口需要鉴权）
  useEffect(() => {
    if (isAuthenticated) {
      fetchModels();
      fetchPinnedModels();
    }
  }, [isAuthenticated, fetchModels, fetchPinnedModels]);

  // 切换置顶：本地乐观更新（有则移除、无则加入），并异步同步到后端；失败静默。
  const togglePinnedModel = useCallback((modelId: string) => {
    setPinnedModelIds((prev) => {
      const next = prev.includes(modelId)
        ? prev.filter((id) => id !== modelId)
        : [...prev, modelId];
      modelApi.updatePinnedModelIds(next).catch(() => {});
      return next;
    });
  }, []);

  // Auto-clean orphaned pinned IDs (models that were deleted)
  // 自动清理失效的置顶 ID：过滤掉已被删除、不在当前可用模型中的置顶项。
  const cleanedPinnedIds = useMemo(() => {
    if (!dbModels || pinnedModelIds.length === 0) return pinnedModelIds;
    const validIds = new Set(dbModels.map((m) => m.id));
    const cleaned = pinnedModelIds.filter((id) => validIds.has(id));
    return cleaned;
  }, [dbModels, pinnedModelIds]);

  // 当清理后数量发生变化，回写清理结果到 state 与后端，保持一致。
  useEffect(() => {
    if (cleanedPinnedIds.length === pinnedModelIds.length) return;
    setPinnedModelIds(cleanedPinnedIds);
    modelApi.updatePinnedModelIds(cleanedPinnedIds).catch(() => {});
  }, [cleanedPinnedIds, pinnedModelIds.length]);

  // 从 DB 读取模型
  const availableModels = useMemo(() => {
    return dbModels;
  }, [dbModels]);

  // 推导默认模型的 value：优先用管理员默认模型 ID 对应项，否则取列表首个。
  const defaultModel = useMemo(() => {
    if (!availableModels || availableModels.length === 0) {
      return "";
    }
    return (
      availableModels.find((model) => model.id === adminDefaultModelId)
        ?.value || availableModels[0].value
    );
  }, [adminDefaultModelId, availableModels]);

  const value: SettingsContextValue = {
    settings,
    enableSkills: getBooleanSetting("ENABLE_SKILLS"),
    enableMemory: getBooleanSetting("ENABLE_MEMORY"),
    availableModels,
    defaultModel,
    pinnedModelIds: cleanedPinnedIds,
    togglePinnedModel,
    isLoading,
    error,
    savingKeys,
    updateSetting,
    resetSetting,
    resetAllSettings,
    clearError,
    exportSettings,
    importSettings,
  };

  return (
    <SettingsContext.Provider value={value}>
      {children}
    </SettingsContext.Provider>
  );
}

// Fast refresh only works when a file only exports components.
// Use a new file to share constants or functions between components
// eslint-disable-next-line react-refresh/only-export-components
// 消费 Hook：必须在 SettingsProvider 内使用，否则抛错以尽早暴露用法错误。
export function useSettingsContext() {
  const context = useContext(SettingsContext);
  if (context === undefined) {
    throw new Error(
      "useSettingsContext must be used within a SettingsProvider",
    );
  }
  return context;
}

// eslint-disable-next-line react-refresh/only-export-components
// 可选消费 Hook：不在 Provider 内时返回 undefined 而非抛错，供「可有可无设置」的组件使用。
export function useOptionalSettingsContext() {
  return useContext(SettingsContext);
}
