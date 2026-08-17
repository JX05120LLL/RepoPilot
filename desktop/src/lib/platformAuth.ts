const DEFAULT_PLATFORM_API = "http://127.0.0.1:8081/api";
const PLATFORM_SESSION_KEY = "repopilot.platform-session.v1";

export type PlatformSession = {
  accessToken: string;
  username: string;
  role: string;
  tenantId: string;
  expiresAt: number;
};

export type PlatformProfile = {
  username: string;
  authorities: string[];
  tenantId: string;
};

type TokenResponse = {
  accessToken?: unknown;
  expiresInSeconds?: unknown;
  username?: unknown;
  role?: unknown;
  tenantId?: unknown;
};

type PlatformError = {
  message?: unknown;
};

function configuredPlatformApi(): string {
  const configured = import.meta.env.VITE_REPOPILOT_PLATFORM_API_URL;
  if (!configured) return DEFAULT_PLATFORM_API;
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
    // 非法地址继续使用固定本机 Java 平台地址。
  }
  return DEFAULT_PLATFORM_API;
}

export const PLATFORM_API = configuredPlatformApi();

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function readNonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

async function platformError(response: Response): Promise<Error> {
  try {
    const payload = (await response.json()) as PlatformError;
    const message = readNonEmptyString(payload.message);
    if (message) return new Error(message);
  } catch {
    // 非 JSON 错误响应不会泄露服务端正文。
  }
  return new Error(`平台服务请求失败（HTTP ${response.status}）`);
}

function sessionFromResponse(payload: TokenResponse): PlatformSession {
  const accessToken = readNonEmptyString(payload.accessToken);
  const username = readNonEmptyString(payload.username);
  const role = readNonEmptyString(payload.role);
  const tenantId = readNonEmptyString(payload.tenantId);
  const expiresInSeconds = typeof payload.expiresInSeconds === "number"
    ? payload.expiresInSeconds
    : Number.NaN;
  if (!accessToken || !username || !role || !tenantId || !Number.isFinite(expiresInSeconds) || expiresInSeconds <= 0) {
    throw new Error("平台返回的登录信息不完整，请检查服务版本。");
  }
  return {
    accessToken,
    username,
    role,
    tenantId,
    expiresAt: Date.now() + expiresInSeconds * 1_000,
  };
}

async function requestToken(path: string, body: Record<string, string>): Promise<PlatformSession> {
  const response = await fetch(`${PLATFORM_API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw await platformError(response);
  return sessionFromResponse((await response.json()) as TokenResponse);
}

export async function loginPlatform(username: string, password: string): Promise<PlatformSession> {
  return requestToken("/auth/login", { username, password });
}

export async function registerPlatform(input: {
  username: string;
  email: string;
  password: string;
  tenantId: string;
  role: "DEVELOPER" | "VIEWER";
}): Promise<PlatformSession> {
  return requestToken("/auth/register", input);
}

export async function fetchPlatformProfile(accessToken: string): Promise<PlatformProfile> {
  const response = await fetch(`${PLATFORM_API}/me`, {
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) throw await platformError(response);
  const payload: unknown = await response.json();
  if (!isRecord(payload)) throw new Error("平台返回的账号信息格式无效。");
  const username = readNonEmptyString(payload.username);
  const tenantId = readNonEmptyString(payload.tenant_id);
  const authorities = Array.isArray(payload.authorities)
    ? payload.authorities.filter((value): value is string => typeof value === "string")
    : [];
  if (!username || !tenantId) throw new Error("平台返回的账号信息不完整。");
  return { username, tenantId, authorities };
}

export function loadPlatformSession(): PlatformSession | null {
  try {
    const raw = window.sessionStorage.getItem(PLATFORM_SESSION_KEY);
    if (!raw) return null;
    const payload: unknown = JSON.parse(raw);
    if (!isRecord(payload)) return null;
    const accessToken = readNonEmptyString(payload.accessToken);
    const username = readNonEmptyString(payload.username);
    const role = readNonEmptyString(payload.role);
    const tenantId = readNonEmptyString(payload.tenantId);
    const expiresAt = typeof payload.expiresAt === "number" ? payload.expiresAt : Number.NaN;
    if (!accessToken || !username || !role || !tenantId || !Number.isFinite(expiresAt) || expiresAt <= Date.now()) {
      window.sessionStorage.removeItem(PLATFORM_SESSION_KEY);
      return null;
    }
    return { accessToken, username, role, tenantId, expiresAt };
  } catch {
    window.sessionStorage.removeItem(PLATFORM_SESSION_KEY);
    return null;
  }
}

export function savePlatformSession(session: PlatformSession): void {
  // 只保留 Access Token 到 sessionStorage，不保存 Refresh Token，也不跨桌面会话持久化。
  window.sessionStorage.setItem(PLATFORM_SESSION_KEY, JSON.stringify(session));
}

export function clearPlatformSession(): void {
  window.sessionStorage.removeItem(PLATFORM_SESSION_KEY);
}
