const DEFAULT_API = "http://127.0.0.1:8765/api";

/** 桌面端只允许连接显式的 loopback API，避免配置把本地权限面暴露给远端。 */
function configuredApiBase(): string {
  const configured = import.meta.env.VITE_REPOPILOT_API_URL;
  if (!configured) return DEFAULT_API;
  try {
    const url = new URL(configured);
    const port = Number(url.port);
    if (
      (url.hostname === "127.0.0.1" || url.hostname === "localhost") &&
      Number.isInteger(port) &&
      port >= 1 &&
      port <= 65_535 &&
      url.pathname.replace(/\/+$/, "") === "/api" &&
      !url.search &&
      !url.hash
    ) {
      return url.toString().replace(/\/$/, "");
    }
  } catch {
    // 非法预览配置回退到固定本机地址。
  }
  return DEFAULT_API;
}

export const API = configuredApiBase();
