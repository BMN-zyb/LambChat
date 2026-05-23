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
    const res = await authFetch(`${BASE}?skip=${skip}&limit=${limit}`);
    if (!res.ok) throw new Error("Failed to list teams");
    return res.json();
  },

  async get(teamId: string): Promise<Team> {
    const res = await authFetch(`${BASE}/${teamId}`);
    if (!res.ok) throw new Error("Failed to get team");
    return res.json();
  },

  async create(data: TeamCreateRequest): Promise<Team> {
    const res = await authFetch(BASE, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    if (!res.ok) throw new Error("Failed to create team");
    return res.json();
  },

  async update(teamId: string, data: TeamUpdateRequest): Promise<Team> {
    const res = await authFetch(`${BASE}/${teamId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    if (!res.ok) throw new Error("Failed to update team");
    return res.json();
  },

  async delete(teamId: string): Promise<void> {
    const res = await authFetch(`${BASE}/${teamId}`, { method: "DELETE" });
    if (!res.ok) throw new Error("Failed to delete team");
  },

  async clone(teamId: string): Promise<Team> {
    const res = await authFetch(`${BASE}/${teamId}/clone`, {
      method: "POST",
    });
    if (!res.ok) throw new Error("Failed to clone team");
    return res.json();
  },
};
