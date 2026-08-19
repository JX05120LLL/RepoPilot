export type RestorableView = "task" | "context" | "settings" | "review" | "account";

export type WorkbenchPreferences = {
  projectId?: string;
  threadId?: string;
  activeView?: RestorableView;
  showTaskInspector?: boolean;
  showTaskTerminal?: boolean;
};

const STORAGE_KEY = "repopilot.workbench.preferences.v1";
const MAX_ID_LENGTH = 256;

function readId(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 && value.length <= MAX_ID_LENGTH
    ? value
    : undefined;
}

export function loadWorkbenchPreferences(): WorkbenchPreferences {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const value = JSON.parse(raw) as Record<string, unknown>;
    const activeView = value.activeView;
    return {
      projectId: readId(value.projectId),
      threadId: readId(value.threadId),
      activeView:
        activeView === "task" || activeView === "context" || activeView === "settings" || activeView === "review" || activeView === "account"
          ? activeView
          : undefined,
      showTaskInspector:
        typeof value.showTaskInspector === "boolean"
          ? value.showTaskInspector
          : undefined,
      showTaskTerminal:
        typeof value.showTaskTerminal === "boolean"
          ? value.showTaskTerminal
          : undefined,
    };
  } catch {
    return {};
  }
}

export function saveWorkbenchPreferences(preferences: WorkbenchPreferences): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(preferences));
  } catch {
    // 存储不可用不影响任务运行或服务端持久化。
  }
}
