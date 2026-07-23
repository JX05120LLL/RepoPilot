import { useEffect, useState } from "react";
import { open } from "@tauri-apps/plugin-dialog";
import {
  Archive,
  ArrowRight,
  ArrowClockwise,
  ArrowUp,
  CaretDown,
  CheckCircle,
  ChatCircle,
  CircleNotch,
  FileArrowUp,
  FileCode,
  FolderOpen,
  ListMagnifyingGlass,
  MagnifyingGlass,
  Paperclip,
  Plus,
  PuzzlePiece,
  ShieldCheck,
  SlidersHorizontal,
  Stack,
  TerminalWindow,
  WarningCircle,
  XCircle,
} from "@phosphor-icons/react";

const API = "http://127.0.0.1:8765/api";
const API_UNAVAILABLE_MESSAGE = "本机 API 尚未启动或无法访问。";
const EVIDENCE_STREAM_ERROR =
  "证据流连接中断，请检查本机 API。任务状态会继续尝试轮询。";
type Mode = "safe-isolated" | "full-local";
type Operation = "change" | "research";
type WorkspaceView = "task" | "context" | "review";
type Project = {
  project_id: string;
  display_name: string;
  root_path?: string;
  is_git_repository?: boolean;
};
type Interrupt = { type: string; message?: string };
type Task = {
  thread_id: string;
  trace_id?: string;
  task_id?: string;
  display_title?: string | null;
  project_id?: string | null;
  task_mode?: string;
  task_operation?: Operation;
  task_description?: string;
  created_at?: string;
  updated_at?: string;
  status: string;
  pending_approval: boolean;
  verdict?: string | null;
  archived_at?: string | null;
  interrupts?: Interrupt[];
  state?: {
    task_operation?: string;
    task_description?: string;
  };
};
type Artifact = {
  kind: string;
  relative_path: string;
  sha256: string;
  size_bytes: number;
  updated_at: string;
};
type ArtifactVersion = {
  kind: string;
  version: number;
  sha256: string;
  size_bytes: number;
  created_at: string;
};
type TimelineEvent = {
  id: string;
  type: string;
  payload: Record<string, unknown>;
};
type McpProbeResult = {
  status: string;
  code: string;
  connection?: {
    server?: {
      state?: string;
      session_info?: {
        server_name?: string;
        server_version?: string;
        protocol_version?: string;
      };
    };
    tools?: Array<{
      capability_id: string;
      description: string;
      risks: string[];
    }>;
  };
  closed?: { state?: string };
};
type ContextSnapshot = {
  snapshot_sha256: string;
  included_chars: number;
  omitted_items: number;
  sources: Array<{
    source_type: string;
    path: string;
    line_start?: number | null;
    line_end?: number | null;
  }>;
  selected_skills: Array<{
    name: string;
    scope: string;
    content_sha256: string;
  }>;
  bound_tool_ids: string[];
  capability_ids: string[];
};
type Telemetry = {
  node_count: number;
  node_total_duration_ms: number;
  model: {
    reported_operations: number;
    unavailable_operations: number;
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    estimated_cost: number | null;
    currency: string | null;
  };
  budget: {
    configured: boolean;
    max_total_tokens: number | null;
    max_estimated_cost: number | null;
    currency: string | null;
    status: string;
    code: string | null;
  };
};
type Plugin = {
  plugin_id: string;
  enabled: boolean;
  active: boolean;
  integrity_status: string;
  manifest: {
    name: string;
    version: string;
    description: string;
    skills_root?: string | null;
    mcp_config?: string | null;
  };
};
type DocumentIndexResult = {
  status: string;
  code: string;
  message?: string;
  indexed_chunks?: number;
  skipped_chunks?: number;
  document?: {
    document_id: string;
    display_name: string;
    content_sha256?: string;
  };
};
type ManagedDocument = {
  document_id: string;
  display_name: string;
  content_sha256: string;
  imported_at: string;
};
type RuntimeHealth = {
  status: "UNKNOWN" | "READY" | "BLOCKED";
  code: string;
  message?: string;
};
type ProjectModeReadiness = {
  status: "READY" | "BLOCKED";
  code: string;
  message: string;
  dirty_entry_count?: number;
  allowed_operations?: Operation[];
};
type ProjectDiagnosis = {
  recommended_task_mode: Mode;
  recommended_task_operation?: Operation;
  task_modes: {
    safe_isolated: ProjectModeReadiness;
    full_local: ProjectModeReadiness;
  };
  git: { is_repository: boolean; baseline_commit: string | null; dirty_entry_count: number };
  profiles: { java_maven: { status: string; code: string; warnings: string[] } };
};
type TaskOutcome = {
  tone: "neutral" | "success" | "warning" | "danger";
  title: string;
  detail: string;
};
type UnifiedDiffLine = {
  kind: "add" | "remove" | "context" | "meta" | "hunk";
  content: string;
  oldLine: number | null;
  newLine: number | null;
};

const artifactLabels: Record<string, string> = {
  report: "任务报告",
  plan_markdown: "修改计划",
  plan_json: "计划 JSON",
  patch_proposal: "补丁提案",
  git_diff: "真实 Diff",
  verification: "验证结果",
  telemetry: "运行遥测",
};

const eventLabels: Record<string, string> = {
  TASK_CREATED: "任务已创建",
  GRAPH_NODE_STARTED: "开始执行节点",
  GRAPH_NODE_COMPLETED: "完成工作节点",
  TOOL_CALL_STARTED: "调用受控工具",
  TOOL_CALL_COMPLETED: "工具返回结果",
  TASK_BUDGET_SNAPSHOT: "任务预算已冻结",
  WORKSPACE_PREPARED: "工作区已准备",
  PREFLIGHT_COMPLETED: "环境预检完成",
  MCP_BINDINGS_DISCOVERED: "MCP 能力已检查",
  CONTEXT_INGESTED: "项目上下文已索引",
  CONTEXT_RETRIEVED: "项目上下文已检索",
  CONTEXT_BROKER_ASSEMBLED: "模型上下文已组装",
  APPROVAL_REQUIRED: "等待人工审批",
  TASK_STATUS_CHANGED: "任务状态更新",
  MODEL_USAGE_RECORDED: "模型用量已记录",
  MODEL_USAGE: "模型用量已记录",
  TOOL_CALL: "调用受控工具",
  NODE_COMPLETED: "完成工作节点",
  PLAN_GENERATED: "已生成修改计划",
  RESEARCH_LIMIT_REACHED: "研究轮次已达上限",
  EVIDENCE: "记录执行证据",
  TASK_RUNTIME_FAILED: "任务运行失败",
  TASK_METADATA_RECOVERED: "已恢复任务信息",
  TASK_EXECUTION_STARTED: "任务开始执行",
  TASK_STATE: "任务状态已更新",
  PLAN_APPROVED: "修改计划已批准",
  EXECUTION_APPROVED: "执行操作已批准",
};

const eventSummaryLabels: Record<string, string> = {
  INTAKE: "任务输入、权限与工作区选择已校验。",
  WORKSPACE: "工作区已绑定，Git 基线和目录边界已检查。",
  PREFLIGHT: "本机依赖、模型服务和项目条件已检查。",
  MCP_BINDINGS: "MCP 工具发现与任务级授权已检查。",
  INGEST: "代码与研发文档索引状态已更新。",
  RETRIEVE: "已按项目和代码基线检索上下文。",
  CONTEXT_BROKER_READY: "模型上下文已在预算与来源边界内冻结。",
  ANALYZE: "代码分析阶段已完成。",
  RESEARCH_TOOLS: "受控只读工具研究已完成。",
  PLAN: "修改计划阶段已完成。",
  PLAN_APPROVAL: "计划审批结果已写入任务状态。",
  EXECUTION_APPROVAL: "执行审批结果已写入任务状态。",
  RUNNING: "任务执行器已开始处理。",
  WAITING_APPROVAL: "任务已暂停，正在等待人工审批。",
  MODEL_USAGE_REPORTED: "本次模型用量已纳入任务审计。",
};

function eventSummary(event: TimelineEvent): string {
  const candidates = [
    event.payload.message,
    event.payload.summary,
    event.payload.code,
    event.payload.node,
    event.payload.tool_name,
    event.payload.status,
  ];
  const summary = candidates.find(
    (value) => typeof value === "string" && value.trim(),
  );
  if (typeof summary !== "string") return "已写入可审计事件。";
  if (eventSummaryLabels[summary]) return eventSummaryLabels[summary];
  if (summary.startsWith("TASK_RUNTIME_FAILED:")) {
    return "运行时操作失败，任务已按策略安全阻断。";
  }
  return summary;
}

