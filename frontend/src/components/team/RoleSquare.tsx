import { useMemo } from "react";
import { Search, Plus } from "lucide-react";
import type { PersonaPreset } from "../../types";

interface RoleSquareProps {
  presets: PersonaPreset[];
  loading?: boolean;
  onAddRole: (preset: PersonaPreset) => void;
  searchQuery: string;
  onSearchChange: (q: string) => void;
}

export function RoleSquare({
  presets,
  loading,
  onAddRole,
  searchQuery,
  onSearchChange,
}: RoleSquareProps) {
  const filtered = useMemo(() => {
    if (!searchQuery.trim()) return presets;
    const q = searchQuery.toLowerCase();
    return presets.filter(
      (p) =>
        p.name.toLowerCase().includes(q) ||
        p.description.toLowerCase().includes(q) ||
        p.tags.some((t) => t.toLowerCase().includes(q)),
    );
  }, [presets, searchQuery]);

  return (
    <div className="flex flex-col h-full">
      <div className="p-3 border-b border-border">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <input
            type="text"
            placeholder="Search roles..."
            value={searchQuery}
            onChange={(e) => onSearchChange(e.target.value)}
            className="w-full pl-9 pr-3 py-2 rounded-lg bg-muted text-sm"
          />
        </div>
      </div>
      <div className="flex-1 overflow-y-auto p-3 space-y-2">
        {loading && (
          <p className="text-sm text-muted-foreground text-center py-4">
            Loading roles...
          </p>
        )}
        {!loading && filtered.length === 0 && (
          <p className="text-sm text-muted-foreground text-center py-4">
            No roles found
          </p>
        )}
        {filtered.map((preset) => (
          <div
            key={preset.id}
            className="flex items-center gap-3 p-2 rounded-lg hover:bg-muted/50 group"
          >
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium truncate">{preset.name}</p>
              {preset.description && (
                <p className="text-xs text-muted-foreground truncate">
                  {preset.description}
                </p>
              )}
              {preset.tags.length > 0 && (
                <div className="flex gap-1 mt-1 flex-wrap">
                  {preset.tags.slice(0, 3).map((tag) => (
                    <span
                      key={tag}
                      className="px-1.5 py-0.5 text-[10px] rounded bg-muted text-muted-foreground"
                    >
                      {tag}
                    </span>
                  ))}
                </div>
              )}
            </div>
            <button
              onClick={() => onAddRole(preset)}
              className="opacity-0 group-hover:opacity-100 p-1.5 rounded-md hover:bg-accent transition-opacity"
              title="Add to team"
            >
              <Plus className="h-4 w-4" />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
