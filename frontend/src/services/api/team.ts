import { authFetch } from "./fetch";
import { API_BASE } from "./config";
import type {
  Team,
  TeamCreateRequest,
  TeamUpdateRequest,
  TeamListResponse,
} from "../../types/team";

const BASE = `${API_BASE}/teams`;

export const teamApi = {
  async list(skip = 0, limit = 20): Promise<TeamListResponse> {
    return authFetch<TeamListResponse>(`${BASE}?skip=${skip}&limit=${limit}`);
  },

  async get(teamId: string): Promise<Team> {
    return authFetch<Team>(`${BASE}/${teamId}`);
  },

  async create(data: TeamCreateRequest): Promise<Team> {
    return authFetch<Team>(BASE, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
  },

  async update(teamId: string, data: TeamUpdateRequest): Promise<Team> {
    return authFetch<Team>(`${BASE}/${teamId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
  },

  async delete(teamId: string): Promise<void> {
    await authFetch(`${BASE}/${teamId}`, { method: "DELETE" });
  },

  async clone(teamId: string): Promise<Team> {
    return authFetch<Team>(`${BASE}/${teamId}/clone`, {
      method: "POST",
    });
  },
};