function resolveTaskOutcome(item: Task, running: boolean): TaskOutcome {
  if (item.pending_approval) {
    return {
      tone: "warning",
      title: "等待你的审批",
      detail: "任务已暂停，不会在批准前继续执行后续动作。",
    };
  }
  if (running) {
    return {
      tone: "neutral",
      title: "任务正在运行",
      detail: "RepoPilot 正在分析项目并持续记录可审计证据。",
    };
  }

  const result = (item.verdict ?? item.status).toUpperCase();
  if (result === "PASSED") {
    return {
      tone: "success",
      title: "任务已通过验证",
      detail: "代码 Diff 与声明的验证结果均已生成，可进入审阅。",
    };
  }
  if (result === "FAILED") {
    return {
      tone: "danger",
      title: "任务执行失败",
      detail: "补丁或验证明确失败，请在证据与产物中查看原因。",
    };
  }
  if (result === "BLOCKED") {
    return {
      tone: "danger",
      title: "任务已安全阻断",
      detail: "任务没有继续执行高风险动作，请查看最后一条证据定位原因。",
    };
  }
  if (result === "CANCELLED") {
    return {
      tone: "warning",
      title: "任务已取消",
      detail: "取消请求已生效，任务不会继续执行。",
    };
  }
  if (result === "UNVERIFIED") {
    return {
      tone: "warning",
      title: "结果尚未验证",
      detail: "当前已有分析或计划，但没有足够的补丁与验证证据。",
    };
  }
  return {
    tone: "neutral",
    title: "任务已结束",
    detail: "任务状态已经固化，可查看证据和产物了解完整过程。",
  };
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function readArtifactJson(content: string): Record<string, unknown> | null {
  try {
    return asRecord(JSON.parse(content));
  } catch {
    return null;
  }
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function readStringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function parseUnifiedDiff(content: string): UnifiedDiffLine[] {
  let oldLine: number | null = null;
  let newLine: number | null = null;
  return content.replace(/\r\n/g, "\n").split("\n").map((line) => {
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
    if (hunk) {
      oldLine = Number(hunk[1]);
      newLine = Number(hunk[2]);
      return { kind: "hunk", content: line, oldLine: null, newLine: null };
    }
    if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ") || line.startsWith("index ") || line.startsWith("\\ No newline")) {
      return { kind: "meta", content: line, oldLine: null, newLine: null };
    }
    if (line.startsWith("+")) {
      const result = { kind: "add" as const, content: line, oldLine: null, newLine };
      newLine = newLine === null ? null : newLine + 1;
      return result;
    }
    if (line.startsWith("-")) {
      const result = { kind: "remove" as const, content: line, oldLine, newLine: null };
      oldLine = oldLine === null ? null : oldLine + 1;
      return result;
    }
    if (line.startsWith(" ")) {
      const result = { kind: "context" as const, content: line, oldLine, newLine };
      oldLine = oldLine === null ? null : oldLine + 1;
      newLine = newLine === null ? null : newLine + 1;
      return result;
    }
    return { kind: "meta", content: line, oldLine: null, newLine: null };
  });
}

function ArtifactContent({ kind, content }: { kind: string; content: string }) {
  if (kind === "git_diff") {
    const lines = parseUnifiedDiff(content);
    return (
      <div className="artifact-content diff-view" aria-label="代码变更 Diff">
        {lines.map((line, index) => (
          <div className={"diff-line diff-" + line.kind} key={index}>
            <span>{line.oldLine ?? ""}</span>
            <span>{line.newLine ?? ""}</span>
            <code>{line.content || " "}</code>
          </div>
        ))}
      </div>
    );
  }

  const data = readArtifactJson(content);
  if (kind === "verification" && data) {
    const status = readString(data.status) ?? "UNKNOWN";
    const success = status === "PASSED";
    const reports = readStringList(data.surefire_reports);
    const argv = readStringList(data.argv);
    const stdout = readString(data.stdout_summary);
    const stderr = readString(data.stderr_summary);
    return (
      <div className="artifact-content verification-view">
        <div className={"verification-verdict " + (success ? "passed" : "failed")}>
          {success ? <CheckCircle size={20} weight="fill" /> : <WarningCircle size={20} weight="fill" />}
          <div><strong>{success ? "验证通过" : "验证未通过"}</strong><span>{status}</span></div>
        </div>
        <dl className="artifact-facts">
          <div><dt>Recipe</dt><dd>{readString(data.recipe) ?? "未记录"}</dd></div>
          <div><dt>退出码</dt><dd>{String(data.exit_code ?? "未记录")}</dd></div>
          <div><dt>耗时</dt><dd>{typeof data.duration_ms === "number" ? data.duration_ms.toLocaleString() + " ms" : "未记录"}</dd></div>
          <div><dt>审计代码</dt><dd>{readString(data.code) ?? "未记录"}</dd></div>
        </dl>
        {argv.length > 0 && <code className="artifact-command">{argv.join(" ")}</code>}
        {reports.length > 0 && (
          <section className="artifact-list-section">
            <h3>Surefire 报告</h3>
            <ul>{reports.map((report) => <li key={report}>{report}</li>)}</ul>
          </section>
        )}
        {(stdout || stderr) && (
          <details className="artifact-details">
            <summary>查看截断后的 Maven 输出摘要</summary>
            {stdout && <pre>{stdout}</pre>}
            {stderr && <pre>{stderr}</pre>}
          </details>
        )}
      </div>
    );
  }

  if (kind === "plan_json" && data) {
    const evidence = Array.isArray(data.evidence) ? data.evidence.map(asRecord).filter(Boolean) as Record<string, unknown>[] : [];
    return (
      <div className="artifact-content plan-view">
        <section className="plan-summary">
          <span>问题摘要</span>
          <p>{readString(data.summary) ?? readString(data.problem_summary) ?? "计划未提供问题摘要。"}</p>
        </section>
        <section className="artifact-list-section">
          <h3>候选文件</h3>
          <ul className="path-list">{readStringList(data.candidate_files).map((path) => <li key={path}>{path}</li>)}</ul>
          {readStringList(data.candidate_files).length === 0 && <p>尚未确认可修改文件。</p>}
        </section>
        <section className="artifact-list-section">
          <h3>修改步骤</h3>
          <ol>{readStringList(data.steps).map((step, index) => <li key={index}>{step}</li>)}</ol>
          {readStringList(data.steps).length === 0 && <p>本任务未生成写入步骤。</p>}
        </section>
        <section className="artifact-list-section">
          <h3>验证建议</h3>
          <p>{readStringList(data.verification).join("；") || "未记录额外验证建议。"}</p>
          <code className="artifact-command">{readString(data.verification_recipe) ?? "未指定 Recipe"}</code>
        </section>
        {evidence.length > 0 && (
          <section className="artifact-list-section">
            <h3>来源证据</h3>
            <ul className="path-list">
              {evidence.map((item, index) => {
                const path = readString(item.path) ?? "未知来源";
                const lineStart = typeof item.line_start === "number" ? ":" + item.line_start : "";
                const note = readString(item.note);
                return <li key={path + index}><code>{path + lineStart}</code>{note && <span>{note}</span>}</li>;
              })}
            </ul>
          </section>
        )}
      </div>
    );
  }

  if (kind === "patch_proposal" && data) {
    const changes = Array.isArray(data.changes) ? data.changes.map(asRecord).filter(Boolean) as Record<string, unknown>[] : [];
    return (
      <div className="artifact-content patch-view">
        <section className="plan-summary"><span>补丁摘要</span><p>{readString(data.summary) ?? "补丁提案未提供摘要。"}</p></section>
        <dl className="artifact-facts"><div><dt>Recipe</dt><dd>{readString(data.recipe) ?? "未记录"}</dd></div><div><dt>目标测试</dt><dd>{readString(data.test_class) ?? "未指定"}</dd></div></dl>
        <section className="artifact-list-section"><h3>待修改文件</h3><ul className="path-list">{changes.map((change, index) => <li key={readString(change.path) ?? String(index)}>{readString(change.path) ?? "未命名文件"}</li>)}</ul></section>
      </div>
    );
  }

  if (kind === "telemetry" && data) {
    const model = asRecord(data.model);
    const budget = asRecord(data.budget);
    return (
      <div className="artifact-content telemetry-view">
        <dl className="artifact-facts">
          <div><dt>节点</dt><dd>{String(data.node_count ?? "未记录")}</dd></div>
          <div><dt>总耗时</dt><dd>{typeof data.node_total_duration_ms === "number" ? data.node_total_duration_ms.toLocaleString() + " ms" : "未记录"}</dd></div>
          <div><dt>Token</dt><dd>{typeof model?.total_tokens === "number" ? model.total_tokens.toLocaleString() : "未记录"}</dd></div>
          <div><dt>预算</dt><dd>{readString(budget?.status) ?? "未记录"}</dd></div>
        </dl>
        <pre className="artifact-raw-content">{content}</pre>
      </div>
    );
  }

  return <pre className="artifact-content artifact-raw-content">{content}</pre>;
}

function compactTaskLabel(item: Task): string {
  const title = item.display_title?.trim();
  if (title) return title;
  const identifier = (item.task_id || item.thread_id).replace(/^task-/, "");
  return "未命名任务 · " + identifier.slice(-8);
}

function resolvedTaskOperation(item: Task): Operation {
  const value = item.task_operation ?? item.state?.task_operation;
  return value === "research" ? "research" : "change";
}

export function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [projectPath, setProjectPath] = useState("");
  const [projectName, setProjectName] = useState("");
  const [description, setDescription] = useState("");
  const [mode, setMode] = useState<Mode>("safe-isolated");
  const [operation, setOperation] = useState<Operation>("change");
  const [activeView, setActiveView] = useState<WorkspaceView>("task");
  const [confirmed, setConfirmed] = useState(false);
  const [task, setTask] = useState<Task | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [showArchived, setShowArchived] = useState(false);
  const [showTaskSearch, setShowTaskSearch] = useState(false);
  const [taskQuery, setTaskQuery] = useState("");
  const [events, setEvents] = useState<TimelineEvent[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [selectedArtifact, setSelectedArtifact] = useState("");
  const [artifactVersions, setArtifactVersions] = useState<ArtifactVersion[]>(
    [],
  );
  const [selectedArtifactVersion, setSelectedArtifactVersion] = useState<
    number | null
  >(null);
  const [artifactContent, setArtifactContent] = useState("");
  const [revisionComment, setRevisionComment] = useState("");
  const [requestError, setRequestError] = useState("");
  const [mcpServer, setMcpServer] = useState("");
  const [mcpConfigPath, setMcpConfigPath] = useState(".repopilot/mcp.toml");
  const [mcpRiskApproved, setMcpRiskApproved] = useState(false);
  const [mcpBusy, setMcpBusy] = useState(false);
  const [mcpResult, setMcpResult] = useState<McpProbeResult | null>(null);
  const [approvedMcpTools, setApprovedMcpTools] = useState<string[]>([]);
  const [contextSnapshot, setContextSnapshot] =
    useState<ContextSnapshot | null>(null);
  const [telemetry, setTelemetry] = useState<Telemetry | null>(null);
  const [plugins, setPlugins] = useState<Plugin[]>([]);
  const [pluginSource, setPluginSource] = useState("");
  const [pluginBusy, setPluginBusy] = useState(false);
  const [apiReady, setApiReady] = useState(false);
  const [runtimeHealth, setRuntimeHealth] = useState<RuntimeHealth>({
    status: "UNKNOWN",
    code: "API_NOT_CHECKED",
  });
  const [documentPath, setDocumentPath] = useState("");
  const [documentBusy, setDocumentBusy] = useState(false);
  const [documentResult, setDocumentResult] =
    useState<DocumentIndexResult | null>(null);
  const [documents, setDocuments] = useState<ManagedDocument[]>([]);
  const [projectDiagnosis, setProjectDiagnosis] = useState<ProjectDiagnosis | null>(null);

  async function loadProjects() {
    const response = await fetch(`${API}/projects`);
    if (!response.ok) throw new Error("无法读取项目列表");
    const data = (await response.json()) as { projects?: Project[] };
    const nextProjects = data.projects ?? [];
    setProjects(nextProjects);
    setProjectId((current) => current || nextProjects[0]?.project_id || "");
  }

  async function loadTasks(includeArchived = showArchived) {
    const response = await fetch(
      `${API}/tasks?limit=50&include_archived=${includeArchived}`,
    );
    if (!response.ok) throw new Error("无法读取任务列表");
    const data = (await response.json()) as { tasks?: Task[] };
    setTasks(data.tasks ?? []);
  }

  async function loadPlugins() {
    const response = await fetch(`${API}/plugins`);
    if (!response.ok) throw new Error("无法读取插件目录");
    const data = (await response.json()) as { plugins?: Plugin[] };
    setPlugins(data.plugins ?? []);
  }

  async function loadDocuments(targetProjectId: string) {
    if (!targetProjectId) {
      setDocuments([]);
      return;
    }
    const response = await fetch(
      `${API}/projects/${encodeURIComponent(targetProjectId)}/documents`,
    );
    if (response.status === 405) {
      throw new Error("本机 API 版本已更新，请重启桌面预览服务后重试。");
    }
    if (!response.ok) throw new Error("无法读取已导入研发文档");
    const data = (await response.json()) as { documents?: ManagedDocument[] };
    setDocuments(data.documents ?? []);
  }

  async function loadProjectDiagnosis(targetProjectId: string) {
    if (!targetProjectId) {
      setProjectDiagnosis(null);
      return;
    }
    const response = await fetch(`${API}/projects/${encodeURIComponent(targetProjectId)}/diagnostics`);
    // 旧版本机 API 尚未提供诊断时，保留原有项目元数据作为兼容回退。
    if (response.status === 404) {
      setProjectDiagnosis(null);
      return;
    }
    if (!response.ok) throw new Error("无法读取项目诊断");
    setProjectDiagnosis((await response.json()) as ProjectDiagnosis);
  }

  async function checkApiHealth() {
    try {
      const response = await fetch(`${API}/health`);
      const payload = (await response.json()) as {
        status?: string;
        agent_status?: "READY" | "BLOCKED";
        dependencies?: Array<{
          status?: string;
          code?: string;
          message?: string;
        }>;
      };
      // Desktop workflow needs the runtime dependency contract, not only an HTTP 200.
      const hasCurrentContract =
        typeof payload.agent_status === "string" &&
        Array.isArray(payload.dependencies);
      const ready =
        response.ok && payload.status === "READY" && hasCurrentContract;
      setApiReady(ready);
      const blockedDependency = payload.dependencies?.find(
        (item) => item.status === "BLOCKED",
      );
      setRuntimeHealth({
        status: ready ? payload.agent_status! : "UNKNOWN",
        code:
          blockedDependency?.code ??
          (hasCurrentContract ? "AGENT_RUNTIME_READY" : "API_VERSION_MISMATCH"),
        message:
          blockedDependency?.message ??
          (hasCurrentContract
            ? payload.agent_status === "READY"
              ? "本机服务、模型和检索依赖已就绪。"
              : "Agent 运行依赖未满足，请检查本机服务。"
            : "桌面端与本机 API 版本不兼容，请更新后重试。"),
      });
      if (ready)
        setRequestError((current) =>
          current === API_UNAVAILABLE_MESSAGE ? "" : current,
        );
    } catch {
      setApiReady(false);
      setRuntimeHealth({
        status: "UNKNOWN",
        code: "API_UNAVAILABLE",
        message: "无法连接本机 API，请启动 RepoPilot 后端后重试。",
      });
    }
  }

  useEffect(() => {
    void Promise.all([
      loadProjects(),
      loadTasks(showArchived),
      loadPlugins(),
      checkApiHealth(),
    ]).catch(() => setRequestError(API_UNAVAILABLE_MESSAGE));
  }, [showArchived]);

  useEffect(() => {
    const timer = window.setInterval(() => void checkApiHealth(), 5_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    void loadDocuments(projectId).catch((error) =>
      setRequestError(
        error instanceof Error ? error.message : "无法读取已导入研发文档",
      ),
    );
  }, [projectId]);

  useEffect(() => {
    void loadProjectDiagnosis(projectId).catch((error) =>
      setRequestError(error instanceof Error ? error.message : "无法读取项目诊断"),
    );
  }, [projectId]);

  useEffect(() => {
    if (task || !projectDiagnosis) return;
    const recommendedOperation =
      projectDiagnosis.recommended_task_operation ??
      (projectDiagnosis.task_modes.full_local.code === "FULL_LOCAL_RESEARCH_ONLY"
        ? "research"
        : "change");
    setMode(projectDiagnosis.recommended_task_mode);
    setOperation(recommendedOperation);
    setConfirmed(false);
  }, [projectDiagnosis, task?.thread_id]);

  useEffect(() => {
    if (!task) return;
    const source = new EventSource(`${API}/tasks/${task.thread_id}/events`);
    let completed = false;
    const appendEvent = (event: MessageEvent<string>) => {
      try {
        const payload = JSON.parse(event.data) as Record<string, unknown>;
        const id =
          event.lastEventId || String(payload.event_id ?? crypto.randomUUID());
        setEvents((items) =>
          items.some((item) => item.id === id)
            ? items
            : [
                ...items,
                { id, type: String(payload.type ?? "EVIDENCE"), payload },
              ],
        );
      } catch {
        setEvents((items) => [
          ...items,
          {
            id: crypto.randomUUID(),
            type: "UNPARSEABLE_EVENT",
            payload: { message: event.data },
          },
        ]);
      }
    };
    source.addEventListener("evidence", appendEvent);
    source.addEventListener("state", (event) => {
      appendEvent(event as MessageEvent<string>);
      try {
        const snapshot = JSON.parse(
          (event as MessageEvent<string>).data,
        ) as Pick<Task, "status" | "pending_approval" | "verdict">;
        setTask((current) => (current ? { ...current, ...snapshot } : current));
        if (["REPORT", "BLOCKED", "CANCELLED"].includes(snapshot.status)) {
          completed = true;
          source.close();
        }
      } catch {
        // 事件保留在时间线，任务详情仍会由轮询同步。
      }
    });
    source.addEventListener("error", () => {
      if (completed) return;
      // EventSource 会自动重连；仅在本机 API 也不可用时向用户报告故障。
      void fetch(`${API}/health`)
        .then((response) => {
          if (!response.ok) throw new Error("API_UNAVAILABLE");
          setRequestError((current) =>
            current === EVIDENCE_STREAM_ERROR ? "" : current,
          );
        })
        .catch(() => setRequestError(EVIDENCE_STREAM_ERROR));
    });
    return () => source.close();
  }, [task?.thread_id]);

  useEffect(() => {
    if (task?.pending_approval) setActiveView("task");
  }, [task?.pending_approval]);

  useEffect(() => {
    if (!task) return;
    const threadId = task.thread_id;
    let active = true;
    async function refreshTask() {
      try {
        const response = await fetch(`${API}/tasks/${threadId}`);
        if (!response.ok) return;
        const snapshot = (await response.json()) as Task;
        const artifactResponse = await fetch(
          `${API}/tasks/${threadId}/artifacts`,
        );
        const artifactPayload = artifactResponse.ok
          ? ((await artifactResponse.json()) as { artifacts?: Artifact[] })
          : { artifacts: [] };
        const contextResponse = await fetch(`${API}/tasks/${threadId}/context`);
        const contextPayload = contextResponse.ok
          ? ((await contextResponse.json()) as {
              context_snapshot?: ContextSnapshot;
            })
          : {};
        const telemetryResponse = await fetch(
          `${API}/tasks/${threadId}/telemetry`,
        );
        const telemetryPayload = telemetryResponse.ok
          ? ((await telemetryResponse.json()) as Telemetry)
          : null;
        if (active) {
          setTask((current) => ({ ...current, ...snapshot }));
          setTasks((items) => {
            const existing = items.find(
              (item) => item.thread_id === snapshot.thread_id,
            );
            const merged = { ...existing, ...snapshot };
            return merged.archived_at && !showArchived
              ? items.filter((item) => item.thread_id !== snapshot.thread_id)
              : [
                  merged,
                  ...items.filter(
                    (item) => item.thread_id !== snapshot.thread_id,
                  ),
                ];
          });
          setArtifacts(artifactPayload.artifacts ?? []);
          setContextSnapshot(contextPayload.context_snapshot ?? null);
          setTelemetry(telemetryPayload);
        }
      } catch {
        // SSE 是首选通道；轮询失败不覆盖已显示的证据或产物。
      }
    }
    void refreshTask();
    const timer = window.setInterval(() => void refreshTask(), 2000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [task?.thread_id]);

  useEffect(() => {
    if (!artifacts.length) {
      setSelectedArtifact("");
      setArtifactVersions([]);
      setSelectedArtifactVersion(null);
      setArtifactContent("");
      return;
    }
    if (!artifacts.some((item) => item.kind === selectedArtifact)) {
      const preferred =
        artifacts.find((item) => item.kind === "report") ?? artifacts[0];
      setSelectedArtifact(preferred.kind);
      setSelectedArtifactVersion(null);
    }
  }, [artifacts, selectedArtifact]);

  useEffect(() => {
    if (!task || !selectedArtifact) return;
    const threadId = task.thread_id;
    let active = true;
    async function loadArtifactVersions() {
      try {
        const response = await fetch(
          `${API}/tasks/${threadId}/artifacts/${selectedArtifact}/versions`,
        );
        if (!response.ok) throw new Error("产物版本目录不可读取");
        const payload = (await response.json()) as {
          versions?: ArtifactVersion[];
        };
        const versions = payload.versions ?? [];
        if (active) {
          setArtifactVersions(versions);
          setSelectedArtifactVersion((current) =>
            versions.some((item) => item.version === current)
              ? current
              : (versions[0]?.version ?? null),
          );
        }
      } catch (error) {
        if (active) {
          setArtifactVersions([]);
          setSelectedArtifactVersion(null);
          setArtifactContent(
            error instanceof Error ? error.message : "产物版本目录读取失败",
          );
        }
      }
    }
    void loadArtifactVersions();
    return () => {
      active = false;
    };
  }, [task?.thread_id, selectedArtifact]);

  useEffect(() => {
    if (!task || !selectedArtifact) return;
    const threadId = task.thread_id;
    let active = true;
    async function loadArtifact() {
      try {
        const suffix =
          selectedArtifactVersion === null
            ? ""
            : `/versions/${selectedArtifactVersion}`;
        const response = await fetch(
          `${API}/tasks/${threadId}/artifacts/${selectedArtifact}${suffix}`,
        );
        if (!response.ok) throw new Error("产物不可读取或完整性校验失败");
        const payload = (await response.json()) as { content: string };
        if (active) setArtifactContent(payload.content);
      } catch (error) {
        if (active)
          setArtifactContent(
            error instanceof Error ? error.message : "产物读取失败",
          );
      }
    }
    void loadArtifact();
    return () => {
      active = false;
    };
  }, [task?.thread_id, selectedArtifact, selectedArtifactVersion]);

  async function addProject() {
    if (!projectPath.trim()) return;
    setRequestError("");
    try {
      const response = await fetch(
        `${API}/projects?path=${encodeURIComponent(projectPath)}&name=${encodeURIComponent(projectName)}`,
        { method: "POST" },
      );
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "项目注册失败");
      const project = payload.project as Project;
      setProjectId(project.project_id);
      setProjectPath("");
      setProjectName("");
      await loadProjects();
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "项目注册失败");
    }
  }

  async function installPlugin() {
    if (!pluginSource.trim()) return;
    setPluginBusy(true);
    setRequestError("");
    try {
      const response = await fetch(`${API}/plugins`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source: pluginSource.trim() }),
      });
      const payload = (await response.json()) as {
        detail?: string | { message?: string };
      };
      if (!response.ok)
        throw new Error(
          typeof payload.detail === "string"
            ? payload.detail
            : (payload.detail?.message ?? "插件安装失败"),
        );
      setPluginSource("");
      await loadPlugins();
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "插件安装失败");
    } finally {
      setPluginBusy(false);
    }
  }

  async function setPluginEnabled(plugin: Plugin, enabled: boolean) {
    setPluginBusy(true);
    setRequestError("");
    try {
      const response = await fetch(
        `${API}/plugins/${encodeURIComponent(plugin.plugin_id)}/enabled`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled }),
        },
      );
      const payload = (await response.json()) as {
        detail?: string | { message?: string };
      };
      if (!response.ok)
        throw new Error(
          typeof payload.detail === "string"
            ? payload.detail
            : (payload.detail?.message ?? "插件状态更新失败"),
        );
      await loadPlugins();
    } catch (error) {
      setRequestError(
        error instanceof Error ? error.message : "插件状态更新失败",
      );
    } finally {
      setPluginBusy(false);
    }
  }

  async function chooseProjectDirectory() {
    try {
      const selected = await open({
        directory: true,
        multiple: false,
        title: "选择 RepoPilot 项目目录",
      });
      if (typeof selected === "string") setProjectPath(selected);
    } catch {
      setRequestError(
        "系统目录选择器仅在已安装的 RepoPilot Desktop 中可用；浏览器调试时请手动输入路径。",
      );
    }
  }

  async function chooseDocument() {
    try {
      const selected = await open({
        multiple: false,
        title: "选择研发文档",
        filters: [{ name: "Markdown 或文本", extensions: ["md", "txt"] }],
      });
      if (typeof selected === "string") setDocumentPath(selected);
    } catch {
      setRequestError(
        "系统文件选择器仅在已安装的 RepoPilot Desktop 中可用；浏览器调试时请手动输入 MD/TXT 路径。",
      );
    }
  }

  async function indexDocument() {
    if (!projectId || !documentPath.trim()) return;
    setDocumentBusy(true);
    setDocumentResult(null);
    setRequestError("");
    try {
      const response = await fetch(
        `${API}/projects/${encodeURIComponent(projectId)}/documents`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ file: documentPath.trim() }),
        },
      );
      const raw = (await response.json()) as DocumentIndexResult & {
        detail?: DocumentIndexResult | string;
      };
      const payload =
        typeof raw.detail === "object" && raw.detail ? raw.detail : raw;
      if (!response.ok)
        throw new Error(
          payload.message ??
            payload.code ??
            (typeof raw.detail === "string" ? raw.detail : "文档索引失败"),
        );
      setDocumentResult(payload);
      setDocumentPath("");
      await loadDocuments(projectId);
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "文档索引失败");
    } finally {
      setDocumentBusy(false);
    }
  }

  async function start() {
    if (
      !projectId ||
      !description.trim() ||
      (mode === "full-local" && !confirmed)
    )
      return;
    if (
      mode === "safe-isolated" &&
      currentProject &&
      !currentProject.is_git_repository
    ) {
      setRequestError(
        "当前项目不是 Git 仓库，无法创建隔离 Worktree。请先初始化 Git，或明确切换到完全本机控制模式。",
      );
      return;
    }
    if (!operationAllowed) {
      setRequestError(taskAdmissionMessage);
      return;
    }
    setEvents([]);
    setArtifacts([]);
    setArtifactVersions([]);
    setSelectedArtifactVersion(null);
    setArtifactContent("");
    setContextSnapshot(null);
    setTelemetry(null);
    setRequestError("");
    try {
      const response = await fetch(`${API}/tasks`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project_id: projectId,
          description,
          task_mode: mode,
          operation,
          confirmation: confirmed ? "我已了解完全权限风险" : null,
          approved_mcp_tools: approvedMcpTools,
        }),
      });
      const payload = await response.json();
      if (!response.ok) {
        const detail = payload.detail;
        throw new Error(
          typeof detail === "string"
            ? detail
            : (detail?.message ?? "任务创建失败"),
        );
      }
      setTask(payload as Task);
      setActiveView("task");
      await loadTasks();
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "任务创建失败");
    }
  }

  async function probeMcp() {
    if (
      !projectId ||
      !mcpServer.trim() ||
      (mode === "full-local" && !confirmed)
    )
      return;
    setMcpBusy(true);
    setMcpResult(null);
    setRequestError("");
    try {
      const response = await fetch(`${API}/projects/${projectId}/mcp/probe`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          server: mcpServer.trim(),
          config_path: mcpConfigPath.trim(),
          task_mode: mode,
          confirmation:
            mode === "full-local" && confirmed ? "我已了解完全权限风险" : null,
          approve_risk: mcpRiskApproved,
        }),
      });
      const raw = (await response.json()) as {
        detail?: McpProbeResult;
      } & McpProbeResult;
      const payload = raw.detail ?? raw;
      setMcpResult(payload);
      setApprovedMcpTools([]);
      if (!response.ok) setRequestError(`MCP 未连接：${payload.code}`);
    } catch {
      setRequestError("MCP 探测失败，请确认本机 API、配置路径和 Server 名称。");
    } finally {
      setMcpBusy(false);
    }
  }

  async function approve(decision: "approve" | "revise" | "reject") {
    if (!task) return;
    const response = await fetch(`${API}/tasks/${task.thread_id}/approval`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        decision,
        comment: decision === "revise" ? revisionComment : null,
      }),
    });
    const payload = await response.json();
    if (!response.ok) {
      setRequestError(payload.detail ?? "审批提交失败");
      return;
    }
    if (decision === "revise") setRevisionComment("");
    setTask(payload as Task);
  }

  async function cancelTask() {
    if (!task) return;
    try {
      const response = await fetch(`${API}/tasks/${task.thread_id}/cancel`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: "用户从桌面端请求取消任务。" }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "取消请求失败");
      setTask(payload as Task);
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "取消请求失败");
    }
  }

  async function archiveTask() {
    if (!task) return;
    try {
      const response = await fetch(`${API}/tasks/${task.thread_id}`, {
        method: "DELETE",
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "任务归档失败");
      setTask(payload.task as Task);
      await loadTasks(showArchived);
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "任务归档失败");
    }
  }

  function beginNewTask() {
    setTask(null);
    setEvents([]);
    setArtifacts([]);
    setArtifactVersions([]);
    setSelectedArtifact("");
    setSelectedArtifactVersion(null);
    setArtifactContent("");
    setContextSnapshot(null);
    setTelemetry(null);
    setDescription("");
    setMode("safe-isolated");
    setOperation("change");
    setConfirmed(false);
    setRevisionComment("");
    setRequestError("");
    setActiveView("task");
  }

  async function selectTask(selected: Task) {
    setRequestError("");
    setEvents([]);
    try {
      const response = await fetch(`${API}/tasks/${selected.thread_id}`);
      if (!response.ok) throw new Error("任务详情不可恢复");
      const snapshot = (await response.json()) as Task;
      const merged = { ...selected, ...snapshot };
      setTask(merged);
      setProjectId(merged.project_id ?? projectId);
      if (merged.task_mode === "safe-isolated" || merged.task_mode === "full-local") {
        setMode(merged.task_mode);
        setConfirmed(merged.task_mode === "full-local");
      }
      setOperation(resolvedTaskOperation(merged));
      setActiveView("task");
    } catch (error) {
      setRequestError(
        error instanceof Error ? error.message : "任务详情不可恢复",
      );
    }
  }

  const interrupt = task?.interrupts?.[0];
  const currentProject = projects.find((item) => item.project_id === projectId);
  const safeModeReadiness = projectDiagnosis?.task_modes.safe_isolated;
  const selectedModeReadiness =
    mode === "safe-isolated"
      ? projectDiagnosis?.task_modes.safe_isolated
      : projectDiagnosis?.task_modes.full_local;
  const allowedOperations: Operation[] =
    selectedModeReadiness?.allowed_operations ??
    (currentProject
      ? mode === "safe-isolated"
        ? currentProject.is_git_repository
          ? ["change", "research"]
          : []
        : currentProject.is_git_repository
          ? ["change", "research"]
          : ["research"]
      : []);
  const operationAllowed = allowedOperations.includes(operation);
  const taskAdmissionMessage = operationAllowed
    ? ""
    : mode === "full-local" && operation === "change"
      ? "当前项目不是 Git 仓库，无法生成可信基线和 Diff；请使用仅研究，或先初始化 Git 并创建提交。"
      : (selectedModeReadiness?.message ?? "当前项目不支持所选任务类型。");
  const safeModeBlockedByProject = Boolean(
    currentProject && (safeModeReadiness ? safeModeReadiness.status !== "READY" : !currentProject.is_git_repository),
  );
  const safeModeWarningMessage = safeModeReadiness?.message ?? "当前项目尚未初始化 Git，不能创建隔离 Worktree。初始化 Git 后可使用安全隔离修复。";
  const projectStatusLabel = projectDiagnosis
    ? projectDiagnosis.task_modes.safe_isolated.status === "READY"
      ? "隔离修复可用"
      : projectDiagnosis.task_modes.full_local.code === "FULL_LOCAL_RESEARCH_ONLY"
        ? "仅完整本机研究"
        : projectDiagnosis.task_modes.safe_isolated.code
    : currentProject
      ? currentProject.is_git_repository
        ? "Git 基线待诊断"
        : "非 Git 项目"
      : "等待选择项目";
  const taskIsRunning = Boolean(
    task &&
      !["REPORT", "FAILED", "PASSED", "BLOCKED", "CANCELLED", "UNVERIFIED"].includes(
        task.status,
      ),
  );
  const taskOutcome = task ? resolveTaskOutcome(task, taskIsRunning) : null;
  const taskStatus =
    task?.status ?? (!apiReady ? "OFFLINE" : runtimeHealth.status);
  const serviceStatus = !apiReady
    ? "offline"
    : runtimeHealth.status === "READY"
      ? "ready"
      : "degraded";
  const visibleEvents = events.slice(-14);
  const activeTaskOperation = task ? resolvedTaskOperation(task) : operation;
  const activeTaskMode: Mode =
    task?.task_mode === "full-local" ? "full-local" : "safe-isolated";
  const displayedTaskDescription =
    task?.task_description ??
    task?.state?.task_description ??
    (task ? task.display_title : description.trim());
  const researchPlanApproval = Boolean(
    task?.pending_approval &&
      activeTaskOperation === "research" &&
      interrupt?.type === "PLAN_APPROVAL_REQUIRED",
  );
  const canStart =
    !task &&
    Boolean(projectId && description.trim()) &&
    runtimeHealth.status === "READY" &&
    operationAllowed &&
    !(safeModeBlockedByProject && mode === "safe-isolated") &&
    !(mode === "full-local" && !confirmed);

  return (
    <main className="product-shell">
      <aside className="navigation-pane">
        <div className="product-brand">
          <span className="brand-menu">
            <strong>RepoPilot</strong>
            <CaretDown size={14} />
          </span>
          <button
            className={showTaskSearch ? "icon-button active" : "icon-button"}
            type="button"
            title="搜索任务"
            aria-label="搜索任务"
            onClick={() => setShowTaskSearch((current) => !current)}
          >
            <MagnifyingGlass size={18} />
          </button>
        </div>

        <nav className="primary-navigation" aria-label="产品导航">
          <button
            className={activeView === "task" && !task ? "active" : ""}
            type="button"
            onClick={beginNewTask}
          >
            <ChatCircle size={18} />
            新建任务
          </button>
          <button
            className={activeView === "review" ? "active" : ""}
            type="button"
            onClick={() => setActiveView("review")}
            disabled={!task}
          >
            <ListMagnifyingGlass size={18} />
            任务审阅
            {task?.pending_approval && <span className="nav-notice" />}
          </button>
          <button
            className={activeView === "context" ? "active" : ""}
            type="button"
            onClick={() => setActiveView("context")}
          >
            <PuzzlePiece size={18} />
            上下文与扩展
            <small>{plugins.filter((item) => item.active).length}</small>
          </button>
        </nav>

        {showTaskSearch && (
          <div className="task-search">
            <MagnifyingGlass size={15} />
            <input
              value={taskQuery}
              onChange={(event) => setTaskQuery(event.target.value)}
              placeholder="搜索任务"
              autoFocus
              aria-label="搜索任务"
            />
          </div>
        )}

        <section className="project-navigation">
          <div className="navigation-heading">
            <span>项目</span>
            <button
              className="icon-button"
              type="button"
              title="选择本地项目目录"
              onClick={() => void chooseProjectDirectory()}
            >
              <FolderOpen size={17} />
            </button>
          </div>
          <div className="project-tree">
            {projects.length === 0 && <p className="sidebar-empty">尚未注册本地项目</p>}
            {projects.map((project) => {
              const projectTasks = tasks.filter(
                (item) =>
                  item.project_id === project.project_id &&
                  (!taskQuery.trim() ||
                    compactTaskLabel(item)
                      .toLocaleLowerCase()
                      .includes(taskQuery.trim().toLocaleLowerCase())),
              );
              const selected = project.project_id === projectId;
              return (
                <section className="project-node" key={project.project_id}>
                  <button
                    className={selected ? "project-row selected" : "project-row"}
                    type="button"
                    onClick={() => {
                      setProjectId(project.project_id);
                      setApprovedMcpTools([]);
                      setConfirmed(false);
                    }}
                  >
                    <FolderOpen size={16} weight={selected ? "fill" : "regular"} />
                    <span>{project.display_name}</span>
                    <small>{project.is_git_repository ? "Git" : "非 Git"}</small>
                  </button>
                  {selected && (
                    <div className="project-task-list">
                      {projectTasks.length === 0 && <p>还没有任务记录</p>}
                      {projectTasks.map((item) => (
                        <button
                          className={task?.thread_id === item.thread_id ? "active" : ""}
                          type="button"
                          key={item.thread_id}
                          onClick={() => void selectTask(item)}
                        >
                          <span>{compactTaskLabel(item)}</span>
                          <i className={"task-dot status-" + item.status.toLowerCase()} />
                        </button>
                      ))}
                    </div>
                  )}
                </section>
              );
            })}
          </div>
          <details className="project-adder" open={Boolean(projectPath)}>
            <summary>
              <Plus size={16} />
              注册本地项目
            </summary>
            <div className="sidebar-form">
              <label>
                项目目录
                <div className="input-with-action">
                  <input
                    value={projectPath}
                    onChange={(event) => setProjectPath(event.target.value)}
                    placeholder="D:\\code\\my-project"
                  />
                  <button className="icon-button" type="button" title="选择目录" onClick={() => void chooseProjectDirectory()}>
                    <FolderOpen size={16} />
                  </button>
                </div>
              </label>
              <label>
                显示名称
                <input value={projectName} onChange={(event) => setProjectName(event.target.value)} placeholder="可选" />
              </label>
              <button className="secondary-button" type="button" onClick={() => void addProject()} disabled={!projectPath.trim()}>
                注册项目
              </button>
            </div>
          </details>
        </section>

        <div className="navigation-footer">
          <label className="archive-filter">
            <input type="checkbox" checked={showArchived} onChange={(event) => setShowArchived(event.target.checked)} />
            显示归档
          </label>
          <div className={"runtime-indicator runtime-" + serviceStatus}>
            <span />
            <div>
              <b>{serviceStatus === "ready" ? "Agent 已就绪" : "Agent 未就绪"}</b>
              <small>{runtimeHealth.code}</small>
            </div>
            <button className="icon-button" type="button" title="重新检测" onClick={() => void checkApiHealth()}>
              <ArrowClockwise size={15} />
            </button>
          </div>
        </div>
      </aside>

      <section className="workbench">
        <header className="workspace-header">
          <div className="workspace-identity">
            <FolderOpen size={18} />
            <strong>{currentProject?.display_name ?? "选择本地项目"}</strong>
            {task && (
              <>
                <span>/</span>
                <small>{compactTaskLabel(task)}</small>
              </>
            )}
          </div>
          <div className="workspace-actions" aria-label="工作区视图">
            <button className={activeView === "task" ? "active" : ""} type="button" title="Agent 会话" onClick={() => setActiveView("task")}>
              <ChatCircle size={18} />
            </button>
            <button className={activeView === "context" ? "active" : ""} type="button" title="上下文与扩展" onClick={() => setActiveView("context")}>
              <Stack size={18} />
            </button>
            <button className={activeView === "review" ? "active" : ""} type="button" title="证据与产物" onClick={() => setActiveView("review")} disabled={!task}>
              <FileCode size={18} />
            </button>
          </div>
        </header>

        {activeView === "task" && (
          <section className="session-view">
            <div className="conversation-scroll">
              <div className="conversation-column">
                {!task && (
                  <div className="new-task-state">
                    <p className="new-task-kicker">当前工作区</p>
                    <h2>{operation === "research" ? "研究当前代码库" : "准备开始代码任务"}</h2>
                    <p>
                      {currentProject
                        ? currentProject.display_name + "  ·  " + projectStatusLabel
                        : "从左侧选择或注册一个本地项目"}
                    </p>
                  </div>
                )}

                {task && (
                  <>
                    <article className="task-request">
                      <header>
                        <strong>任务</strong>
                        <div className="task-metadata">
                          <span>{activeTaskOperation === "research" ? "仅研究" : "修改代码"}</span>
                          <span>{activeTaskMode === "safe-isolated" ? "安全隔离修复" : "完全本机控制"}</span>
                        </div>
                      </header>
                      <p>{displayedTaskDescription || "继续任务 " + compactTaskLabel(task)}</p>
                    </article>
                    <article className="execution-record">
                      <div className="agent-response">
                        <div className="agent-response-header">
                          <span className="agent-mark"><TerminalWindow size={15} weight="bold" /></span>
                          <strong>RepoPilot</strong>
                          <span className={"state-chip state-" + taskStatus.toLowerCase()}>
                            {task.pending_approval ? "等待审批" : taskStatus}
                          </span>
                        </div>
                        {taskOutcome && (
                          <div className={"task-outcome outcome-" + taskOutcome.tone} aria-live="polite">
                            <span>
                              {taskOutcome.tone === "success"
                                ? <CheckCircle size={18} weight="fill" />
                                : taskOutcome.tone === "neutral"
                                  ? <CircleNotch className={taskIsRunning ? "spin" : ""} size={18} />
                                  : <WarningCircle size={18} weight="fill" />}
                            </span>
                            <div>
                              <strong>{taskOutcome.title}</strong>
                              <p>{taskOutcome.detail}</p>
                            </div>
                          </div>
                        )}
                        {visibleEvents.length === 0 && taskIsRunning && (
                          <div className="activity-loading" aria-label="任务正在初始化"><span /><span /><span /></div>
                        )}
                        <div className="agent-activity">
                          {visibleEvents.map((event, index) => (
                            <article key={event.id}>
                              <span className="activity-line">
                                {index === visibleEvents.length - 1 && taskIsRunning
                                  ? <CircleNotch className="spin" size={15} />
                                  : <CheckCircle size={15} weight="fill" />}
                              </span>
                              <div>
                                <b>{eventLabels[event.type] ?? event.type}</b>
                                <p>{eventSummary(event)}</p>
                              </div>
                            </article>
                          ))}
                        </div>
                        {artifacts.length > 0 && (
                          <div className="result-strip">
                            <div>
                              <FileCode size={18} />
                              <span>
                                已生成 {artifacts.length} 份可审计产物
                                <small>{task.verdict ?? "等待最终验证"}</small>
                              </span>
                            </div>
                            <button type="button" onClick={() => setActiveView("review")}>
                              打开审阅 <ArrowRight size={15} />
                            </button>
                          </div>
                        )}
                        {!taskIsRunning && artifacts.length === 0 && (
                          <p className="agent-message">任务已停止，当前没有可供审阅的产物。</p>
                        )}
                      </div>
                    </article>
                  </>
                )}

                {task?.pending_approval && (
                  <section className="inline-approval">
                    <div className="approval-heading">
                      <WarningCircle size={20} weight="fill" />
                      <div>
                        <strong>
                          {researchPlanApproval
                            ? "研究结论等待确认"
                            : interrupt?.type === "EXECUTION_APPROVAL_REQUIRED"
                            ? "执行前需要你的批准"
                            : "修改计划等待审阅"}
                        </strong>
                        <p>{interrupt?.message ?? "审阅计划后决定是否继续。"}</p>
                      </div>
                    </div>
                    {interrupt?.type === "PLAN_APPROVAL_REQUIRED" && (
                      <textarea value={revisionComment} onChange={(event) => setRevisionComment(event.target.value)} placeholder="填写需要调整的地方" aria-label="计划修改意见" />
                    )}
                    <div className="approval-buttons">
                      {interrupt?.type === "PLAN_APPROVAL_REQUIRED" && (
                        <button className="secondary-button" type="button" onClick={() => void approve("revise")} disabled={!revisionComment.trim()}>
                          <ArrowClockwise size={16} />要求调整
                        </button>
                      )}
                      <button className="primary-button" type="button" onClick={() => void approve("approve")}>
                        <CheckCircle size={16} weight="bold" />
                        {researchPlanApproval ? "确认并生成报告" : "批准继续"}
                      </button>
                      <button className="danger-button" type="button" onClick={() => void approve("reject")}>
                        <XCircle size={16} />拒绝
                      </button>
                    </div>
                  </section>
                )}
              </div>
            </div>

            <div className="composer-region">
              {task ? (
                <div className={"task-command-bar outcome-" + (taskOutcome?.tone ?? "neutral")}>
                  <div className="task-command-status">
                    {taskOutcome?.tone === "success"
                      ? <CheckCircle size={19} weight="fill" />
                      : taskOutcome?.tone === "neutral"
                        ? <CircleNotch className={taskIsRunning ? "spin" : ""} size={19} />
                        : <WarningCircle size={19} weight="fill" />}
                    <div>
                      <strong>{taskOutcome?.title ?? "任务状态已更新"}</strong>
                      <span>{task.pending_approval ? "请在上方完成审批" : task.verdict ?? task.status}</span>
                    </div>
                  </div>
                  <div className="task-command-actions">
                    {artifacts.length > 0 && (
                      <button className="secondary-button" type="button" onClick={() => setActiveView("review")}>
                        <ListMagnifyingGlass size={16} />审阅产物
                      </button>
                    )}
                    {taskIsRunning && (
                      <button className="danger-button" type="button" onClick={() => void cancelTask()}>
                        <XCircle size={16} />停止任务
                      </button>
                    )}
                    {!taskIsRunning && !task.archived_at && (
                      <button className="secondary-button" type="button" onClick={() => void archiveTask()}>
                        <Archive size={16} />归档
                      </button>
                    )}
                    {!taskIsRunning && (
                      <button className="primary-button" type="button" onClick={beginNewTask}>
                        <Plus size={16} />新建任务
                      </button>
                    )}
                  </div>
                </div>
              ) : (
                <>
                  <div className="composer">
                    {(requestError ||
                      (apiReady && runtimeHealth.status !== "READY") ||
                      (Boolean(currentProject) && !operationAllowed) ||
                      (mode === "safe-isolated" && safeModeBlockedByProject)) && (
                      <div className="composer-error">
                        <WarningCircle size={16} />
                        <span>
                          {requestError ||
                            (runtimeHealth.status !== "READY"
                              ? runtimeHealth.status === "BLOCKED"
                                ? "Agent 运行依赖未就绪：" +
                                  (runtimeHealth.message ?? runtimeHealth.code) +
                                  "（" +
                                  runtimeHealth.code +
                                  "）"
                                : "本机 Agent API 版本需要更新"
                              : !operationAllowed
                                ? taskAdmissionMessage
                                : safeModeWarningMessage)}
                        </span>
                      </div>
                    )}
                    {mode === "full-local" && !confirmed && (
                      <label className="full-access-confirmation">
                        <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
                        <span><b>确认完全本机访问</b>Agent 将直接在当前项目目录中执行已实现的高风险操作。</span>
                      </label>
                    )}
                    {(documentPath || documentResult) && (
                      <div className="attachment-row">
                        <FileArrowUp size={16} />
                        <span>{documentPath || documentResult?.document?.display_name || "研发文档"}</span>
                        {documentPath && (
                          <button type="button" onClick={() => void indexDocument()} disabled={documentBusy || !projectId}>
                            {documentBusy ? "正在索引" : "加入上下文"}
                          </button>
                        )}
                      </div>
                    )}
                    <textarea
                      value={description}
                      onChange={(event) => setDescription(event.target.value)}
                      onKeyDown={(event) => {
                        if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && canStart) {
                          event.preventDefault();
                          void start();
                        }
                      }}
                      placeholder={operation === "research"
                        ? "描述要理解、定位或评估的代码问题"
                        : "描述要完成的代码改动"}
                      aria-label="代码任务描述"
                    />
                    <div className="composer-toolbar">
                      <div className="composer-tools">
                        <button className="icon-button" type="button" title="添加 MD 或 TXT 研发文档" onClick={() => void chooseDocument()} disabled={!projectId}>
                          <Paperclip size={19} />
                        </button>
                        <div className="operation-control" role="group" aria-label="任务类型">
                          <button
                            className={operation === "change" ? "active" : ""}
                            type="button"
                            disabled={!allowedOperations.includes("change")}
                            onClick={() => setOperation("change")}
                            aria-label="修改代码"
                            aria-pressed={operation === "change"}
                            title="生成计划，经审批后修改代码并运行验证"
                          >
                            <FileCode size={15} />
                            <span>修改代码</span>
                          </button>
                          <button
                            className={operation === "research" ? "active" : ""}
                            type="button"
                            disabled={!allowedOperations.includes("research")}
                            onClick={() => setOperation("research")}
                            aria-label="仅研究"
                            aria-pressed={operation === "research"}
                            title="只研究代码并输出证据化计划，不写入文件"
                          >
                            <ListMagnifyingGlass size={15} />
                            <span>仅研究</span>
                          </button>
                        </div>
                        <label className={"permission-control mode-" + mode}>
                          {mode === "safe-isolated" ? <ShieldCheck size={17} /> : <WarningCircle size={17} />}
                          <select
                            value={mode}
                            aria-label="任务权限模式"
                            onChange={(event) => {
                              const nextMode = event.target.value as Mode;
                              const readiness = nextMode === "safe-isolated"
                                ? projectDiagnosis?.task_modes.safe_isolated
                                : projectDiagnosis?.task_modes.full_local;
                              const nextAllowed = readiness?.allowed_operations;
                              setMode(nextMode);
                              if (nextAllowed?.length && !nextAllowed.includes(operation)) {
                                setOperation(nextAllowed[0]);
                              }
                              if (nextMode === "safe-isolated") setConfirmed(false);
                            }}
                          >
                            <option value="safe-isolated" disabled={Boolean(safeModeReadiness && safeModeReadiness.status !== "READY")}>
                              安全隔离
                            </option>
                            <option value="full-local">完全本机</option>
                          </select>
                        </label>
                      </div>
                      <button className="send-button" type="button" title="开始任务（Ctrl + Enter）" onClick={() => void start()} disabled={!canStart}>
                        <ArrowUp size={19} weight="bold" />
                      </button>
                    </div>
                  </div>
                  <p className="composer-caption">{currentProject ? projectStatusLabel : "选择项目后即可创建任务"}</p>
                </>
              )}
            </div>
          </section>
        )}

        {activeView === "context" && (
          <section className="utility-view">
            <header className="utility-header">
              <div><h2>上下文与扩展</h2><p>{currentProject?.display_name ?? "尚未选择项目"}</p></div>
              <span>{contextSnapshot ? "已冻结任务快照" : "项目级配置"}</span>
            </header>

            <section className="settings-section">
              <div className="settings-title">
                <FileArrowUp size={19} />
                <div><h3>研发文档</h3><p>MD / TXT · {documents.length} 份已索引文档</p></div>
              </div>
              <div className="settings-content">
                <div className="inline-form">
                  <input value={documentPath} onChange={(event) => setDocumentPath(event.target.value)} placeholder="本地文档路径" />
                  <button className="icon-button" type="button" title="选择文档" onClick={() => void chooseDocument()}><FolderOpen size={17} /></button>
                  <button className="secondary-button" type="button" onClick={() => void indexDocument()} disabled={!projectId || !documentPath.trim() || documentBusy}>
                    {documentBusy ? "索引中" : "添加"}
                  </button>
                </div>
                <div className="document-list">
                  {documents.length === 0 ? <p>暂无项目文档</p> : documents.map((document) => (
                    <span key={document.document_id}><FileCode size={15} />{document.display_name}</span>
                  ))}
                </div>
              </div>
            </section>

            <section className="settings-section">
              <div className="settings-title">
                <PuzzlePiece size={19} />
                <div><h3>MCP 工具</h3><p>连接状态：{mcpResult?.status ?? "未探测"}</p></div>
              </div>
              <div className="settings-content">
                <div className="mcp-form-grid">
                  <label>配置路径<input value={mcpConfigPath} onChange={(event) => setMcpConfigPath(event.target.value)} /></label>
                  <label>Server<input value={mcpServer} onChange={(event) => setMcpServer(event.target.value)} placeholder="engineering-docs" /></label>
                </div>
                <label className="checkbox-row">
                  <input type="checkbox" checked={mcpRiskApproved} onChange={(event) => setMcpRiskApproved(event.target.checked)} />
                  批准本次 MCP 网络或写入风险
                </label>
                <button className="secondary-button" type="button" onClick={() => void probeMcp()} disabled={mcpBusy || !projectId || !mcpServer.trim()}>
                  {mcpBusy ? "正在握手" : "探测服务"}
                </button>
                {mcpResult && (
                  <div className="tool-directory">
                    {(mcpResult.connection?.tools ?? []).map((tool) => (
                      <label key={tool.capability_id}>
                        <input
                          type="checkbox"
                          checked={approvedMcpTools.includes(tool.capability_id)}
                          onChange={(event) => setApprovedMcpTools((current) =>
                            event.target.checked
                              ? [...new Set([...current, tool.capability_id])]
                              : current.filter((item) => item !== tool.capability_id)
                          )}
                        />
                        <span><b>{tool.capability_id}</b><small>{tool.description}</small></span>
                      </label>
                    ))}
                  </div>
                )}
              </div>
            </section>

            <section className="settings-section">
              <div className="settings-title">
                <SlidersHorizontal size={19} />
                <div><h3>Skills 与插件</h3><p>{plugins.filter((item) => item.active).length} 个活动插件</p></div>
              </div>
              <div className="settings-content">
                <div className="inline-form">
                  <input value={pluginSource} onChange={(event) => setPluginSource(event.target.value)} placeholder="本地插件目录" />
                  <button className="secondary-button" type="button" onClick={() => void installPlugin()} disabled={!pluginSource.trim() || pluginBusy}>安装</button>
                </div>
                <div className="plugin-list-clean">
                  {plugins.length === 0 ? <p>尚未安装插件</p> : plugins.map((plugin) => (
                    <label key={plugin.plugin_id}>
                      <span><b>{plugin.manifest.name}</b><small>{plugin.manifest.description}</small></span>
                      <input type="checkbox" checked={plugin.enabled} disabled={pluginBusy} onChange={(event) => void setPluginEnabled(plugin, event.target.checked)} />
                    </label>
                  ))}
                </div>
              </div>
            </section>

            {(contextSnapshot || telemetry) && (
              <section className="settings-section">
                <div className="settings-title">
                  <Stack size={19} />
                  <div><h3>任务上下文快照</h3><p>{contextSnapshot ? contextSnapshot.sources.length + " 个来源" : "暂无来源"}</p></div>
                </div>
                <div className="settings-content snapshot-content">
                  {contextSnapshot && (
                    <>
                      <code>{contextSnapshot.snapshot_sha256}</code>
                      <div className="source-list">
                        {contextSnapshot.sources.map((source, index) => (
                          <span key={source.path + index}>{source.path}{source.line_start ? ":" + source.line_start : ""}</span>
                        ))}
                      </div>
                    </>
                  )}
                  {telemetry && (
                    <div className="telemetry-row">
                      <span>{telemetry.node_count} nodes</span>
                      <span>{telemetry.model.total_tokens.toLocaleString()} tokens</span>
                      <span>{telemetry.node_total_duration_ms} ms</span>
                    </div>
                  )}
                </div>
              </section>
            )}
          </section>
        )}

        {activeView === "review" && (
          <section className="review-view">
            <aside className="artifact-navigation">
              <div className="review-pane-heading"><span>任务产物</span><b>{artifacts.length}</b></div>
              {artifacts.length === 0 && <p>暂无可审阅产物</p>}
              {artifacts.map((artifact) => (
                <button
                  className={selectedArtifact === artifact.kind ? "active" : ""}
                  type="button"
                  key={artifact.kind}
                  onClick={() => {
                    setSelectedArtifact(artifact.kind);
                    setSelectedArtifactVersion(null);
                  }}
                >
                  <FileCode size={16} />
                  <span>{artifactLabels[artifact.kind] ?? artifact.kind}<small>{artifact.size_bytes} B</small></span>
                </button>
              ))}
            </aside>
            <article className="artifact-reader">
              <header>
                <div>
                  <h2>{selectedArtifact ? artifactLabels[selectedArtifact] ?? selectedArtifact : "选择任务产物"}</h2>
                  <p>{task ? compactTaskLabel(task) + " · " + (task.verdict ?? task.status) : "尚未选择任务"}</p>
                </div>
                {artifactVersions.length > 0 && (
                  <select value={selectedArtifactVersion ?? ""} onChange={(event) => setSelectedArtifactVersion(Number(event.target.value))} aria-label="产物版本">
                    {artifactVersions.map((version) => (
                      <option key={version.version} value={version.version}>
                        v{version.version} · {version.created_at.slice(0, 19).replace("T", " ")}
                      </option>
                    ))}
                  </select>
                )}
              </header>
              {selectedArtifact ? (
                <>
                  <p className="artifact-hash">
                    {artifactVersions.find((version) => version.version === selectedArtifactVersion)?.sha256 ??
                      artifacts.find((artifact) => artifact.kind === selectedArtifact)?.sha256}
                  </p>
                  <ArtifactContent kind={selectedArtifact} content={artifactContent} />
                </>
              ) : (
                <div className="reader-empty"><FileCode size={24} /><span>从左侧选择报告、计划、Diff 或验证结果</span></div>
              )}
            </article>
            <aside className="evidence-pane">
              <div className="review-pane-heading"><span>证据</span><b>{events.length}</b></div>
              {events.length === 0 && <p>暂无证据事件</p>}
              {events.map((event) => (
                <article key={event.id}>
                  <b>{eventLabels[event.type] ?? event.type}</b>
                  <p>{eventSummary(event)}</p>
                  <details><summary>查看记录</summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details>
                </article>
              ))}
            </aside>
          </section>
        )}
      </section>
    </main>
  );
}
