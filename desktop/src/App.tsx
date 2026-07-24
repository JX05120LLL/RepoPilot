import { useEffect, useRef, useState } from "react";
import { open, save } from "@tauri-apps/plugin-dialog";
import Markdown from "react-markdown";
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

const DEFAULT_API = "http://127.0.0.1:8765/api";

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
    // 预览配置非法时保持桌面端仅连接默认本机 API。
  }
  return DEFAULT_API;
}

const API = configuredApiBase();
const API_UNAVAILABLE_MESSAGE = "本机 API 尚未启动或无法访问。";
const EVIDENCE_STREAM_ERROR =
  "证据流连接中断，请检查本机 API。任务状态会继续尝试轮询。";
const TASK_EVIDENCE_EXPORT_CAPABILITY = "task_evidence_export";
type Mode = "safe-isolated" | "full-local";
type Operation = "change" | "research";
type WorkspaceView = "task" | "context" | "review";
type EvidenceScope = "key" | "all";
type Project = {
  project_id: string;
  display_name: string;
  root_path?: string;
  is_git_repository?: boolean;
};
type Interrupt = {
  type: string;
  message?: string;
  candidate_files?: unknown;
  recipe?: unknown;
  target_test_class?: unknown;
};
type TaskProgressStage = {
  id: string;
  label: string;
  state: "completed" | "current" | "pending" | "passed" | "failed" | "blocked" | "cancelled" | "unverified";
};
type TaskProgress = {
  current_stage: string;
  summary: string;
  terminal: boolean;
  terminal_kind: string | null;
  stages: TaskProgressStage[];
};
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
  progress?: TaskProgress;
  archived_at?: string | null;
  interrupts?: Interrupt[];
  state?: {
    task_operation?: string;
    task_description?: string;
    plan?: Record<string, unknown> | null;
    pending_approval_action?: string | null;
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
type TaskEvidenceExport = {
  thread_id: string;
  artifact_count: number;
  event_count: number;
  size_bytes: number;
  sha256: string;
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
type TaskAttachment = {
  document_id: string;
  display_name: string;
  content_sha256: string;
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
type RuntimeConfiguration = {
  status: "READY" | "BLOCKED";
  code?: string;
  message?: string;
  writable: boolean;
  restart_required: boolean;
  chat?: { base_url: string; model: string; api_key_configured: boolean };
  embedding?: {
    base_url: string;
    model: string;
    dimensions: number | null;
    api_key_configured: boolean;
  };
  qdrant?: { url: string };
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

type DiffFileSummary = {
  path: string;
  additions: number;
  deletions: number;
  binary: boolean;
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

const keyEvidenceTypes = new Set([
  "TASK_CREATED",
  "TASK_BUDGET_SNAPSHOT",
  "WORKSPACE_PREPARED",
  "PREFLIGHT_COMPLETED",
  "CONTEXT_INGESTED",
  "CONTEXT_RETRIEVED",
  "CONTEXT_BROKER_ASSEMBLED",
  "PLAN_GENERATED",
  "APPROVAL_REQUIRED",
  "PLAN_APPROVED",
  "EXECUTION_APPROVED",
  "TASK_RUNTIME_FAILED",
  "TASK_STATUS_CHANGED",
]);

const keyEvidenceNodes = new Set([
  "INTAKE",
  "WORKSPACE",
  "PREFLIGHT",
  "PLAN",
  "PLAN_APPROVAL",
  "EXECUTION_APPROVAL",
  "PATCH",
  "VERIFY",
  "REVIEW",
  "REPORT",
]);

function isKeyEvidenceEvent(event: TimelineEvent): boolean {
  if (keyEvidenceTypes.has(event.type)) return true;
  const node = event.payload.node;
  if (typeof node === "string" && keyEvidenceNodes.has(node)) return true;
  return /APPROVAL|PATCH|VERIFY|VERIFICATION|FAILED|BLOCKED|CANCELLED/.test(
    event.type,
  );
}

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

function summarizeUnifiedDiff(content: string): DiffFileSummary[] {
  const files: DiffFileSummary[] = [];
  let current: DiffFileSummary | null = null;

  for (const line of content.replace(/\r\n/g, "\n").split("\n")) {
    const header = /^diff --git a\/(.+) b\/(.+)$/.exec(line);
    if (header) {
      current = {
        path: header[2],
        additions: 0,
        deletions: 0,
        binary: false,
      };
      files.push(current);
      continue;
    }
    if (!current) continue;
    if (line.startsWith("Binary files ") || line.startsWith("GIT binary patch")) {
      current.binary = true;
      continue;
    }
    if (line.startsWith("+") && !line.startsWith("+++")) {
      current.additions += 1;
    } else if (line.startsWith("-") && !line.startsWith("---")) {
      current.deletions += 1;
    }
  }
  return files;
}

function DiffSummary({ files }: { files: DiffFileSummary[] }) {
  const additions = files.reduce((total, file) => total + file.additions, 0);
  const deletions = files.reduce((total, file) => total + file.deletions, 0);
  const visibleFiles = files.slice(0, 12);

  return (
    <section className="diff-summary" aria-label="代码变更摘要">
      <div className="diff-summary-facts">
        <span><b>{files.length}</b> 个文件</span>
        <span className="diff-add"><b>+{additions}</b> 新增</span>
        <span className="diff-remove"><b>-{deletions}</b> 删除</span>
      </div>
      {files.length > 0 ? (
        <ul className="diff-file-list">
          {visibleFiles.map((file) => (
            <li key={file.path}>
              <code title={file.path}>{file.path}</code>
              {file.binary ? (
                <span className="diff-binary">二进制</span>
              ) : (
                <span><i className="diff-add">+{file.additions}</i><i className="diff-remove">-{file.deletions}</i></span>
              )}
            </li>
          ))}
          {files.length > visibleFiles.length && (
            <li className="diff-file-more">其余 {files.length - visibleFiles.length} 个文件请查看下方完整 Diff</li>
          )}
        </ul>
      ) : (
        <p>当前产物没有可解析的文件级变更。</p>
      )}
    </section>
  );
}

function ArtifactContent({ kind, content }: { kind: string; content: string }) {
  if (kind === "git_diff") {
    const lines = parseUnifiedDiff(content);
    const files = summarizeUnifiedDiff(content);
    return (
      <div className="artifact-content diff-view" aria-label="代码变更 Diff">
        <DiffSummary files={files} />
        <div className="diff-code-lines">
          {lines.map((line, index) => (
            <div className={"diff-line diff-" + line.kind} key={index}>
              <span>{line.oldLine ?? ""}</span>
              <span>{line.newLine ?? ""}</span>
              <code>{line.content || " "}</code>
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (kind === "report" || kind === "plan_markdown") {
    return (
      <article className="artifact-content markdown-content">
        <Markdown>{content}</Markdown>
      </article>
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
  const [evidenceScope, setEvidenceScope] = useState<EvidenceScope>("key");
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [selectedArtifact, setSelectedArtifact] = useState("");
  const [artifactVersions, setArtifactVersions] = useState<ArtifactVersion[]>(
    [],
  );
  const [selectedArtifactVersion, setSelectedArtifactVersion] = useState<
    number | null
  >(null);
  const [artifactContent, setArtifactContent] = useState("");
  const [exportPath, setExportPath] = useState("");
  const [exportingEvidence, setExportingEvidence] = useState(false);
  const [evidenceExport, setEvidenceExport] = useState<TaskEvidenceExport | null>(null);
  const [revisionComment, setRevisionComment] = useState("");
  const [approvalBusy, setApprovalBusy] = useState(false);
  const [requestError, setRequestError] = useState("");
  const [mcpServer, setMcpServer] = useState("");
  const [mcpConfigPath, setMcpConfigPath] = useState(".repopilot/mcp.toml");
  const [mcpRiskApproved, setMcpRiskApproved] = useState(false);
  const [mcpBusy, setMcpBusy] = useState(false);
  const [mcpResult, setMcpResult] = useState<McpProbeResult | null>(null);
  const [approvedMcpTools, setApprovedMcpTools] = useState<string[]>([]);
  const [contextSnapshot, setContextSnapshot] =
    useState<ContextSnapshot | null>(null);
  const [taskAttachments, setTaskAttachments] = useState<TaskAttachment[]>([]);
  const [telemetry, setTelemetry] = useState<Telemetry | null>(null);
  const [plugins, setPlugins] = useState<Plugin[]>([]);
  const [pluginSource, setPluginSource] = useState("");
  const [pluginBusy, setPluginBusy] = useState(false);
  const [apiReady, setApiReady] = useState(false);
  const [apiCapabilities, setApiCapabilities] = useState<string[]>([]);
  const taskSearchRef = useRef<HTMLInputElement>(null);
  const taskDescriptionRef = useRef<HTMLTextAreaElement>(null);
  const [runtimeHealth, setRuntimeHealth] = useState<RuntimeHealth>({
    status: "UNKNOWN",
    code: "API_NOT_CHECKED",
  });
  const [runtimeConfiguration, setRuntimeConfiguration] =
    useState<RuntimeConfiguration | null>(null);
  const [runtimeConfigurationBusy, setRuntimeConfigurationBusy] = useState(false);
  const [runtimeConfigurationMessage, setRuntimeConfigurationMessage] = useState("");
  const [chatBaseUrl, setChatBaseUrl] = useState("");
  const [chatApiKey, setChatApiKey] = useState("");
  const [chatModel, setChatModel] = useState("");
  const [clearChatApiKey, setClearChatApiKey] = useState(false);
  const [embeddingBaseUrl, setEmbeddingBaseUrl] = useState("");
  const [embeddingApiKey, setEmbeddingApiKey] = useState("");
  const [embeddingModel, setEmbeddingModel] = useState("");
  const [embeddingDimensions, setEmbeddingDimensions] = useState("");
  const [clearEmbeddingApiKey, setClearEmbeddingApiKey] = useState(false);
  const [qdrantUrl, setQdrantUrl] = useState("");
  const [documentPath, setDocumentPath] = useState("");
  const [documentBusy, setDocumentBusy] = useState(false);
  const [documents, setDocuments] = useState<ManagedDocument[]>([]);
  const [attachedDocumentIds, setAttachedDocumentIds] = useState<string[]>([]);
  const [showDocumentPicker, setShowDocumentPicker] = useState(false);
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

  async function loadRuntimeConfiguration() {
    const response = await fetch(`${API}/runtime/configuration`);
    if (response.status === 404 || response.status === 405) {
      setRuntimeConfiguration(null);
      return;
    }
    if (!response.ok) throw new Error("无法读取运行配置");
    const payload = (await response.json()) as RuntimeConfiguration;
    setRuntimeConfiguration(payload);
    if (payload.chat) {
      setChatBaseUrl(payload.chat.base_url);
      setChatModel(payload.chat.model);
    }
    if (payload.embedding) {
      setEmbeddingBaseUrl(payload.embedding.base_url);
      setEmbeddingModel(payload.embedding.model);
      setEmbeddingDimensions(
        payload.embedding.dimensions === null ? "" : String(payload.embedding.dimensions),
      );
    }
    if (payload.qdrant) setQdrantUrl(payload.qdrant.url);
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
        capabilities?: unknown;
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
      const capabilities = Array.isArray(payload.capabilities)
        ? payload.capabilities.filter((item): item is string => typeof item === "string")
        : [];
      setApiCapabilities(ready ? capabilities : []);
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
      setApiCapabilities([]);
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
      loadRuntimeConfiguration(),
      checkApiHealth(),
    ]).catch(() => setRequestError(API_UNAVAILABLE_MESSAGE));
  }, [showArchived]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const shortcut = event.ctrlKey || event.metaKey;
      if (shortcut && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setShowTaskSearch(true);
        window.requestAnimationFrame(() => taskSearchRef.current?.focus());
        return;
      }
      if (shortcut && event.key.toLowerCase() === "n") {
        event.preventDefault();
        beginNewTask();
        window.requestAnimationFrame(() => taskDescriptionRef.current?.focus());
        return;
      }
      if (event.key === "Escape") setShowTaskSearch(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => void checkApiHealth(), 5_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    setAttachedDocumentIds([]);
    setDocumentPath("");
    setShowDocumentPicker(false);
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
              attached_documents?: TaskAttachment[];
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
          setTaskAttachments(contextPayload.attached_documents ?? []);
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

  useEffect(() => {
    setEvidenceScope("key");
  }, [task?.thread_id]);

  useEffect(() => {
    function isEditableTarget(target: EventTarget | null): boolean {
      return (
        target instanceof HTMLInputElement ||
        target instanceof HTMLTextAreaElement ||
        target instanceof HTMLSelectElement ||
        (target instanceof HTMLElement && target.isContentEditable)
      );
    }

    function handleShortcut(event: KeyboardEvent) {
      const key = event.key.toLowerCase();
      if ((event.ctrlKey || event.metaKey) && key === "k") {
        event.preventDefault();
        setShowTaskSearch(true);
        window.requestAnimationFrame(() => taskSearchRef.current?.focus());
        return;
      }
      if (event.key === "Escape" && showTaskSearch) {
        event.preventDefault();
        setShowTaskSearch(false);
        setTaskQuery("");
        return;
      }
      if (
        (event.ctrlKey || event.metaKey) &&
        key === "n" &&
        !isEditableTarget(event.target)
      ) {
        event.preventDefault();
        beginNewTask();
      }
    }

    window.addEventListener("keydown", handleShortcut);
    return () => window.removeEventListener("keydown", handleShortcut);
  }, [showTaskSearch]);

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

  async function saveRuntimeConfiguration() {
    if (!runtimeConfiguration?.writable) return;
    setRuntimeConfigurationBusy(true);
    setRuntimeConfigurationMessage("");
    setRequestError("");
    const dimensions = embeddingDimensions.trim();
    const payload: Record<string, string | number> = {};
    const addText = (name: string, value: string) => {
      if (value.trim()) payload[name] = value.trim();
    };
    addText("chat_base_url", chatBaseUrl);
    addText("chat_model", chatModel);
    addText("embedding_base_url", embeddingBaseUrl);
    addText("embedding_model", embeddingModel);
    addText("qdrant_url", qdrantUrl);
    if (dimensions) payload.embedding_dimensions = Number(dimensions);
    if (chatApiKey) payload.chat_api_key = chatApiKey;
    else if (clearChatApiKey) payload.chat_api_key = "";
    if (embeddingApiKey) payload.embedding_api_key = embeddingApiKey;
    else if (clearEmbeddingApiKey) payload.embedding_api_key = "";
    try {
      const response = await fetch(`${API}/runtime/configuration`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const raw = (await response.json()) as RuntimeConfiguration & {
        detail?: { code?: string; message?: string } | string;
      };
      const detail = typeof raw.detail === "object" ? raw.detail : undefined;
      if (!response.ok) {
        throw new Error(detail?.message ?? detail?.code ?? "运行配置保存失败");
      }
      setRuntimeConfiguration(raw);
      setChatApiKey("");
      setEmbeddingApiKey("");
      setClearChatApiKey(false);
      setClearEmbeddingApiKey(false);
      setRuntimeConfigurationMessage(
        raw.message ?? "配置已保存；重启 RepoPilot Desktop 后生效。",
      );
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "运行配置保存失败");
    } finally {
      setRuntimeConfigurationBusy(false);
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

  function toggleTaskDocument(documentId: string, selected: boolean) {
    if (selected && !attachedDocumentIds.includes(documentId) && attachedDocumentIds.length >= 4) {
      setRequestError("单个任务最多附加 4 份研发文档，请先移除已有附件。");
      return;
    }
    setAttachedDocumentIds((current) => {
      if (!selected) return current.filter((id) => id !== documentId);
      if (current.includes(documentId)) return current;
      return [...current, documentId];
    });
  }

  async function indexDocument(attachToCurrentTask = true) {
    if (!projectId || !documentPath.trim()) return;
    if (attachToCurrentTask && attachedDocumentIds.length >= 4) {
      setRequestError("单个任务最多附加 4 份研发文档，请先移除已有附件。");
      return;
    }
    setDocumentBusy(true);
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
      setDocumentPath("");
      await loadDocuments(projectId);
      const documentId = payload.document?.document_id;
      if (attachToCurrentTask && documentId) {
        setAttachedDocumentIds((current) =>
          current.includes(documentId) ? current : [...current, documentId],
        );
      }
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "文档索引失败");
    } finally {
      setDocumentBusy(false);
    }
  }

  async function exportEvidence() {
    if (!task || taskIsRunning) return;
    setRequestError("");
    setEvidenceExport(null);
    let output = exportPath.trim();
    if (!output) {
      try {
        const selected = await save({
          title: "导出 RepoPilot 审计证据包",
          defaultPath: `repopilot-${task.thread_id}.zip`,
          filters: [{ name: "RepoPilot 审计包", extensions: ["zip"] }],
        });
        if (!selected) return;
        output = selected;
      } catch {
        setRequestError(
          "系统保存对话框仅在已安装的 RepoPilot Desktop 中可用；浏览器预览时请填写 ZIP 的绝对路径。",
        );
        return;
      }
    }
    setExportingEvidence(true);
    try {
      const response = await fetch(`${API}/tasks/${encodeURIComponent(task.thread_id)}/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ output }),
      });
      const raw = (await response.json()) as {
        export?: TaskEvidenceExport;
        detail?: string | { code?: string; message?: string };
      };
      if (!response.ok) {
        const detail = raw.detail;
        throw new Error(
          typeof detail === "string"
            ? detail
            : detail?.message ?? detail?.code ?? "审计证据包导出失败",
        );
      }
      if (!raw.export) throw new Error("审计证据包导出结果无效");
      setEvidenceExport(raw.export);
      setExportPath("");
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "审计证据包导出失败");
    } finally {
      setExportingEvidence(false);
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
    setTaskAttachments([]);
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
          attached_document_ids: attachedDocumentIds,
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
    if (!task || approvalBusy) return;
    setApprovalBusy(true);
    setRequestError("");
    try {
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
        setRequestError(
          typeof payload.detail === "string" ? payload.detail : "审批提交失败",
        );
        return;
      }
      if (decision === "revise") setRevisionComment("");
      setTask(payload as Task);
    } catch {
      setRequestError("审批请求未送达本机 API，请检查服务状态后重试。");
    } finally {
      setApprovalBusy(false);
    }
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
    setTaskAttachments([]);
    setTelemetry(null);
    setDescription("");
    setMode("safe-isolated");
    setOperation("change");
    setConfirmed(false);
    setRevisionComment("");
    setRequestError("");
    setActiveView("task");
  }

  function applyTaskStarter(value: string) {
    setDescription(value);
    setRequestError("");
    window.requestAnimationFrame(() => taskDescriptionRef.current?.focus());
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
      ? "当前项目不是 Git 仓库，无法生成可信基线和 Diff；请使用计划模式，或先初始化 Git 并创建提交。"
      : (selectedModeReadiness?.message ?? "当前项目不支持所选任务类型。");
  const safeModeBlockedByProject = Boolean(
    currentProject && (safeModeReadiness ? safeModeReadiness.status !== "READY" : !currentProject.is_git_repository),
  );
  const safeModeWarningMessage = safeModeReadiness?.message ?? "当前项目尚未初始化 Git，不能创建隔离 Worktree。初始化 Git 后可使用安全隔离修复。";
  const projectStatusLabel = projectDiagnosis
    ? projectDiagnosis.task_modes.safe_isolated.status === "READY"
      ? "隔离修复可用"
      : projectDiagnosis.task_modes.full_local.code === "FULL_LOCAL_RESEARCH_ONLY"
        ? "仅完整本机计划"
        : projectDiagnosis.task_modes.safe_isolated.code
    : currentProject
      ? currentProject.is_git_repository
        ? "Git 基线待诊断"
        : "非 Git 项目"
      : "等待选择项目";
  const projectReadinessItems = currentProject
    ? [
        {
          label: "工作区",
          value:
            projectDiagnosis?.task_modes.safe_isolated.status === "READY"
              ? "安全隔离可用"
              : "完整本机研究",
        },
        {
          label: "Git 基线",
          value: currentProject.is_git_repository ? "已识别" : "未初始化",
        },
        {
          label: "工程 Profile",
          value:
            projectDiagnosis?.profiles.java_maven.status === "READY"
              ? "Java / Maven"
              : "等待预检",
        },
      ]
    : [];
  const taskStarters =
    operation === "research"
      ? [
          {
            label: "梳理项目结构",
            value: "梳理当前项目的模块结构、主要入口和核心业务链路，并给出带来源的说明。",
          },
          {
            label: "定位相关代码",
            value: "根据需求描述定位相关的 Controller、Service、Mapper 和测试文件，并说明每个文件的职责。",
          },
          {
            label: "生成修改计划",
            value: "分析当前项目中潜在的参数校验、权限隔离和异常处理风险，生成一份带证据引用的修改计划。",
          },
        ]
      : [
          {
            label: "修复参数校验",
            value: "定位接口参数校验缺失的问题，提出最小修改方案，并使用现有 Maven 测试验证。",
          },
          {
            label: "检查权限过滤",
            value: "检查当前查询链路中的租户或权限过滤是否完整，定位风险并在最小范围内修复。",
          },
          {
            label: "补充回归测试",
            value: "分析当前变更涉及的行为边界，定位已有测试并补充最小的 Maven 回归测试。",
          },
        ];
  const taskIsRunning = Boolean(
    task &&
      !["REPORT", "FAILED", "PASSED", "BLOCKED", "CANCELLED", "UNVERIFIED"].includes(
        task.status,
      ),
  );
  const taskOutcome = task ? resolveTaskOutcome(task, taskIsRunning) : null;
  const taskCanExportEvidence = Boolean(
    task &&
      ["REPORT", "BLOCKED", "CANCELLED"].includes(task.status) &&
      apiCapabilities.includes(TASK_EVIDENCE_EXPORT_CAPABILITY),
  );
  const taskEvidenceExportRequiresApiRestart = Boolean(
    task &&
      ["REPORT", "BLOCKED", "CANCELLED"].includes(task.status) &&
      apiReady &&
      !apiCapabilities.includes(TASK_EVIDENCE_EXPORT_CAPABILITY),
  );
  const taskStatus =
    task?.status ?? (!apiReady ? "OFFLINE" : runtimeHealth.status);
  const serviceStatus = !apiReady
    ? "offline"
    : runtimeHealth.status === "READY"
      ? "ready"
      : "degraded";
  const visibleEvents = events.slice(-14);
  const attachedDocuments = attachedDocumentIds.map((documentId) =>
    documents.find((document) => document.document_id === documentId) ?? {
      document_id: documentId,
      display_name: `已绑定文档 ${documentId.slice(0, 8)}`,
      content_sha256: "",
      imported_at: "",
    },
  );
  const keyEvidenceEvents = events.filter(isKeyEvidenceEvent);
  const reviewEvents =
    evidenceScope === "key" ? keyEvidenceEvents : events;
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
  const approvalPlan = asRecord(task?.state?.plan);
  const approvalCandidateFiles = readStringList(
    interrupt?.candidate_files ?? approvalPlan?.candidate_files,
  );
  const approvalRecipe =
    readString(interrupt?.recipe) ??
    readString(approvalPlan?.verification_recipe) ??
    "未指定";
  const approvalTargetTest =
    readString(interrupt?.target_test_class) ??
    readString(approvalPlan?.target_test_class);
  const approvalSteps = readStringList(approvalPlan?.steps);
  const executionApproval = interrupt?.type === "EXECUTION_APPROVAL_REQUIRED";
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
            title="搜索任务 (Ctrl+K)"
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
            title="新建任务 (Ctrl+N)"
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
              ref={taskSearchRef}
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
            <strong title={currentProject?.display_name ?? "选择本地项目"}>{currentProject?.display_name ?? "选择本地项目"}</strong>
            {task && (
              <>
                <span>/</span>
                <small title={compactTaskLabel(task)}>{compactTaskLabel(task)}</small>
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
                    <h2>{operation === "research" ? "制定代码计划" : "准备开始代码任务"}</h2>
                    <p>
                      {currentProject
                        ? currentProject.display_name + "  ·  " + projectStatusLabel
                        : "从左侧选择或注册一个本地项目"}
                    </p>
                    {projectReadinessItems.length > 0 && (
                      <dl className="project-readiness" aria-label="当前项目就绪状态">
                        {projectReadinessItems.map((item) => (
                          <div key={item.label}>
                            <dt>{item.label}</dt>
                            <dd>{item.value}</dd>
                          </div>
                        ))}
                      </dl>
                    )}
                    {currentProject && (
                      <div className="task-starters" aria-label="常用任务起点">
                        <span>常用起点</span>
                        <div>
                          {taskStarters.map((starter) => (
                            <button
                              key={starter.label}
                              type="button"
                              onClick={() => applyTaskStarter(starter.value)}
                            >
                              {starter.label}
                              <ArrowRight size={14} />
                            </button>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                )}

                {task && (
                  <>
                    <article className="task-request">
                      <header>
                        <strong>任务</strong>
                        <div className="task-metadata">
                          <span>{activeTaskOperation === "research" ? "计划模式" : "修改代码"}</span>
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
                        {task.progress && task.progress.stages.length > 0 && (
                          <section className="task-progress" aria-label="Agent 任务阶段">
                            <div className="task-progress-heading">
                              <span>任务阶段</span>
                              <small>{task.progress.summary}</small>
                            </div>
                            <ol>
                              {task.progress.stages.map((stage) => (
                                <li key={stage.id} className={`progress-${stage.state}`}>
                                  <span className="progress-marker" aria-hidden="true">
                                    {stage.state === "completed" || stage.state === "passed"
                                      ? <CheckCircle size={14} weight="fill" />
                                      : stage.state === "current"
                                        ? <CircleNotch className={taskIsRunning ? "spin" : ""} size={14} />
                                        : stage.state === "failed" || stage.state === "blocked"
                                          ? <WarningCircle size={14} weight="fill" />
                                          : <i />}
                                  </span>
                                  <span>{stage.label}</span>
                                </li>
                              ))}
                            </ol>
                          </section>
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
                            ? "计划结论等待确认"
                            : interrupt?.type === "EXECUTION_APPROVAL_REQUIRED"
                            ? "执行前需要你的批准"
                            : "修改计划等待审阅"}
                        </strong>
                        <p>{interrupt?.message ?? "审阅计划后决定是否继续。"}</p>
                      </div>
                    </div>
                    <div className="approval-scope" aria-label="本次审批范围">
                      <div className="approval-scope-facts">
                        <span><b>{executionApproval ? "执行范围" : "计划范围"}</b>{executionApproval ? "仅允许受控补丁写入候选文件" : "本次确认不写入代码"}</span>
                        <span><b>Maven Recipe</b><code>{approvalRecipe}</code></span>
                        {approvalTargetTest && <span><b>目标测试</b><code>{approvalTargetTest}</code></span>}
                      </div>
                      {approvalCandidateFiles.length > 0 && (
                        <div className="approval-file-list">
                          <span>候选文件</span>
                          <ul>
                            {approvalCandidateFiles.slice(0, 5).map((path) => <li key={path}><code>{path}</code></li>)}
                          </ul>
                          {approvalCandidateFiles.length > 5 && <small>另有 {approvalCandidateFiles.length - 5} 个候选文件</small>}
                        </div>
                      )}
                      {!executionApproval && approvalSteps.length > 0 && (
                        <details className="approval-steps">
                          <summary>查看计划步骤（{approvalSteps.length}）</summary>
                          <ol>{approvalSteps.map((step, index) => <li key={index}>{step}</li>)}</ol>
                        </details>
                      )}
                      {executionApproval && (
                        <p className="execution-approval-note">批准后才会生成并校验结构化补丁；补丁、Maven 执行和真实 Diff 都将写入审计证据。</p>
                      )}
                    </div>
                    {interrupt?.type === "PLAN_APPROVAL_REQUIRED" && (
                      <textarea value={revisionComment} onChange={(event) => setRevisionComment(event.target.value)} placeholder="填写需要调整的地方" aria-label="计划修改意见" />
                    )}
                    <div className="approval-buttons">
                      {interrupt?.type === "PLAN_APPROVAL_REQUIRED" && (
                        <button className="secondary-button" type="button" onClick={() => void approve("revise")} disabled={!revisionComment.trim() || approvalBusy}>
                          <ArrowClockwise size={16} />要求调整
                        </button>
                      )}
                      <button className="primary-button" type="button" onClick={() => void approve("approve")} disabled={approvalBusy}>
                        <CheckCircle size={16} weight="bold" />
                        {approvalBusy ? "正在提交" : researchPlanApproval ? "确认并生成报告" : executionApproval ? "批准执行" : "确认计划"}
                      </button>
                      <button className="danger-button" type="button" onClick={() => void approve("reject")} disabled={approvalBusy}>
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
                    {(documentPath || attachedDocuments.length > 0) && (
                      <div className="attachment-row">
                        <FileArrowUp size={16} />
                        <span className="attachment-label">{documentPath || "已绑定研发文档"}</span>
                        {documentPath && (
                          <button type="button" onClick={() => void indexDocument(true)} disabled={documentBusy || !projectId || attachedDocumentIds.length >= 4}>
                            {documentBusy ? "正在索引" : "加入上下文"}
                          </button>
                        )}
                        {attachedDocuments.map((document) => (
                          <span className="attachment-chip" key={document.document_id}>
                            <FileCode size={14} />
                            {document.display_name}
                            <button
                              type="button"
                              title="从当前任务移除此文档"
                              aria-label={`移除 ${document.display_name}`}
                              onClick={() => setAttachedDocumentIds((current) => current.filter((id) => id !== document.document_id))}
                            >
                              <XCircle size={15} />
                            </button>
                          </span>
                        ))}
                      </div>
                    )}
                    {showDocumentPicker && (
                      <section className="attachment-picker" aria-label="任务研发文档附件">
                        <header>
                          <div><strong>任务附件</strong><span>{attachedDocumentIds.length}/4</span></div>
                          <button
                            className="icon-button"
                            type="button"
                            title="关闭任务附件面板"
                            aria-label="关闭任务附件面板"
                            onClick={() => setShowDocumentPicker(false)}
                          >
                            <XCircle size={17} />
                          </button>
                        </header>
                        <div className="attachment-document-list">
                          {documents.length === 0 ? (
                            <p>当前项目还没有已索引研发文档。</p>
                          ) : documents.map((document) => (
                            <label key={document.document_id}>
                              <input
                                type="checkbox"
                                checked={attachedDocumentIds.includes(document.document_id)}
                                onChange={(event) => toggleTaskDocument(document.document_id, event.target.checked)}
                                disabled={!attachedDocumentIds.includes(document.document_id) && attachedDocumentIds.length >= 4}
                              />
                              <FileCode size={15} />
                              <span>{document.display_name}</span>
                              {attachedDocumentIds.includes(document.document_id) && <CheckCircle size={15} />}
                            </label>
                          ))}
                        </div>
                        <div className="attachment-import">
                          <input
                            value={documentPath}
                            onChange={(event) => setDocumentPath(event.target.value)}
                            placeholder="MD/TXT 本地路径"
                            aria-label="研发文档本地路径"
                          />
                          <button className="icon-button" type="button" title="选择 MD 或 TXT 文档" onClick={() => void chooseDocument()}>
                            <FolderOpen size={17} />
                          </button>
                          <button
                            className="secondary-button"
                            type="button"
                            onClick={() => void indexDocument(true)}
                            disabled={!projectId || !documentPath.trim() || documentBusy || attachedDocumentIds.length >= 4}
                          >
                            {documentBusy ? "导入中" : "导入并绑定"}
                          </button>
                        </div>
                      </section>
                    )}
                    <textarea
                      ref={taskDescriptionRef}
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
                        <button className="icon-button" type="button" title="管理任务研发文档附件" onClick={() => setShowDocumentPicker((current) => !current)} disabled={!projectId}>
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
                            aria-label="计划模式"
                            aria-pressed={operation === "research"}
                            title="只研究代码并输出证据化计划，不写入文件、不运行 Maven"
                          >
                            <ListMagnifyingGlass size={15} />
                            <span>计划模式</span>
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
                <SlidersHorizontal size={19} />
                <div><h3>运行配置</h3><p>仅保存到桌面应用自己的本地配置文件，密钥不会回显或写入任务证据。</p></div>
              </div>
              <div className="settings-content runtime-configuration">
                {!runtimeConfiguration && (
                  <p className="configuration-notice">当前本地 API 尚未支持应用内运行配置。重启 RepoPilot Desktop 后重试。</p>
                )}
                {runtimeConfiguration && (
                  <>
                    <div className="runtime-configuration-status">
                      <span className={runtimeConfiguration.writable ? "ready" : "blocked"}>
                        {runtimeConfiguration.writable ? "可保存到桌面配置" : "当前连接只读"}
                      </span>
                      <p>{runtimeConfiguration.message ?? "保存后需要重启 RepoPilot Desktop，正在运行的任务不会读取新配置。"}</p>
                    </div>
                    <div className="runtime-config-grid">
                      <label>Chat Base URL<input value={chatBaseUrl} onChange={(event) => setChatBaseUrl(event.target.value)} placeholder="https://api.deepseek.com" disabled={!runtimeConfiguration.writable} /></label>
                      <label>Chat Model<input value={chatModel} onChange={(event) => setChatModel(event.target.value)} placeholder="deepseek-chat" disabled={!runtimeConfiguration.writable} /></label>
                      <label className="runtime-secret-field">
                        Chat API Key
                        <input type="password" value={chatApiKey} onChange={(event) => { setChatApiKey(event.target.value); setClearChatApiKey(false); }} placeholder={runtimeConfiguration.chat?.api_key_configured ? "已配置，输入新值才会替换" : "未配置"} autoComplete="off" disabled={!runtimeConfiguration.writable || clearChatApiKey} />
                        <small>{runtimeConfiguration.chat?.api_key_configured ? "已配置，值不会显示。" : "尚未配置。"}</small>
                      </label>
                      <label>Embedding Base URL<input value={embeddingBaseUrl} onChange={(event) => setEmbeddingBaseUrl(event.target.value)} placeholder="OpenAI-compatible embedding endpoint" disabled={!runtimeConfiguration.writable} /></label>
                      <label>Embedding Model<input value={embeddingModel} onChange={(event) => setEmbeddingModel(event.target.value)} placeholder="text-embedding-3-small" disabled={!runtimeConfiguration.writable} /></label>
                      <label>Embedding Dimensions<input inputMode="numeric" value={embeddingDimensions} onChange={(event) => setEmbeddingDimensions(event.target.value)} placeholder="1536" disabled={!runtimeConfiguration.writable} /></label>
                      <label className="runtime-secret-field">
                        Embedding API Key
                        <input type="password" value={embeddingApiKey} onChange={(event) => { setEmbeddingApiKey(event.target.value); setClearEmbeddingApiKey(false); }} placeholder={runtimeConfiguration.embedding?.api_key_configured ? "已配置，输入新值才会替换" : "未配置"} autoComplete="off" disabled={!runtimeConfiguration.writable || clearEmbeddingApiKey} />
                        <small>{runtimeConfiguration.embedding?.api_key_configured ? "已配置，值不会显示。" : "尚未配置。"}</small>
                      </label>
                      <label className="runtime-config-wide">Qdrant URL<input value={qdrantUrl} onChange={(event) => setQdrantUrl(event.target.value)} placeholder="http://127.0.0.1:6333" disabled={!runtimeConfiguration.writable} /></label>
                    </div>
                    <div className="runtime-configuration-actions">
                      <label className="checkbox-row"><input type="checkbox" checked={clearChatApiKey} onChange={(event) => setClearChatApiKey(event.target.checked)} disabled={!runtimeConfiguration.writable} />清除 Chat API Key</label>
                      <label className="checkbox-row"><input type="checkbox" checked={clearEmbeddingApiKey} onChange={(event) => setClearEmbeddingApiKey(event.target.checked)} disabled={!runtimeConfiguration.writable} />清除 Embedding API Key</label>
                      <button className="secondary-button" type="button" onClick={() => void saveRuntimeConfiguration()} disabled={!runtimeConfiguration.writable || runtimeConfigurationBusy}>
                        {runtimeConfigurationBusy ? "正在保存" : "保存配置"}
                      </button>
                    </div>
                    {runtimeConfigurationMessage && <p className="configuration-notice success">{runtimeConfigurationMessage}</p>}
                  </>
                )}
              </div>
            </section>

            <section className="settings-section">
              <div className="settings-title">
                <FileArrowUp size={19} />
                <div><h3>研发文档</h3><p>MD / TXT · {documents.length} 份已索引文档</p></div>
              </div>
              <div className="settings-content">
                <div className="inline-form">
                  <input value={documentPath} onChange={(event) => setDocumentPath(event.target.value)} placeholder="本地文档路径" />
                  <button className="icon-button" type="button" title="选择文档" onClick={() => void chooseDocument()}><FolderOpen size={17} /></button>
                  <button className="secondary-button" type="button" onClick={() => void indexDocument(false)} disabled={!projectId || !documentPath.trim() || documentBusy}>
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

            {(contextSnapshot || telemetry || taskAttachments.length > 0) && (
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
                  {taskAttachments.length > 0 && (
                    <div className="task-attachment-summary">
                      <span>本次任务附件</span>
                      <ul>
                        {taskAttachments.map((document) => (
                          <li key={document.document_id}>
                            <FileCode size={15} />
                            <b>{document.display_name}</b>
                            <code>{document.content_sha256.slice(0, 12)}</code>
                          </li>
                        ))}
                      </ul>
                    </div>
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
                <div className="review-header-actions">
                  {artifactVersions.length > 0 && (
                    <select value={selectedArtifactVersion ?? ""} onChange={(event) => setSelectedArtifactVersion(Number(event.target.value))} aria-label="产物版本">
                      {artifactVersions.map((version) => (
                        <option key={version.version} value={version.version}>
                          v{version.version} · {version.created_at.slice(0, 19).replace("T", " ")}
                        </option>
                      ))}
                    </select>
                  )}
                  {taskCanExportEvidence && (
                    <button className="secondary-button" type="button" onClick={() => void exportEvidence()} disabled={exportingEvidence}>
                      <Archive size={16} />{exportingEvidence ? "正在导出" : "导出证据包"}
                    </button>
                  )}
                </div>
              </header>
              {taskCanExportEvidence && (
                <div className="evidence-export-control">
                  <input
                    value={exportPath}
                    onChange={(event) => setExportPath(event.target.value)}
                    placeholder="浏览器预览可填写 ZIP 绝对路径"
                    aria-label="审计证据包导出路径"
                  />
                  {evidenceExport && (
                    <span>已导出 {evidenceExport.artifact_count} 份产物 · {evidenceExport.size_bytes.toLocaleString()} B · {evidenceExport.sha256.slice(0, 12)}</span>
                  )}
                </div>
              )}
              {taskEvidenceExportRequiresApiRestart && (
                <p className="export-capability-warning">
                  本机 API 尚未加载审计导出能力。重启预览或 RepoPilot Desktop 后重试。
                </p>
              )}
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
              <div className="review-pane-heading">
                <span>证据</span>
                <div className="evidence-filter" aria-label="证据显示范围">
                  <button
                    className={evidenceScope === "key" ? "active" : ""}
                    type="button"
                    aria-pressed={evidenceScope === "key"}
                    onClick={() => setEvidenceScope("key")}
                  >
                    关键 {keyEvidenceEvents.length}
                  </button>
                  <button
                    className={evidenceScope === "all" ? "active" : ""}
                    type="button"
                    aria-pressed={evidenceScope === "all"}
                    onClick={() => setEvidenceScope("all")}
                  >
                    全部 {events.length}
                  </button>
                </div>
              </div>
              {reviewEvents.length === 0 && <p>暂无符合当前范围的证据事件</p>}
              {reviewEvents.map((event) => (
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
