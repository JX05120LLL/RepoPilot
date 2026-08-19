import { useState, type FormEvent } from "react";
import { CheckCircle, CircleNotch, SignOut, UserCircle } from "@phosphor-icons/react";
import {
  loginPlatform,
  registerPlatform,
  type PlatformSession,
} from "../lib/platformAuth";

type AccountMode = "login" | "register";

type AccountAccessProps = {
  session: PlatformSession | null;
  onAuthenticated: (session: PlatformSession) => void;
  onSignOut: () => void;
};

export function AccountAccess({ session, onAuthenticated, onSignOut }: AccountAccessProps) {
  const [mode, setMode] = useState<AccountMode>("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [email, setEmail] = useState("");
  const [tenantId, setTenantId] = useState("");
  const [role, setRole] = useState<"DEVELOPER" | "VIEWER">("DEVELOPER");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const switchMode = (next: AccountMode) => {
    setMode(next);
    setError("");
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const nextSession = mode === "login"
        ? await loginPlatform(username.trim(), password)
        : await registerPlatform({
            username: username.trim(),
            email: email.trim(),
            password,
            tenantId: tenantId.trim(),
            role,
          });
      onAuthenticated(nextSession);
      setPassword("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法连接平台服务，请稍后重试。");
    } finally {
      setBusy(false);
    }
  };

  if (session) {
    return (
      <section className="account-view" aria-labelledby="account-title">
        <header className="account-header">
          <p className="account-eyebrow">协作平台</p>
          <h2 id="account-title">账号已连接</h2>
          <p>此身份用于 Java 平台的租户与角色权限。当前本机 Agent 任务仍遵循原有本地工作区权限模型。</p>
        </header>
        <article className="account-profile">
          <UserCircle size={34} weight="duotone" aria-hidden="true" />
          <div>
            <strong>{session.username}</strong>
            <span>{session.role === "ADMIN" ? "管理员" : session.role === "VIEWER" ? "只读成员" : "开发成员"}</span>
          </div>
          <div className="account-profile-meta">
            <span>租户</span>
            <code>{session.tenantId}</code>
          </div>
          <CheckCircle className="account-connected" size={20} weight="fill" aria-label="平台连接已验证" />
        </article>
        <div className="account-actions">
          <button className="secondary-button" type="button" onClick={onSignOut}>
            <SignOut size={16} />退出当前账号
          </button>
        </div>
      </section>
    );
  }

  return (
    <section className="account-view" aria-labelledby="account-title">
      <header className="account-header">
        <p className="account-eyebrow">协作平台</p>
        <h2 id="account-title">登录以连接你的团队</h2>
        <p>账号信息由本机 Java 平台验证。Access Token 只保存到本次桌面会话，关闭应用后自动清除。</p>
      </header>

      <div className="account-mode-switch" role="tablist" aria-label="账号操作">
        <button className={mode === "login" ? "active" : ""} type="button" role="tab" aria-selected={mode === "login"} onClick={() => switchMode("login")}>登录</button>
        <button className={mode === "register" ? "active" : ""} type="button" role="tab" aria-selected={mode === "register"} onClick={() => switchMode("register")}>注册</button>
      </div>

      <form className="account-form" onSubmit={(event) => void submit(event)}>
        <label>
          用户名
          <input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" minLength={3} maxLength={64} required />
        </label>
        {mode === "register" && (
          <>
            <label>
              邮箱
              <input type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="email" maxLength={128} required />
            </label>
            <label>
              租户标识
              <input value={tenantId} onChange={(event) => setTenantId(event.target.value)} autoComplete="organization" placeholder="例如：team-shanghai" maxLength={80} required />
              <small>由团队管理员提供，用于隔离平台数据。</small>
            </label>
            <label>
              初始角色
              <select value={role} onChange={(event) => setRole(event.target.value as "DEVELOPER" | "VIEWER")}>
                <option value="DEVELOPER">开发成员</option>
                <option value="VIEWER">只读成员</option>
              </select>
            </label>
          </>
        )}
        <label>
          密码
          <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete={mode === "login" ? "current-password" : "new-password"} minLength={8} maxLength={128} required />
        </label>
        {error && <p className="account-error" role="alert">{error}</p>}
        <button className="primary-button account-submit" type="submit" disabled={busy}>
          {busy ? <CircleNotch className="spin" size={17} /> : null}
          {busy ? "正在验证" : mode === "login" ? "登录工作台" : "创建并登录"}
        </button>
      </form>
    </section>
  );
}
