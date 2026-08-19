export type PermissionMode = "safe-isolated" | "full-local";
export type ComposerMode = "auto" | "chat" | "research" | "change";

/**
 * 完全本机权限只对代码任务有意义。将带项目的普通/自动对话提升为只读研究，
 * 避免界面显示“完全访问”而实际仍调用普通聊天接口。
 */
export function resolveComposerMode(
  composerMode: ComposerMode,
  permissionMode: PermissionMode,
  hasProject: boolean,
): Exclude<ComposerMode, "auto"> {
  if (
    hasProject &&
    permissionMode === "full-local" &&
    (composerMode === "auto" || composerMode === "chat")
  ) {
    return "research";
  }
  return composerMode === "auto" ? "chat" : composerMode;
}
