import { useEffect, useRef, useState, type MouseEvent } from "react";
import ReactMarkdown from "react-markdown";
import { open, save } from "@tauri-apps/plugin-dialog";
import { ArtifactContent } from "./components/ArtifactContent";
import { CommandPalette, type CommandPaletteItem } from "./components/CommandPalette";
import { TaskInspector } from "./components/TaskInspector";
import { TaskTerminalDock, type TerminalCommand, type TerminalResult } from "./components/TaskTerminalDock";
import { TaskProgressTrail } from "./components/TaskProgressTrail";
import { ReviewDecisionSummary } from "./components/ReviewDecisionSummary";
import { TaskDiagnosticPanel } from "./components/TaskDiagnosticPanel";
import { API } from "./lib/api";
import { asRecord, readString, readStringList } from "./lib/values";
import {
  loadWorkbenchPreferences,
  saveWorkbenchPreferences,
  type WorkbenchPreferences,
} from "./lib/workbenchPreferences";
import {
  Archive,
  ArrowRight,
  ArrowClockwise,
  ArrowUp,
  CheckCircle,
  ChatCircle,
  CircleNotch,
  DotsThree,
  FileArrowUp,
  FileCode,
  FolderOpen,
  ListMagnifyingGlass,
  MagnifyingGlass,
  Paperclip,
  PencilSimple,
  Plus,
  PuzzlePiece,
  ShieldCheck,
  SidebarSimple,
  SlidersHorizontal,
  Stack,
  Target,
  TerminalWindow,
  WarningCircle,
  XCircle,
} from "@phosphor-icons/react";

const API_UNAVAILABLE_MESSAGE = "本机 API 尚未启动或无法访问。";
const EVIDENCE_STREAM_ERROR =
  "证据流连接中断，请检查本机 API。任务状态会继续尝试轮询。";
const TASK_EVIDENCE_EXPORT_CAPABILITY = "task_evidence_export";
const terminalTaskStatuses = new Set([
  "REPORT",
  "FAILED",
  "PASSED",
  "BLOCKED",
  "CANCELLED",
  "UNVERIFIED",
]);
const capabilityKindLabels = {
  builtin_tool: "内置工具",
  skill: "Skill",
  mcp_tool: "MCP 工具",
} as const;
const capabilityRiskLabels: Record<string, string> = {
  read: "读取",
  write: "写入",
  process: "进程",
  network: "网络",
  secret_access: "密钥",
};
type Mode = "safe-isolated" | "full-local";
type Operation = "change" | "research";
type ConversationMode = "goal" | "plan";
type ComposerMode = "chat" | "research" | "change";
type WorkspaceView = "task" | "context" | "settings" | "review";
type EvidenceScope = "key" | "all";
type EventStreamState = "idle" | "connecting" | "connected" | "reconnecting" | "offline" | "closed";
type Project = {
  project_id: string;
  display_name: string;
  root_path?: string;
  is_git_repository?: boolean;
  archived_at?: string | null;
};
type Conversation = {
  conversation_id: string;
  project_id?: string | null;
  display_title: string;
  mode: ConversationMode;
  created_at: string;
  updated_at: string;
  archived_at?: string | null;
};
type ConversationMessage = {
  message_id: string;
  conversation_id: string;
  sequence: number;
  role: "user" | "assistant";
  kind: "chat_request" | "chat_response" | "task_request" | "task_summary";
  content: string;
  task_thread_id?: string | null;
  task_status?: string | null;
  task_verdict?: string | null;
  created_at: string;
};
type ConversationContextState = {
  compacted: boolean;
  compacted_through_sequence: number;
  estimated_tokens: number;
  budget_tokens: number;
};
type StreamingChat = {
  conversationId: string;
  content: string;
};
type RenameTarget =
  | { kind: "project"; id: string; title: string }
  | { kind: "task"; id: string; title: string }
  | { kind: "conversation"; id: string; title: string };
type SidebarMenu =
  | { kind: "project"; id: string; x: number; y: number }
  | { kind: "task"; id: string; x: number; y: number }
  | { kind: "conversation"; id: string; x: number; y: number };
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
  conversation_id?: string | null;
  task_mode?: string;
  task_operation?: Operation;
  task_description?: string;
  created_at?: string;
  updated_at?: string;
  status: string;
  pending_approval: boolean;
  verdict?: string | null;
  diagnostic?: {
    tone: "neutral" | "success" | "warning" | "danger";
    code: string;
    title: string;
    summary: string;
    recommended_action: string;
  };
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
type CapabilityPolicyDecision = {
  allowed: boolean;
  requires_approval: boolean;
  code: string;
  reason: string;
};
type CapabilityDirectoryItem = {
  capability_id: string;
  name: string;
  description: string;
  kind: keyof typeof capabilityKindLabels;
  scope: string;
  source_label: string;
  risks: string[];
  enabled: boolean;
  details: Record<string, unknown>;
  safe_policy?: CapabilityPolicyDecision;
  full_policy?: CapabilityPolicyDecision;
  discovered_mcp?: boolean;
};
type CapabilityDirectory = {
  status: string;
  capabilities: CapabilityDirectoryItem[];
  plugins: Array<{
    plugin_id: string;
    name: string;
    version: string;
    description: string;
    enabled: boolean;
    integrity_status: string;
    active: boolean;
  }>;
  issues: Array<{ code: string; message: string }>;
};
type CapabilityFilter = "all" | keyof typeof capabilityKindLabels;
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
type RuntimeDependency = {
  component: string;
  status: "READY" | "BLOCKED";
  code: string;
  message: string;
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
type ChatModelPreset = {
  id: string;
  label: string;
  baseUrl: string;
  model: string;
  description: string;
};
type EmbeddingModelPreset = {
  id: string;
  label: string;
  baseUrl: string;
  model: string;
  dimensions: string;
  description: string;
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
const artifactLabels: Record<string, string> = {
  report: "任务报告",
  plan_markdown: "修改计划",
  plan_json: "计划 JSON",
  patch_proposal: "补丁提案",
  git_diff: "真实 Diff",
  verification: "验证结果",
  telemetry: "运行遥测",
};
const taskStateLabels: Record<string, string> = {
  WAITING_APPROVAL: "等待审批",
  RUNNING: "正在执行",
  REPORT: "任务已结束",
  PASSED: "验证通过",
  FAILED: "验证失败",
  BLOCKED: "已阻断",
  CANCELLED: "已取消",
  UNVERIFIED: "尚未验证",
};
const eventStreamLabels: Record<EventStreamState, string> = {
  idle: "未连接证据流",
  connecting: "正在连接证据流",
  connected: "实时证据流",
  reconnecting: "证据流重连中",
  offline: "API 不可达，轮询保底",
  closed: "证据流已结束",
};
const runtimeDependencyLabels: Record<string, string> = {
  chat_provider: "对话模型",
  embedding_provider: "Embedding 模型",
  qdrant: "Qdrant",
};
const chatModelPresets: ChatModelPreset[] = [
  {
    id: "deepseek-chat",
    label: "DeepSeek Chat",
    baseUrl: "https://api.deepseek.com",
    model: "deepseek-chat",
    description: "OpenAI-compatible 对话接口",
  },
  {
    id: "kimi-k3",
    label: "Kimi K3",
    baseUrl: "https://api.moonshot.cn/v1",
    model: "kimi-k3",
    description: "需使用开放平台可用的 API Key",
  },
];
const embeddingModelPresets: EmbeddingModelPreset[] = [
  {
    id: "openai-embedding-small",
    label: "OpenAI embedding-3-small",
    baseUrl: "https://api.openai.com/v1",
    model: "text-embedding-3-small",
    dimensions: "1536",
    description: "1536 维 OpenAI-compatible 向量接口",
  },
];

function taskStateLabel(status: string, verdict?: string | null, pendingApproval = false): string {
  if (pendingApproval) return taskStateLabels.WAITING_APPROVAL;
  return taskStateLabels[verdict || status] ?? verdict ?? status;
}

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
      title: "已准备好继续处理",
      detail: "RepoPilot 已完成定位；安全隔离模式会在实际写入前等待一次明确确认。",
    };
  }
  if (running) {
    return {
      tone: "neutral",
      title: "正在处理这个目标",
      detail: "正在定位相关代码、应用受控修改并执行验证。",
    };
  }

  const result = (item.verdict ?? item.status).toUpperCase();
  if (result === "PASSED") {
    return {
      tone: "success",
      title: "目标已完成",
      detail: "已应用代码修改，并已通过声明的 Maven 验证。",
    };
  }
  if (result === "FAILED") {
    return {
      tone: "danger",
      title: "目标尚未完成",
      detail: "修改或验证出现明确失败，RepoPilot 已停止继续写入。",
    };
  }
  if (result === "BLOCKED") {
    return {
      tone: "danger",
      title: "暂时无法继续处理",
      detail: "安全策略或环境条件阻断了任务，未继续执行高风险动作。",
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
      title: "已完成分析，但尚未修复",
      detail: "当前只有研究结论或计划，没有可确认的代码修改和验证证据。",
    };
  }
  return {
    tone: "neutral",
    title: "任务已结束",
    detail: "任务状态已经固化，可查看证据和产物了解完整过程。",
  };
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
  const [savedWorkbenchPreferences] = useState<WorkbenchPreferences>(
    loadWorkbenchPreferences,
  );
  const [projects, setProjects] = useState<Project[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [projectId, setProjectId] = useState(
    () => savedWorkbenchPreferences.projectId ?? "",
  );
  const [description, setDescription] = useState("");
  const [mode, setMode] = useState<Mode>("safe-isolated");
  const [operation, setOperation] = useState<Operation>("change");
  const [composerMode, setComposerMode] = useState<ComposerMode>("chat");
  const [chatBusy, setChatBusy] = useState(false);
  const [activeView, setActiveView] = useState<WorkspaceView>(
    () => savedWorkbenchPreferences.activeView ?? "task",
  );
  const [confirmed, setConfirmed] = useState(false);
  const [task, setTask] = useState<Task | null>(null);
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const selectedConversationIdRef = useRef<string | null>(null);
  const conversationMessageRequestRef = useRef(0);
  const taskSelectionRequestRef = useRef(0);
  const [conversationMessages, setConversationMessages] = useState<ConversationMessage[]>([]);
  const [conversationContext, setConversationContext] =
    useState<ConversationContextState | null>(null);
  const [conversationMessagesLoading, setConversationMessagesLoading] = useState(false);
  const [streamingChat, setStreamingChat] = useState<StreamingChat | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [showArchived, setShowArchived] = useState(false);
  const [showTaskSearch, setShowTaskSearch] = useState(false);
  const [taskQuery, setTaskQuery] = useState("");
  const [showCommandPalette, setShowCommandPalette] = useState(false);
  const [showTaskInspector, setShowTaskInspector] = useState(
    () =>
      savedWorkbenchPreferences.showTaskInspector ??
      window.matchMedia("(min-width: 1081px)").matches,
  );
  const [showTaskTerminal, setShowTaskTerminal] = useState(
    () => savedWorkbenchPreferences.showTaskTerminal ?? false,
  );
  const [initialDataReady, setInitialDataReady] = useState(false);
  const [workspaceRestored, setWorkspaceRestored] = useState(false);
  const restoreStartedRef = useRef(false);
  const [events, setEvents] = useState<TimelineEvent[]>([]);
  const [eventStreamState, setEventStreamState] = useState<EventStreamState>("idle");
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
  const [executionApprovalConfirmation, setExecutionApprovalConfirmation] = useState(false);
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
  const [capabilityDirectory, setCapabilityDirectory] =
    useState<CapabilityDirectory | null>(null);
  const [capabilityFilter, setCapabilityFilter] = useState<CapabilityFilter>("all");
  const [apiReady, setApiReady] = useState(false);
  const [apiCapabilities, setApiCapabilities] = useState<string[]>([]);
  const taskSearchRef = useRef<HTMLInputElement>(null);
  const taskDescriptionRef = useRef<HTMLTextAreaElement>(null);
  const [runtimeHealth, setRuntimeHealth] = useState<RuntimeHealth>({
    status: "UNKNOWN",
    code: "API_NOT_CHECKED",
  });
  const [runtimeDependencies, setRuntimeDependencies] = useState<RuntimeDependency[]>([]);
  const [runtimeHealthChecking, setRuntimeHealthChecking] = useState(false);
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
  const [documentBusy, setDocumentBusy] = useState(false);
  const [documents, setDocuments] = useState<ManagedDocument[]>([]);
  const [attachedDocumentIds, setAttachedDocumentIds] = useState<string[]>([]);
  const [projectDiagnosis, setProjectDiagnosis] = useState<ProjectDiagnosis | null>(null);
  const [sidebarMenu, setSidebarMenu] = useState<SidebarMenu | null>(null);
  const [renameTarget, setRenameTarget] = useState<RenameTarget | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const conversationEndRef = useRef<HTMLDivElement | null>(null);
  const summarizedTaskRef = useRef<string | null>(null);
  const taskBlocksNewTurn = Boolean(task && !terminalTaskStatuses.has(task.status));

  function activateConversation(next: Conversation | null) {
    selectedConversationIdRef.current = next?.conversation_id ?? null;
    setConversation(next);
  }

  async function loadProjects() {
    const response = await fetch(`${API}/projects`);
    if (!response.ok) throw new Error("无法读取项目列表");
    const data = (await response.json()) as { projects?: Project[] };
    const nextProjects = data.projects ?? [];
    setProjects(nextProjects);
    setProjectId((current) =>
      nextProjects.some((project) => project.project_id === current)
        ? current
        : nextProjects[0]?.project_id || "",
    );
  }

  async function loadConversations(includeArchived = showArchived) {
    const response = await fetch(
      `${API}/conversations?include_archived=${includeArchived}`,
    );
    if (!response.ok) throw new Error("无法读取对话列表");
    const data = (await response.json()) as { conversations?: Conversation[] };
    setConversations(data.conversations ?? []);
  }

  async function loadConversationMessages(conversationId: string) {
    if (selectedConversationIdRef.current !== conversationId) return null;
    const requestSequence = ++conversationMessageRequestRef.current;
    setConversationMessagesLoading(true);
    try {
      const response = await fetch(
        `${API}/conversations/${encodeURIComponent(conversationId)}/messages`,
      );
      if (!response.ok) throw new Error("无法读取对话消息");
      const data = (await response.json()) as {
        messages?: ConversationMessage[];
        context?: ConversationContextState;
      };
      if (
        selectedConversationIdRef.current !== conversationId ||
        conversationMessageRequestRef.current !== requestSequence
      ) {
        return null;
      }
      setConversationMessages(data.messages ?? []);
      setConversationContext(data.context ?? null);
      return data;
    } finally {
      if (
        selectedConversationIdRef.current === conversationId &&
        conversationMessageRequestRef.current === requestSequence
      ) {
        setConversationMessagesLoading(false);
      }
    }
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
    if (!targetProjectId || !projects.some((project) => project.project_id === targetProjectId)) {
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

  async function loadCapabilityDirectory(targetProjectId: string) {
    if (!targetProjectId || !projects.some((project) => project.project_id === targetProjectId)) {
      setCapabilityDirectory(null);
      return;
    }
    const response = await fetch(
      `${API}/projects/${encodeURIComponent(targetProjectId)}/capability-directory`,
    );
    // 与旧版本机 API 保持兼容，避免桌面预览因新增信息面板阻断原有任务流程。
    if (response.status === 404) {
      setCapabilityDirectory(null);
      return;
    }
    if (!response.ok) throw new Error("无法读取项目能力目录");
    setCapabilityDirectory((await response.json()) as CapabilityDirectory);
  }

  async function checkApiHealth() {
    setRuntimeHealthChecking(true);
    try {
      const response = await fetch(`${API}/health`);
      const payload = (await response.json()) as {
        status?: string;
        agent_status?: "READY" | "BLOCKED";
        capabilities?: unknown;
        dependencies?: Array<{
          component?: string;
          status?: string;
          code?: string;
          message?: string;
        }>;
      };
      const dependencies = Array.isArray(payload.dependencies)
        ? payload.dependencies.flatMap((item) =>
            typeof item.component === "string" &&
            (item.status === "READY" || item.status === "BLOCKED") &&
            typeof item.code === "string" &&
            typeof item.message === "string"
              ? [{ component: item.component, status: item.status as RuntimeDependency["status"], code: item.code, message: item.message }]
              : [],
          )
        : [];
      setRuntimeDependencies(dependencies);
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
      const blockedDependency = dependencies.find(
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
      setRuntimeDependencies([]);
      setRuntimeHealth({
        status: "UNKNOWN",
        code: "API_UNAVAILABLE",
        message: "无法连接本机 API，请启动 RepoPilot 后端后重试。",
      });
    } finally {
      setRuntimeHealthChecking(false);
    }
  }

  async function copyTerminalCommand(command: string): Promise<boolean> {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(command);
        return true;
      }
      const field = document.createElement("textarea");
      field.value = command;
      field.setAttribute("readonly", "");
      field.style.position = "fixed";
      field.style.opacity = "0";
      document.body.append(field);
      field.select();
      const copied = document.execCommand("copy");
      field.remove();
      if (!copied) throw new Error("COPY_FAILED");
      return true;
    } catch {
      setRequestError("无法复制受控终端命令，请手动选择后复制。");
      return false;
    }
  }

  async function runTerminalCommand(command: TerminalCommand): Promise<TerminalResult> {
    if (!task) throw new Error("请先选择一个任务");
    const threadId = encodeURIComponent(task.thread_id);
    const taskResponse = await fetch(`${API}/tasks/${threadId}`);
    if (!taskResponse.ok) throw new Error("任务状态不可读取，请检查本机 API");
    const snapshot = (await taskResponse.json()) as Task;
    setTask(snapshot);

    const status = taskStateLabel(snapshot.status, snapshot.verdict, snapshot.pending_approval);
    const progress = snapshot.progress?.summary ?? "尚未生成阶段摘要";
    if (command.id === "status") {
      return {
        title: "任务状态",
        lines: [
          `状态  ${status}`,
          `阶段  ${snapshot.progress?.current_stage ?? snapshot.status}`,
          progress,
        ],
      };
    }

    if (command.id === "events") {
      const recentEvents = events.slice(-8).reverse().map((event) =>
        `${eventLabels[event.type] ?? event.type}  ${eventSummary(event)}`,
      );
      return {
        title: `最近证据 ${recentEvents.length}`,
        lines: recentEvents.length
          ? recentEvents
          : ["当前任务尚未产生可展示的脱敏证据事件。"],
      };
    }

    const artifactResponse = await fetch(`${API}/tasks/${threadId}/artifacts`);
    if (!artifactResponse.ok) throw new Error("任务产物目录不可读取");
    const artifactPayload = (await artifactResponse.json()) as { artifacts?: Artifact[] };
    const nextArtifacts = artifactPayload.artifacts ?? [];
    setArtifacts(nextArtifacts);

    if (command.id === "artifacts") {
      return {
        title: `任务产物 ${nextArtifacts.length}`,
        lines: nextArtifacts.length
          ? nextArtifacts.slice(0, 8).map((artifact) =>
              `${artifactLabels[artifact.kind] ?? artifact.kind}  ${artifact.size_bytes.toLocaleString()} B  ${artifact.sha256.slice(0, 12)}`,
            )
          : ["当前任务尚未生成可读取产物。"],
      };
    }

    const recentEvidence = events.slice(-3).reverse().map((event) =>
      `${eventLabels[event.type] ?? event.type}  ${eventSummary(event)}`,
    );
    return {
      title: "任务审阅",
      lines: [
        `状态  ${status}`,
        snapshot.pending_approval ? "当前任务正在等待明确审批。" : "当前任务没有待处理审批。",
        `已验证产物  ${nextArtifacts.length} 项`,
        ...(recentEvidence.length ? recentEvidence : ["尚无可展示的关键证据事件。"]),
      ],
    };
  }

  useEffect(() => {
    void Promise.all([
      loadProjects(),
      loadTasks(showArchived),
      loadConversations(showArchived),
      loadPlugins(),
      loadRuntimeConfiguration(),
      checkApiHealth(),
    ])
      .then(() => setInitialDataReady(true))
      .catch(() => setRequestError(API_UNAVAILABLE_MESSAGE));
  }, [showArchived]);

  useEffect(() => {
    if (!initialDataReady || restoreStartedRef.current) return;
    restoreStartedRef.current = true;
    const savedTask = savedWorkbenchPreferences.threadId
      ? tasks.find((item) => item.thread_id === savedWorkbenchPreferences.threadId)
      : undefined;
    if (!savedTask) {
      setWorkspaceRestored(true);
      return;
    }
    void selectTask(
      savedTask,
      savedWorkbenchPreferences.activeView ?? "task",
    ).finally(() => setWorkspaceRestored(true));
  }, [initialDataReady, savedWorkbenchPreferences, tasks]);

  useEffect(() => {
    if (!workspaceRestored) return;
    saveWorkbenchPreferences({
      projectId: projectId || undefined,
      threadId: task?.thread_id,
      activeView,
      showTaskInspector,
      showTaskTerminal,
    });
  }, [
    activeView,
    projectId,
    showTaskInspector,
    showTaskTerminal,
    task?.thread_id,
    workspaceRestored,
  ]);

  useEffect(() => {
    const timer = window.setInterval(() => void checkApiHealth(), 5_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    setAttachedDocumentIds([]);
    void loadDocuments(projectId).catch((error) =>
      setRequestError(
        error instanceof Error ? error.message : "无法读取已导入研发文档",
      ),
    );
  }, [projectId, projects]);

  useEffect(() => {
    void loadProjectDiagnosis(projectId).catch((error) =>
      setRequestError(error instanceof Error ? error.message : "无法读取项目诊断"),
    );
  }, [projectId, projects]);

  useEffect(() => {
    void loadCapabilityDirectory(projectId).catch((error) =>
      setRequestError(error instanceof Error ? error.message : "无法读取项目能力目录"),
    );
  }, [projectId, projects]);

  useEffect(() => {
    if (taskBlocksNewTurn || !projectDiagnosis) return;
    const recommendedOperation =
      projectDiagnosis.recommended_task_operation ??
      (projectDiagnosis.task_modes.full_local.code === "FULL_LOCAL_RESEARCH_ONLY"
        ? "research"
        : "change");
    setMode(projectDiagnosis.recommended_task_mode);
    setOperation(recommendedOperation);
    setConfirmed(false);
  }, [projectDiagnosis, task?.thread_id, taskBlocksNewTurn]);

  useEffect(() => {
    if (!task) return;
    const source = new EventSource(`${API}/tasks/${task.thread_id}/events`);
    setEventStreamState("connecting");
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
    source.onopen = () => {
      setEventStreamState("connected");
      setRequestError((current) => (current === EVIDENCE_STREAM_ERROR ? "" : current));
    };
    source.addEventListener("evidence", appendEvent);
    source.addEventListener("state", (event) => {
      appendEvent(event as MessageEvent<string>);
      try {
        const snapshot = JSON.parse(
          (event as MessageEvent<string>).data,
        ) as Pick<Task, "status" | "pending_approval" | "verdict">;
        setTask((current) => (current ? { ...current, ...snapshot } : current));
        if (terminalTaskStatuses.has(snapshot.status)) {
          completed = true;
          setEventStreamState("closed");
          source.close();
          if (task.conversation_id) {
            void Promise.all([
              loadConversationMessages(task.conversation_id),
              loadConversations(showArchived),
            ]).catch(() =>
              setRequestError("任务已结束，但暂时无法刷新对话记录。"),
            );
          }
        }
      } catch {
        // 事件保留在时间线，任务详情仍会由轮询同步。
      }
    });
    source.addEventListener("error", () => {
      if (completed) return;
      setEventStreamState("reconnecting");
      // EventSource 会自动重连；健康检查只用于区分 API 故障和短暂的连接抖动。
      void fetch(`${API}/health`)
        .then((response) => {
          if (!response.ok) throw new Error("API_UNAVAILABLE");
          setRequestError((current) => current === EVIDENCE_STREAM_ERROR ? "" : current);
        })
        .catch(() => {
          setEventStreamState("offline");
          setRequestError(EVIDENCE_STREAM_ERROR);
        });
    });
    return () => source.close();
  }, [task?.thread_id]);

  useEffect(() => {
    if (!conversationMessages.length && !task) return;
    const frame = window.requestAnimationFrame(() =>
      conversationEndRef.current?.scrollIntoView({ block: "end" }),
    );
    return () => window.cancelAnimationFrame(frame);
  }, [conversationMessages.length, task?.thread_id, task?.status, task?.pending_approval]);

  useEffect(() => {
    if (task?.pending_approval) setActiveView("task");
  }, [task?.pending_approval]);

  useEffect(() => {
    if (!task?.pending_approval) setExecutionApprovalConfirmation(false);
  }, [task?.pending_approval]);

  useEffect(() => {
    setExecutionApprovalConfirmation(false);
  }, [task?.thread_id]);

  useEffect(() => {
    if (!task) return;
    const threadId = task.thread_id;
    let active = true;
    let timer: number | undefined;
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
          if (
            snapshot.conversation_id &&
            terminalTaskStatuses.has(snapshot.status)
          ) {
            const summaryKey = `${snapshot.thread_id}:${snapshot.status}:${snapshot.verdict ?? ""}`;
            if (summarizedTaskRef.current !== summaryKey) {
              summarizedTaskRef.current = summaryKey;
              void Promise.all([
                loadConversationMessages(snapshot.conversation_id),
                loadConversations(showArchived),
              ]).catch(() =>
                setRequestError("任务已结束，但暂时无法刷新对话记录。"),
              );
            }
            if (timer !== undefined) window.clearInterval(timer);
          }
        }
      } catch {
        // SSE 是首选通道；轮询失败不覆盖已显示的证据或产物。
      }
    }
    void refreshTask();
    timer = window.setInterval(() => void refreshTask(), 2000);
    return () => {
      active = false;
      if (timer !== undefined) window.clearInterval(timer);
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
        setShowCommandPalette(true);
        return;
      }
      if (event.key === "Escape" && showCommandPalette) {
        event.preventDefault();
        setShowCommandPalette(false);
        return;
      }
      if (event.key === "Escape" && showTaskSearch) {
        event.preventDefault();
        setShowTaskSearch(false);
        setTaskQuery("");
        return;
      }
      if (event.key === "Escape" && showTaskInspector) {
        event.preventDefault();
        setShowTaskInspector(false);
        return;
      }
      if (
        (event.ctrlKey || event.metaKey) &&
        event.altKey &&
        key === "i" &&
        task &&
        activeView === "task"
      ) {
        event.preventDefault();
        setShowTaskInspector((current) => !current);
        return;
      }
      if (
        (event.ctrlKey || event.metaKey) &&
        key === "j" &&
        task &&
        activeView === "task" &&
        !isEditableTarget(event.target)
      ) {
        event.preventDefault();
        setShowTaskTerminal((current) => !current);
        return;
      }
      if (
        (event.ctrlKey || event.metaKey) &&
        key === "n" &&
        !isEditableTarget(event.target)
      ) {
        event.preventDefault();
        void beginNewConversation();
      }
    }

    window.addEventListener("keydown", handleShortcut);
    return () => window.removeEventListener("keydown", handleShortcut);
  }, [
    activeView,
    showCommandPalette,
    showTaskInspector,
    showTaskTerminal,
    showTaskSearch,
    task,
    taskBlocksNewTurn,
  ]);

  async function addProject(path: string) {
    if (!path.trim()) return;
    setRequestError("");
    try {
      const response = await fetch(
        `${API}/projects?path=${encodeURIComponent(path.trim())}`,
        { method: "POST" },
      );
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "项目注册失败");
      const project = payload.project as Project;
      setProjectId(project.project_id);
      if (conversation?.conversation_id) {
        const association = await fetch(
          `${API}/conversations/${encodeURIComponent(conversation.conversation_id)}`,
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ project_id: project.project_id }),
          },
        );
        if (!association.ok) throw new Error("项目已添加，但当前对话未能关联。");
        const associationPayload = (await association.json()) as { conversation: Conversation };
        activateConversation(associationPayload.conversation);
        await loadConversations(showArchived);
      }
      await loadProjects();
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "项目注册失败");
    }
  }

  function openSidebarMenu(
    event: MouseEvent<HTMLElement>,
    kind: SidebarMenu["kind"],
    id: string,
  ) {
    event.preventDefault();
    setSidebarMenu({ kind, id, x: event.clientX, y: event.clientY });
  }

  function openRename(target: RenameTarget) {
    setSidebarMenu(null);
    setRenameTarget(target);
    setRenameValue(target.title);
  }

  async function submitRename() {
    if (!renameTarget || !renameValue.trim()) return;
    setRequestError("");
    try {
      const path = renameTarget.kind === "project"
        ? `${API}/projects/${encodeURIComponent(renameTarget.id)}`
        : renameTarget.kind === "task"
          ? `${API}/tasks/${encodeURIComponent(renameTarget.id)}`
          : `${API}/conversations/${encodeURIComponent(renameTarget.id)}`;
      const field = renameTarget.kind === "project" ? "display_name" : "display_title";
      const response = await fetch(path, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [field]: renameValue.trim() }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "重命名失败");
      if (renameTarget.kind === "project") await loadProjects();
      if (renameTarget.kind === "task") {
        const updated = payload.task as Task;
        setTask((current) => current?.thread_id === updated.thread_id ? { ...current, ...updated } : current);
        await loadTasks(showArchived);
      }
      if (renameTarget.kind === "conversation") {
        const updated = payload.conversation as Conversation;
        setConversation((current) => current?.conversation_id === updated.conversation_id ? updated : current);
        await loadConversations(showArchived);
      }
      setRenameTarget(null);
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "重命名失败");
    }
  }

  async function archiveProject(targetProjectId: string) {
    setSidebarMenu(null);
    try {
      const response = await fetch(`${API}/projects/${encodeURIComponent(targetProjectId)}/archive`, { method: "POST" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "项目归档失败");
      if (projectId === targetProjectId) beginNewTask();
      await loadProjects();
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "项目归档失败");
    }
  }

  async function archiveConversation(conversationId: string) {
    setSidebarMenu(null);
    try {
      const response = await fetch(`${API}/conversations/${encodeURIComponent(conversationId)}/archive`, { method: "POST" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "对话归档失败");
      if (conversation?.conversation_id === conversationId) beginNewTask();
      await loadConversations(showArchived);
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "对话归档失败");
    }
  }

  async function moveConversation(conversationId: string, nextProjectId: string | null) {
    setSidebarMenu(null);
    try {
      const response = await fetch(`${API}/conversations/${encodeURIComponent(conversationId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: nextProjectId }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "对话归属更新失败");
      const updated = payload.conversation as Conversation;
      setConversation((current) => current?.conversation_id === updated.conversation_id ? updated : current);
      await loadConversations(showArchived);
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "对话归属更新失败");
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
      await loadCapabilityDirectory(projectId);
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
      await loadCapabilityDirectory(projectId);
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

  function applyChatModelPreset(preset: ChatModelPreset) {
    setChatBaseUrl(preset.baseUrl);
    setChatModel(preset.model);
    setRuntimeConfigurationMessage("已填入模型预设；请自行输入 API Key 后保存。预设不会读取或修改已有密钥。");
  }

  function applyEmbeddingModelPreset(preset: EmbeddingModelPreset) {
    setEmbeddingBaseUrl(preset.baseUrl);
    setEmbeddingModel(preset.model);
    setEmbeddingDimensions(preset.dimensions);
    setRuntimeConfigurationMessage("已填入 Embedding 预设；请自行输入 API Key 后保存。预设不会读取或修改已有密钥。");
  }

  async function chooseProjectDirectory() {
    try {
      const selected = await open({
        directory: true,
        multiple: false,
        title: "选择 RepoPilot 项目目录",
      });
      if (typeof selected === "string") {
        await addProject(selected);
      }
    } catch {
      setRequestError(
        "系统目录选择器仅在已安装的 RepoPilot Desktop 中可用；浏览器调试时请手动输入路径。",
      );
    }
  }

  async function chooseDocument() {
    if (!projectId) {
      setRequestError("请先为当前对话选择一个项目，再上传研发文档。");
      return;
    }
    try {
      const selected = await open({
        multiple: false,
        title: "选择研发文档",
        filters: [{ name: "Markdown 或文本", extensions: ["md", "txt"] }],
      });
      if (typeof selected === "string") await indexDocument(selected, true);
    } catch {
      setRequestError(
        "系统文件选择器仅在已安装的 RepoPilot Desktop 中可用；浏览器调试时请手动输入 MD/TXT 路径。",
      );
    }
  }

  async function indexDocument(file: string, attachToCurrentTask = true) {
    if (!projectId || !file.trim()) return;
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
          body: JSON.stringify({ file: file.trim() }),
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
      taskBlocksNewTurn ||
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
      let activeConversation = conversation;
      if (!activeConversation) {
        const created = await fetch(`${API}/conversations`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            project_id: projectId,
            display_title: description.trim().slice(0, 80),
            mode: operation === "research" ? "plan" : "goal",
          }),
        });
        const createdPayload = await created.json();
        if (!created.ok) {
          throw new Error(createdPayload.detail ?? "无法创建任务会话");
        }
        activeConversation = createdPayload.conversation as Conversation;
        activateConversation(activeConversation);
      } else if (activeConversation.project_id !== projectId) {
        const associated = await fetch(
          `${API}/conversations/${encodeURIComponent(activeConversation.conversation_id)}`,
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ project_id: projectId }),
          },
        );
        const associatedPayload = await associated.json();
        if (!associated.ok) {
          throw new Error(associatedPayload.detail ?? "当前对话无法关联到选择的项目");
        }
        activeConversation = associatedPayload.conversation as Conversation;
        activateConversation(activeConversation);
      }
      const response = await fetch(`${API}/tasks`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project_id: projectId,
          conversation_id: activeConversation.conversation_id,
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
      setDescription("");
      setAttachedDocumentIds([]);
      summarizedTaskRef.current = null;
      setActiveView("task");
      await Promise.all([
        loadTasks(),
        loadConversations(showArchived),
        loadConversationMessages(activeConversation.conversation_id),
      ]);
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "任务创建失败");
    }
  }

  async function sendChat() {
    if (chatBusy || taskBlocksNewTurn || !description.trim()) return;
    const submittedContent = description.trim();
    setChatBusy(true);
    setRequestError("");
    try {
      let activeConversation = conversation;
      if (!activeConversation) {
        const created = await fetch(`${API}/conversations`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            project_id: projectId || null,
            display_title: submittedContent.slice(0, 80),
            mode: "goal",
          }),
        });
        const createdPayload = await created.json();
        if (!created.ok) {
          throw new Error(createdPayload.detail ?? "无法创建对话");
        }
        activeConversation = createdPayload.conversation as Conversation;
        activateConversation(activeConversation);
      }
      setDescription("");
      setStreamingChat({ conversationId: activeConversation.conversation_id, content: "" });
      const response = await fetch(
        `${API}/conversations/${encodeURIComponent(activeConversation.conversation_id)}/chat/stream`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content: submittedContent }),
        },
      );
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(typeof payload.detail === "string" ? payload.detail : "普通对话失败");
      }
      if (!response.body) throw new Error("浏览器不支持对话流式响应");

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let completed = false;
      const appendMessage = (message: ConversationMessage) => {
        setConversationMessages((current) =>
          current.some((item) => item.message_id === message.message_id)
            ? current
            : [...current, message],
        );
      };
      const consumeEvent = (frame: string) => {
        const eventName = frame.match(/^event:\s*(.+)$/m)?.[1]?.trim() ?? "message";
        const data = frame
          .split("\n")
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart())
          .join("\n");
        if (!data) return;
        const payload = JSON.parse(data) as Record<string, unknown>;
        if (eventName === "message" && payload.message && typeof payload.message === "object") {
          appendMessage(payload.message as ConversationMessage);
          return;
        }
        if (eventName === "delta" && typeof payload.content === "string") {
          setStreamingChat((current) =>
            current?.conversationId === activeConversation?.conversation_id
              ? { ...current, content: current.content + payload.content }
              : current,
          );
          return;
        }
        if (eventName === "done") {
          if (payload.message && typeof payload.message === "object") {
            appendMessage(payload.message as ConversationMessage);
          }
          if (payload.context && typeof payload.context === "object") {
            setConversationContext(payload.context as ConversationContextState);
          }
          completed = true;
          return;
        }
        if (eventName === "error") {
          if (payload.message_record && typeof payload.message_record === "object") {
            appendMessage(payload.message_record as ConversationMessage);
          }
          const message = typeof payload.message === "string" ? payload.message : "普通对话流式响应中断";
          throw new Error(message);
        }
      };
      while (true) {
        const { value, done } = await reader.read();
        if (value) {
          buffer += decoder.decode(value, { stream: !done });
          const frames = buffer.split("\n\n");
          buffer = frames.pop() ?? "";
          for (const frame of frames) consumeEvent(frame);
        }
        if (done) break;
      }
      if (buffer.trim()) consumeEvent(buffer);
      if (!completed) throw new Error("对话流式响应未正常结束");
      await loadConversations(showArchived);
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "普通对话失败");
    } finally {
      setStreamingChat(null);
      setChatBusy(false);
    }
  }

  function sendComposerMessage() {
    if (composerMode === "chat") {
      void sendChat();
      return;
    }
    void start();
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
      if (decision === "approve") setExecutionApprovalConfirmation(false);
      const nextTask = payload as Task;
      setTask(nextTask);

      // 完全本机控制已由任务级确认语句冻结授权；此处仅自动继续图内的
      // 第二个内部确认点，不会放宽 PolicyGuard、工具白名单或路径边界。
      if (
        decision === "approve" &&
        nextTask.task_mode === "full-local" &&
        nextTask.task_operation === "change" &&
        nextTask.pending_approval
      ) {
        const continuation = await fetch(
          `${API}/tasks/${nextTask.thread_id}/approval`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ decision: "approve", comment: null }),
          },
        );
        const continuationPayload = await continuation.json();
        if (!continuation.ok) {
          throw new Error(
            typeof continuationPayload.detail === "string"
              ? continuationPayload.detail
              : "无法继续执行已授权的任务",
          );
        }
        setTask(continuationPayload as Task);
      }
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
    await archiveTaskById(task.thread_id);
  }

  async function archiveTaskById(threadId: string) {
    setSidebarMenu(null);
    try {
      const response = await fetch(`${API}/tasks/${encodeURIComponent(threadId)}`, {
        method: "DELETE",
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "任务归档失败");
      const updated = payload.task as Task;
      setTask((current) => current?.thread_id === updated.thread_id ? updated : current);
      await loadTasks(showArchived);
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "任务归档失败");
    }
  }

  async function beginNewConversation(project: string | null = projectId || null) {
    const conversationMode: ConversationMode = operation === "research" ? "plan" : "goal";
    beginNewTask();
    setComposerMode("chat");
    setRequestError("");
    try {
      const response = await fetch(`${API}/conversations`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project_id: project,
          mode: conversationMode,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "新建对话失败");
      const created = payload.conversation as Conversation;
      activateConversation(created);
      setProjectId(created.project_id ?? "");
      setOperation(created.mode === "plan" ? "research" : "change");
      await loadConversations(showArchived);
      window.requestAnimationFrame(() => taskDescriptionRef.current?.focus());
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "新建对话失败");
    }
  }

  function beginNewTask() {
    conversationMessageRequestRef.current += 1;
    taskSelectionRequestRef.current += 1;
    setTask(null);
    activateConversation(null);
    setConversationMessages([]);
    setConversationContext(null);
    setConversationMessagesLoading(false);
    setStreamingChat(null);
    summarizedTaskRef.current = null;
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
    setShowTaskTerminal(false);
  }

  function openRuntimeConfiguration() {
    setActiveView("settings");
    void loadRuntimeConfiguration().catch(() =>
      setRequestError("无法读取运行配置，请检查本地 API。"),
    );
  }

  function switchProject(nextProjectId: string) {
    if (
      (task?.project_id && task.project_id !== nextProjectId) ||
      (conversation?.project_id && conversation.project_id !== nextProjectId)
    ) {
      beginNewTask();
    }
    setProjectId(nextProjectId);
    setApprovedMcpTools([]);
    setMcpResult(null);
    setConfirmed(false);
  }

  function applyTaskStarter(value: string) {
    setDescription(value);
    setRequestError("");
    window.requestAnimationFrame(() => taskDescriptionRef.current?.focus());
  }

  async function selectTask(
    selected: Task,
    view: WorkspaceView = "task",
    preserveConversation = false,
  ) {
    const selectionRequest = ++taskSelectionRequestRef.current;
    setRequestError("");
    setEvents([]);
    try {
      const response = await fetch(`${API}/tasks/${selected.thread_id}`);
      if (!response.ok) throw new Error("任务详情不可恢复");
      const snapshot = (await response.json()) as Task;
      if (taskSelectionRequestRef.current !== selectionRequest) return;
      const merged = { ...selected, ...snapshot };
      setTask(merged);
      if (!preserveConversation) {
        const linkedConversation = merged.conversation_id
          ? conversations.find(
              (item) => item.conversation_id === merged.conversation_id,
            ) ?? null
          : null;
        activateConversation(linkedConversation);
        if (linkedConversation) {
          await loadConversationMessages(linkedConversation.conversation_id);
        } else {
          setConversationMessages([]);
          setConversationContext(null);
        }
      }
      setProjectId(merged.project_id ?? projectId);
      if (merged.task_mode === "safe-isolated" || merged.task_mode === "full-local") {
        setMode(merged.task_mode);
        setConfirmed(merged.task_mode === "full-local");
      }
      setOperation(resolvedTaskOperation(merged));
      setActiveView(view);
    } catch (error) {
      setRequestError(
        error instanceof Error ? error.message : "任务详情不可恢复",
      );
    }
  }

  async function selectConversation(selected: Conversation) {
    taskSelectionRequestRef.current += 1;
    setTask(null);
    activateConversation(selected);
    setConversationMessages([]);
    setConversationContext(null);
    setStreamingChat(null);
    setEvents([]);
    setArtifacts([]);
    setArtifactVersions([]);
    setSelectedArtifact("");
    setSelectedArtifactVersion(null);
    setArtifactContent("");
    setContextSnapshot(null);
    setTaskAttachments([]);
    setTelemetry(null);
    setProjectId(selected.project_id ?? "");
    setOperation(selected.mode === "plan" ? "research" : "change");
    setComposerMode("chat");
    setDescription("");
    setRequestError("");
    setActiveView("task");
    setShowTaskTerminal(false);
    try {
      const payload = await loadConversationMessages(selected.conversation_id);
      if (
        !payload ||
        selectedConversationIdRef.current !== selected.conversation_id
      ) {
        return;
      }
      const latestThreadId = [...(payload.messages ?? [])]
        .reverse()
        .find((item) => item.task_thread_id)?.task_thread_id;
      if (latestThreadId) {
        const latestTask = tasks.find((item) => item.thread_id === latestThreadId) ?? {
          thread_id: latestThreadId,
          status: "RUNNING",
          pending_approval: false,
        };
        await selectTask(latestTask, "task", true);
      }
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "无法读取对话记录");
    }
  }

  function selectOperation(nextOperation: Operation) {
    const fullLocalOperations = projectDiagnosis?.task_modes.full_local.allowed_operations ?? [];
    if (!allowedOperations.includes(nextOperation) && fullLocalOperations.includes(nextOperation)) {
      setMode("full-local");
      setConfirmed(false);
    }
    setOperation(nextOperation);
    setComposerMode(nextOperation);
    if (!conversation) return;
    void fetch(`${API}/conversations/${encodeURIComponent(conversation.conversation_id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: nextOperation === "research" ? "plan" : "goal" }),
    })
      .then(async (response) => {
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail ?? "对话模式更新失败");
        const updated = payload.conversation as Conversation;
        activateConversation(updated);
        await loadConversations(showArchived);
      })
      .catch((error: unknown) =>
        setRequestError(error instanceof Error ? error.message : "对话模式更新失败"),
      );
  }

  const interrupt = task?.interrupts?.[0];
  const currentProject = projects.find((item) => item.project_id === projectId);
  const discoveredMcpCapabilities: CapabilityDirectoryItem[] = (
    mcpResult?.connection?.tools ?? []
  ).map((tool) => ({
    capability_id: tool.capability_id,
    name: tool.capability_id,
    description: tool.description,
    kind: "mcp_tool",
    scope: "project",
    source_label: "当前项目 MCP",
    risks: tool.risks,
    enabled: true,
    details: {},
    discovered_mcp: true,
  }));
  const capabilityItems = [
    ...(capabilityDirectory?.capabilities ?? []),
    ...discoveredMcpCapabilities,
  ].filter((item) => capabilityFilter === "all" || item.kind === capabilityFilter);
  const frozenCapabilityIds = new Set(contextSnapshot?.capability_ids ?? []);
  const frozenToolIds = new Set(contextSnapshot?.bound_tool_ids ?? []);
  const fullAccessConfirmed =
    task?.task_mode === "full-local" || (!task && mode === "full-local" && confirmed);

  function capabilityState(item: CapabilityDirectoryItem) {
    if (item.discovered_mcp) {
      return approvedMcpTools.includes(item.capability_id)
        ? { label: "待任务冻结", tone: "pending", detail: "创建任务后由 PolicyGuard 再次裁决。" }
        : { label: "待选择", tone: "neutral", detail: "仅已发现，尚未加入任何任务。" };
    }
    if (!item.enabled) return { label: "已禁用", tone: "blocked", detail: "能力目录已将该能力禁用。" };
    if (frozenToolIds.has(item.capability_id) || frozenCapabilityIds.has(item.capability_id)) {
      return { label: "已冻结", tone: "ready", detail: "已进入当前任务快照，不能由模型自行升级。" };
    }
    if (mode === "full-local" && !fullAccessConfirmed) {
      return { label: "待确认", tone: "pending", detail: "需先完成完全本机控制确认，才会按完全权限策略创建任务。" };
    }
    const policy = fullAccessConfirmed ? item.full_policy : item.safe_policy;
    if (policy?.allowed) return { label: "可用", tone: "ready", detail: policy.reason };
    if (policy?.requires_approval) return { label: "需审批", tone: "pending", detail: policy.reason };
    return { label: "已阻断", tone: "blocked", detail: policy?.reason ?? "当前策略不允许此能力。" };
  }

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
  const fullLocalOperations = projectDiagnosis?.task_modes.full_local.allowed_operations ?? [];
  const canSelectResearch = allowedOperations.includes("research") || fullLocalOperations.includes("research");
  const canSelectChange = allowedOperations.includes("change") || fullLocalOperations.includes("change");
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
  const taskIsRunning = taskBlocksNewTurn;
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
  const hasGitDiff = artifacts.some((artifact) => artifact.kind === "git_diff");
  const hasVerification = artifacts.some((artifact) => artifact.kind === "verification");
  const hasPlan = artifacts.some((artifact) => artifact.kind === "plan_markdown");
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
  const inspectorEvidence = keyEvidenceEvents.slice(-5).reverse().map((event) => ({
    id: event.id,
    label: eventLabels[event.type] ?? event.type,
    summary: eventSummary(event),
  }));
  const activeTaskOperation = task ? resolvedTaskOperation(task) : operation;
  const activeTaskMode: Mode =
    task?.task_mode === "full-local" ? "full-local" : "safe-isolated";
  const displayedTaskDescription =
    task?.task_description ??
    task?.state?.task_description ??
    (task ? task.display_title : description.trim());
  const currentTaskRequestPersisted = Boolean(
    task &&
      conversationMessages.some(
        (item) =>
          item.task_thread_id === task.thread_id && item.kind === "task_request",
      ),
  );
  const currentTaskSummaryPersisted = Boolean(
    task &&
      conversationMessages.some(
        (item) =>
          item.task_thread_id === task.thread_id && item.kind === "task_summary",
      ),
  );
  const showLiveTaskResponse = Boolean(
    task && (taskBlocksNewTurn || !currentTaskSummaryPersisted),
  );
  const activeStreamingChat =
    streamingChat?.conversationId === conversation?.conversation_id
      ? streamingChat
      : null;
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
    !taskBlocksNewTurn &&
    Boolean(projectId && description.trim()) &&
    runtimeHealth.status === "READY" &&
    operationAllowed &&
    !(safeModeBlockedByProject && mode === "safe-isolated") &&
    !(mode === "full-local" && !confirmed);
  const canSubmit = composerMode === "chat"
    ? !taskBlocksNewTurn && Boolean(description.trim()) && !chatBusy
    : canStart;
  const commandItems: CommandPaletteItem[] = [
    {
      id: "new-conversation",
      group: "操作",
      label: "新建对话",
      description: currentProject ? `在 ${currentProject.display_name} 中开始` : "可先不关联项目",
      icon: <Plus size={16} />,
      shortcut: "Ctrl+N",
      keywords: "new task 新建会话",
      onSelect: () => {
        void beginNewConversation();
      },
    },
    {
      id: "search-tasks",
      group: "操作",
      label: "搜索历史任务",
      description: "在左侧项目树中筛选任务",
      icon: <MagnifyingGlass size={16} />,
      keywords: "find history 搜索历史",
      onSelect: () => {
        setShowTaskSearch(true);
        window.requestAnimationFrame(() => taskSearchRef.current?.focus());
      },
    },
    {
      id: "view-task",
      group: "视图",
      label: "Agent 会话",
      description: "返回任务进度、审批和输入区",
      icon: <ChatCircle size={16} />,
      onSelect: () => setActiveView("task"),
    },
    {
      id: "view-context",
      group: "视图",
      label: "上下文与扩展",
      description: "查看文档、MCP、Skills、插件和任务上下文",
      icon: <Stack size={16} />,
      onSelect: () => setActiveView("context"),
    },
    {
      id: "view-settings",
      group: "视图",
      label: "设置",
      description: "管理本地模型、Embedding 与 Qdrant 连接配置",
      icon: <SlidersHorizontal size={16} />,
      keywords: "settings api key model embedding qdrant 配置",
      onSelect: openRuntimeConfiguration,
    },
    {
      id: "view-review",
      group: "视图",
      label: "证据与产物",
      description: task ? "审阅计划、Diff、验证和审计证据" : "选择任务后可用",
      icon: <FileCode size={16} />,
      disabled: !task,
      onSelect: () => setActiveView("review"),
    },
    {
      id: "toggle-task-inspector",
      group: "视图",
      label: showTaskInspector ? "关闭任务检查器" : "打开任务检查器",
      description: task ? "在会话右侧查看状态、上下文、证据和产物" : "选择任务后可用",
      icon: <SidebarSimple size={16} />,
      shortcut: "Ctrl+Alt+I",
      disabled: !task,
      keywords: "inspector sidebar 检查器 侧栏",
      onSelect: () => {
        setActiveView("task");
        setShowTaskInspector((current) => !current);
      },
    },
    {
      id: "toggle-task-terminal",
      group: "视图",
      label: showTaskTerminal ? "关闭受控终端" : "打开受控终端",
      description: task ? "在底部运行已注册的只读任务查询" : "选择任务后可用",
      icon: <TerminalWindow size={16} />,
      shortcut: "Ctrl+J",
      disabled: !task,
      keywords: "terminal cli status review artifacts 终端",
      onSelect: () => {
        setActiveView("task");
        setShowTaskTerminal((current) => !current);
      },
    },
    {
      id: "refresh-runtime",
      group: "系统",
      label: "刷新本机状态",
      description: "重新读取项目、任务与 Agent 依赖",
      icon: <ArrowClockwise size={16} />,
      keywords: "refresh reload doctor 状态",
      onSelect: () => {
        void Promise.all([loadProjects(), loadTasks(), checkApiHealth()]).catch(() =>
          setRequestError(API_UNAVAILABLE_MESSAGE),
        );
      },
    },
    {
      id: "toggle-archive",
      group: "系统",
      label: showArchived ? "隐藏归档任务" : "显示归档任务",
      description: "切换左侧任务树的归档可见性",
      icon: <Archive size={16} />,
      onSelect: () => setShowArchived((current) => !current),
    },
    ...projects.map((project) => ({
      id: `project-${project.project_id}`,
      group: "项目",
      label: project.display_name,
      description: project.is_git_repository ? "Git 项目" : "非 Git 项目，仅支持完整本机研究",
      icon: <FolderOpen size={16} />,
      keywords: project.project_id,
      onSelect: () => switchProject(project.project_id),
    })),
    ...tasks.slice(0, 20).map((item) => ({
      id: `task-${item.thread_id}`,
      group: "最近任务",
      label: compactTaskLabel(item),
      description: `${taskStateLabel(item.status, item.verdict, item.pending_approval)} · ${item.task_mode === "full-local" ? "完全本机" : "安全隔离"}`,
      icon: <TerminalWindow size={16} />,
      keywords: `${item.thread_id} ${item.project_id ?? ""}`,
      onSelect: () => void selectTask(item),
    })),
  ];
  const terminalCommands: TerminalCommand[] = task
    ? [
        {
          id: "status",
          label: "查看状态",
          description: "读取当前阶段与执行状态",
          command: `repopilot-guard task status --thread-id ${task.thread_id}`,
        },
        {
          id: "events",
          label: "查看证据",
          description: "读取近期审计证据摘要",
          command: `repopilot-guard task events --thread-id ${task.thread_id}`,
        },
        {
          id: "review",
          label: "审阅任务",
          description: "读取计划、审批与验证结论",
          command: `repopilot-guard task review --thread-id ${task.thread_id}`,
        },
        {
          id: "artifacts",
          label: "查看产物",
          description: "读取 Diff、报告等产物清单",
          command: `repopilot-guard task artifacts --thread-id ${task.thread_id}`,
        },
      ]
    : [];

  return (
    <main className="product-shell">
      <aside className="navigation-pane">
        <div className="product-brand">
          <span className="brand-menu"><strong>RepoPilot</strong></span>
        </div>

        <nav className="primary-navigation" aria-label="工作台入口">
          <button type="button" onClick={() => void beginNewConversation()}>
            <Plus size={17} />
            <span>新建对话</span>
            <small>Ctrl+N</small>
          </button>
          <button
            className={showTaskSearch ? "active" : ""}
            type="button"
            onClick={() => setShowTaskSearch((current) => !current)}
          >
            <ListMagnifyingGlass size={17} />
            <span>搜索任务</span>
          </button>
          <button
            className={activeView === "context" ? "active" : ""}
            type="button"
            onClick={() => setActiveView("context")}
          >
            <PuzzlePiece size={17} />
            <span>上下文与扩展</span>
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
            {projects.length === 0 && <p className="sidebar-empty">选择本地文件夹后会自动加入这里</p>}
            {projects.map((project) => {
              const projectTasks = tasks.filter(
                (item) =>
                  item.project_id === project.project_id &&
                  (!taskQuery.trim() ||
                    compactTaskLabel(item)
                      .toLocaleLowerCase()
                      .includes(taskQuery.trim().toLocaleLowerCase())),
              );
              const projectConversations = conversations.filter(
                (item) => item.project_id === project.project_id,
              );
              const selected = project.project_id === projectId;
              return (
                <section className="project-node" key={project.project_id}>
                  <div
                    className={selected ? "project-row selected" : "project-row"}
                    onContextMenu={(event) => openSidebarMenu(event, "project", project.project_id)}
                  >
                    <button
                      className="project-select"
                      type="button"
                      onClick={() => switchProject(project.project_id)}
                    >
                      <FolderOpen size={16} weight={selected ? "fill" : "regular"} />
                      <span>{project.display_name}</span>
                      <small>{project.is_git_repository ? "Git" : "非 Git"}</small>
                    </button>
                    <button
                      className="tree-menu-button"
                      type="button"
                      title={`${project.display_name} 操作`}
                      aria-label={`${project.display_name} 操作`}
                      onClick={(event) => openSidebarMenu(event, "project", project.project_id)}
                    >
                      <DotsThree size={17} weight="bold" />
                    </button>
                  </div>
                  {selected && (
                    <div className="project-task-list">
                      {projectConversations.map((item) => (
                        <div
                          className={conversation?.conversation_id === item.conversation_id ? "tree-row active" : "tree-row"}
                          key={item.conversation_id}
                          onContextMenu={(event) => openSidebarMenu(event, "conversation", item.conversation_id)}
                        >
                          <button type="button" onClick={() => void selectConversation(item)}>
                            <ChatCircle size={15} />
                            <span>{item.display_title}</span>
                            <small>{item.mode === "plan" ? "计划" : "目标"}</small>
                          </button>
                          <button className="tree-menu-button" type="button" title={`${item.display_title} 操作`} onClick={(event) => openSidebarMenu(event, "conversation", item.conversation_id)}>
                            <DotsThree size={16} weight="bold" />
                          </button>
                        </div>
                      ))}
                      {projectTasks.map((item) => (
                        <div
                          className={task?.thread_id === item.thread_id ? "tree-row active" : "tree-row"}
                          key={item.thread_id}
                          onContextMenu={(event) => openSidebarMenu(event, "task", item.thread_id)}
                        >
                          <button type="button" onClick={() => void selectTask(item)}>
                            <ChatCircle size={15} />
                            <span>{compactTaskLabel(item)}</span>
                            <i className={"task-dot status-" + item.status.toLowerCase()} />
                          </button>
                          <button className="tree-menu-button" type="button" title={`${compactTaskLabel(item)} 操作`} onClick={(event) => openSidebarMenu(event, "task", item.thread_id)}>
                            <DotsThree size={16} weight="bold" />
                          </button>
                        </div>
                      ))}
                      {projectTasks.length === 0 && projectConversations.length === 0 && <p>还没有对话</p>}
                    </div>
                  )}
                </section>
              );
            })}
          </div>

          <section className="recent-task-navigation" aria-label="未归属对话">
            <div className="navigation-heading">
              <span>未归属对话</span>
            </div>
            <div className="recent-task-list">
              {conversations.filter((item) => !item.project_id).length === 0 && <p className="sidebar-empty">可先新建不关联项目的对话</p>}
              {conversations.filter((item) => !item.project_id).map((item) => (
                <div
                  className={conversation?.conversation_id === item.conversation_id ? "recent-task-row active" : "recent-task-row"}
                  key={item.conversation_id}
                  onContextMenu={(event) => openSidebarMenu(event, "conversation", item.conversation_id)}
                >
                  <button type="button" onClick={() => void selectConversation(item)}>
                    <ChatCircle size={15} />
                    <span>
                      <strong>{item.display_title}</strong>
                      <small>{item.mode === "plan" ? "计划模式" : "目标模式"} · 未关联项目</small>
                    </span>
                  </button>
                  <button className="tree-menu-button" type="button" title={`${item.display_title} 操作`} onClick={(event) => openSidebarMenu(event, "conversation", item.conversation_id)}>
                    <DotsThree size={16} weight="bold" />
                  </button>
                </div>
              ))}
            </div>
          </section>
        </section>

        <div className="navigation-footer">
          <label className="archive-filter">
            <input type="checkbox" checked={showArchived} onChange={(event) => setShowArchived(event.target.checked)} />
            显示归档
          </label>
          <button className={activeView === "settings" ? "navigation-settings active" : "navigation-settings"} type="button" onClick={openRuntimeConfiguration}>
            <SlidersHorizontal size={16} />
            <span>设置与运行配置</span>
          </button>
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
      {sidebarMenu && (
        <>
          <button className="sidebar-menu-backdrop" type="button" aria-label="关闭操作菜单" onClick={() => setSidebarMenu(null)} />
          <section
            className="sidebar-context-menu"
            role="menu"
            aria-label="项目或对话操作"
            style={{ left: Math.min(sidebarMenu.x, window.innerWidth - 214), top: Math.min(sidebarMenu.y, window.innerHeight - 210) }}
          >
            {sidebarMenu.kind === "project" && (() => {
              const item = projects.find((project) => project.project_id === sidebarMenu.id);
              if (!item) return null;
              return <>
                <button type="button" role="menuitem" onClick={() => void beginNewConversation(item.project_id)}><Plus size={16} />在此项目中新建对话</button>
                <button type="button" role="menuitem" onClick={() => openRename({ kind: "project", id: item.project_id, title: item.display_name })}><PencilSimple size={16} />重命名项目</button>
                <button type="button" role="menuitem" className="menu-danger" onClick={() => void archiveProject(item.project_id)}><Archive size={16} />归档项目</button>
              </>;
            })()}
            {sidebarMenu.kind === "conversation" && (() => {
              const item = conversations.find((conversation) => conversation.conversation_id === sidebarMenu.id);
              if (!item) return null;
              return <>
                <button type="button" role="menuitem" onClick={() => openRename({ kind: "conversation", id: item.conversation_id, title: item.display_title })}><PencilSimple size={16} />重命名对话</button>
                {item.project_id ? (
                  <button type="button" role="menuitem" onClick={() => void moveConversation(item.conversation_id, null)}><FolderOpen size={16} />移至未归属对话</button>
                ) : currentProject ? (
                  <button type="button" role="menuitem" onClick={() => void moveConversation(item.conversation_id, currentProject.project_id)}><FolderOpen size={16} />关联到当前项目</button>
                ) : null}
                <button type="button" role="menuitem" className="menu-danger" onClick={() => void archiveConversation(item.conversation_id)}><Archive size={16} />归档对话</button>
              </>;
            })()}
            {sidebarMenu.kind === "task" && (() => {
              const item = tasks.find((taskItem) => taskItem.thread_id === sidebarMenu.id);
              if (!item) return null;
              const running = !["REPORT", "FAILED", "PASSED", "BLOCKED", "CANCELLED", "UNVERIFIED"].includes(item.status);
              return <>
                <button type="button" role="menuitem" onClick={() => openRename({ kind: "task", id: item.thread_id, title: compactTaskLabel(item) })}><PencilSimple size={16} />重命名对话</button>
                <button type="button" role="menuitem" className="menu-danger" disabled={running || Boolean(item.archived_at)} onClick={() => void archiveTaskById(item.thread_id)}><Archive size={16} />{running ? "运行中，暂不能归档" : "归档对话"}</button>
              </>;
            })()}
          </section>
        </>
      )}
      {renameTarget && (
        <div className="rename-dialog-backdrop" role="presentation">
          <section className="rename-dialog" role="dialog" aria-modal="true" aria-labelledby="rename-dialog-title">
            <header><PencilSimple size={19} /><div><h2 id="rename-dialog-title">重命名{renameTarget.kind === "project" ? "项目" : "对话"}</h2><p>仅更新侧栏显示名称，不会修改文件、任务证据或 Git 历史。</p></div></header>
            <input value={renameValue} maxLength={80} autoFocus aria-label="新名称" onChange={(event) => setRenameValue(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void submitRename(); }} />
            <footer>
              <button className="secondary-button" type="button" onClick={() => setRenameTarget(null)}>取消</button>
              <button className="primary-button" type="button" disabled={!renameValue.trim()} onClick={() => void submitRename()}>保存</button>
            </footer>
          </section>
        </div>
      )}

      <section className="workbench">
        <header className="workspace-header">
          <div className="workspace-identity">
            <FolderOpen size={18} />
            <strong title={currentProject?.display_name ?? "未关联项目"}>{currentProject?.display_name ?? "未关联项目"}</strong>
            {task && (
              <>
                <span>/</span>
                <small title={compactTaskLabel(task)}>{compactTaskLabel(task)}</small>
              </>
            )}
            {conversation && (
              <>
                <span>/</span>
                <small title={conversation.display_title}>{conversation.display_title}</small>
              </>
            )}
          </div>
          <div className="workspace-actions" aria-label="工作区视图">
            <button className={activeView === "review" ? "active" : ""} type="button" title="证据与产物" onClick={() => setActiveView("review")} disabled={!task}>
              <FileCode size={18} />
            </button>
            {task && activeView === "task" && (
              <button
                className={showTaskTerminal ? "active" : ""}
                type="button"
                title="受控终端 (Ctrl+J)"
                aria-label="切换受控终端"
                aria-pressed={showTaskTerminal}
                onClick={() => setShowTaskTerminal((current) => !current)}
              >
                <TerminalWindow size={18} />
              </button>
            )}
            {task && activeView === "task" && (
              <button
                className={showTaskInspector ? "active" : ""}
                type="button"
                title="任务检查器 (Ctrl+Alt+I)"
                aria-label="切换任务检查器"
                aria-pressed={showTaskInspector}
                onClick={() => setShowTaskInspector((current) => !current)}
              >
                <SidebarSimple size={18} />
              </button>
            )}
          </div>
        </header>

        {activeView === "task" && (
          <div
            className={[
              "task-workspace",
              showTaskInspector && task ? "inspector-open" : "",
              showTaskTerminal && task ? "terminal-open" : "",
            ].filter(Boolean).join(" ")}
          >
            <section className="session-view">
            <div className="conversation-scroll">
              <div className="conversation-column">
                {!task && conversationMessages.length === 0 && !conversationMessagesLoading && (
                  <div className="new-task-state">
                    <p className="new-task-kicker">当前工作区</p>
                    <h2>{operation === "research" ? "先制定可信计划" : "明确目标，持续推进"}</h2>
                    <p>
                      {currentProject
                        ? currentProject.display_name + "  ·  " + projectStatusLabel
                        : conversation
                          ? "此对话暂未关联项目。可以先整理目标或计划；开始代码任务前请选择项目。"
                          : "选择一个本地文件夹后，RepoPilot 会自动添加项目并开始分析。"}
                    </p>
                    {!currentProject && (
                      <button className="open-project-button" type="button" onClick={() => void chooseProjectDirectory()}>
                        <FolderOpen size={17} />{conversation ? "选择关联项目" : "打开本地项目"}
                      </button>
                    )}
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

                {conversationMessagesLoading && conversationMessages.length === 0 && (
                  <div className="conversation-loading" aria-live="polite">
                    <CircleNotch className="spin" size={18} />正在恢复对话记录
                  </div>
                )}

                {conversationContext?.compacted && (
                  <p className="conversation-context-notice">
                    早期会话已自动压缩为上下文摘要，原始对话仍保留。
                  </p>
                )}

                {conversationMessages.map((message) =>
                  message.role === "user" ? (
                    <article className="conversation-turn user-turn" key={message.message_id}>
                      <div>
                        <p>{message.content}</p>
                        {message.kind === "task_request" && <small>代码任务</small>}
                      </div>
                    </article>
                  ) : (
                    <article className={`conversation-turn assistant-turn ${message.kind === "task_summary" ? "conversation-summary-turn" : "conversation-chat-turn"}`} key={message.message_id}>
                      <div className="agent-response">
                        {message.kind === "task_summary" && (
                          <div className="agent-response-header">
                            <strong>RepoPilot</strong>
                            <span className="conversation-summary-status">
                              {message.task_verdict ?? message.task_status ?? "已结束"}
                            </span>
                          </div>
                        )}
                        {message.kind === "chat_response" ? (
                          <div className="conversation-chat-markdown">
                            <ReactMarkdown>{message.content}</ReactMarkdown>
                          </div>
                        ) : (
                          <p className="conversation-summary-content">{message.content}</p>
                        )}
                        {message.task_thread_id && (
                          <button
                            className="conversation-detail-button"
                            type="button"
                            onClick={() => {
                              const linkedTask = tasks.find(
                                (item) => item.thread_id === message.task_thread_id,
                              ) ?? {
                                thread_id: message.task_thread_id as string,
                                status: "REPORT",
                                pending_approval: false,
                              };
                              void selectTask(linkedTask, "task", true);
                            }}
                          >
                            <ListMagnifyingGlass size={15} />查看任务详情
                          </button>
                        )}
                      </div>
                    </article>
                  ),
                )}

                {activeStreamingChat && (
                  <article className="conversation-turn assistant-turn conversation-chat-turn streaming-chat-turn">
                    <div className="agent-response">
                      <div className="agent-response-header">
                        <strong>RepoPilot</strong>
                        <span className="streaming-answer-status" aria-live="polite">
                          <CircleNotch className="spin" size={13} />正在回复
                        </span>
                      </div>
                      <div className="conversation-chat-markdown streaming-chat-content">
                        {activeStreamingChat.content
                          ? <ReactMarkdown>{activeStreamingChat.content}</ReactMarkdown>
                          : <p>正在生成回答...</p>}
                      </div>
                    </div>
                  </article>
                )}

                {task && (
                  <>
                    {!currentTaskRequestPersisted && (
                      <article className="conversation-turn user-turn">
                        <span className="turn-avatar">你</span>
                        <div>
                          <p>{displayedTaskDescription || "继续任务 " + compactTaskLabel(task)}</p>
                          <small>
                            {activeTaskOperation === "research" ? "计划模式" : "目标模式"}
                            {" · "}
                            {activeTaskMode === "safe-isolated" ? "安全隔离修复" : "完全本机控制"}
                          </small>
                        </div>
                      </article>
                    )}
                    {showLiveTaskResponse && (
                    <article className="conversation-turn assistant-turn">
                      <span className="turn-avatar agent-avatar"><TerminalWindow size={15} weight="bold" /></span>
                      <div className="agent-response">
                        <div className="agent-response-header">
                          <strong>RepoPilot</strong>
                          {taskIsRunning && (
                            <span className={`event-stream-status stream-${eventStreamState}`} aria-label={eventStreamLabels[eventStreamState]}>
                              <i aria-hidden="true" />正在处理
                            </span>
                          )}
                        </div>
                        {taskOutcome && (
                          <div className="agent-goal-result" aria-live="polite">
                            <span className={"goal-result-icon outcome-" + taskOutcome.tone}>
                              {taskOutcome.tone === "success"
                                ? <CheckCircle size={20} weight="fill" />
                                : taskOutcome.tone === "neutral"
                                  ? <CircleNotch className={taskIsRunning ? "spin" : ""} size={20} />
                                  : <WarningCircle size={20} weight="fill" />}
                            </span>
                            <div>
                              <h2>{taskOutcome.title}</h2>
                              <p>{taskOutcome.detail}</p>
                            </div>
                          </div>
                        )}
                        <div className="goal-result-facts" aria-label="任务结果摘要">
                          {hasGitDiff && <span><CheckCircle size={14} weight="fill" />已生成真实代码修改</span>}
                          {hasVerification && <span><CheckCircle size={14} weight="fill" />已记录 Maven 验证</span>}
                          {hasPlan && !hasGitDiff && <span><FileCode size={14} />已生成处理方案</span>}
                          {!hasGitDiff && !hasVerification && !hasPlan && taskIsRunning && <span><CircleNotch className="spin" size={14} />正在分析代码上下文</span>}
                          {!hasGitDiff && !hasVerification && !hasPlan && !taskIsRunning && <span><WarningCircle size={14} weight="fill" />尚未生成可验证的修改</span>}
                        </div>
                        <div className="goal-result-actions">
                          {hasGitDiff && (
                            <button type="button" onClick={() => { setSelectedArtifact("git_diff"); setSelectedArtifactVersion(null); setActiveView("review"); }}>
                              <FileCode size={16} />查看修改
                            </button>
                          )}
                          {hasVerification && (
                            <button type="button" onClick={() => { setSelectedArtifact("verification"); setSelectedArtifactVersion(null); setActiveView("review"); }}>
                              <CheckCircle size={16} />查看验证
                            </button>
                          )}
                          {hasPlan && !hasGitDiff && (
                            <button type="button" onClick={() => { setSelectedArtifact("plan_markdown"); setSelectedArtifactVersion(null); setActiveView("review"); }}>
                              <ListMagnifyingGlass size={16} />查看处理方案
                            </button>
                          )}
                        </div>
                        {(taskIsRunning || visibleEvents.length > 0 || task.diagnostic) && (
                          <details className="execution-details">
                            <summary>{taskIsRunning ? "执行过程与工具调用" : `执行记录${visibleEvents.length ? `（${visibleEvents.length}）` : ""}`}</summary>
                            <div className="execution-details-body">
                              {task.progress && task.progress.stages.length > 0 && (
                                <TaskProgressTrail summary={task.progress.summary} stages={task.progress.stages} running={taskIsRunning} />
                              )}
                              {visibleEvents.length === 0 && taskIsRunning && (
                                <div className="activity-loading" aria-label="任务正在初始化"><span /><span /><span /></div>
                              )}
                              {visibleEvents.length > 0 && (
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
                              )}
                              <TaskDiagnosticPanel
                                diagnostic={task.diagnostic}
                                artifacts={artifacts}
                                onOpenArtifact={(kind) => { setSelectedArtifact(kind); setSelectedArtifactVersion(null); setActiveView("review"); }}
                                onOpenRuntimeConfiguration={openRuntimeConfiguration}
                              />
                            </div>
                          </details>
                        )}
                      </div>
                    </article>
                    )}
                  </>
                )}

                {task?.pending_approval && (
                  <article className="conversation-turn assistant-turn approval-turn">
                    <span className="turn-avatar agent-avatar"><TerminalWindow size={15} weight="bold" /></span>
                    <section className="inline-approval">
                    <div className="approval-heading">
                      <WarningCircle size={20} weight="fill" />
                      <div>
                        <strong>
                          {researchPlanApproval
                            ? "分析已完成，可以生成结论"
                            : interrupt?.type === "EXECUTION_APPROVAL_REQUIRED"
                            ? "准备开始修复与验证"
                            : "已定位修复方向"}
                        </strong>
                        <p>
                          {researchPlanApproval
                            ? "这是只读计划任务，继续后会整理结论，不会修改代码或运行 Maven。"
                            : executionApproval
                              ? "继续后将只在受控范围内写入代码并运行固定 Maven 验证。"
                              : "RepoPilot 已完成问题定位；你无需审阅计划，继续后会进入下一步处理。"}
                        </p>
                      </div>
                    </div>
                    <details className="approval-details">
                      <summary>查看本次处理范围</summary>
                      <div className="approval-scope" aria-label="本次审批范围">
                        <div className="approval-scope-facts">
                          <span><b>{executionApproval ? "写入范围" : "处理范围"}</b>{executionApproval ? "仅允许受控补丁写入候选文件" : "本次不会直接写入代码"}</span>
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
                          <ol className="approval-plan-steps">{approvalSteps.map((step, index) => <li key={index}>{step}</li>)}</ol>
                        )}
                      </div>
                    </details>
                    <div className="approval-buttons">
                      <button
                        className="primary-button"
                        type="button"
                        onClick={() => {
                          if (executionApproval) {
                            setExecutionApprovalConfirmation(true);
                            return;
                          }
                          void approve("approve");
                        }}
                        disabled={approvalBusy}
                      >
                        <CheckCircle size={16} weight="bold" />
                        {approvalBusy ? "正在继续" : researchPlanApproval ? "生成结论" : executionApproval ? "允许执行并验证" : activeTaskMode === "full-local" ? "继续实现目标" : "继续修复"}
                      </button>
                      <button className="danger-button" type="button" onClick={() => void approve("reject")} disabled={approvalBusy}>
                        <XCircle size={16} />停止任务
                      </button>
                    </div>
                    </section>
                  </article>
                )}
                {executionApprovalConfirmation && task?.pending_approval && executionApproval && (
                  <div className="approval-confirmation-backdrop" role="presentation">
                    <section
                      className="approval-confirmation"
                      role="dialog"
                      aria-modal="true"
                      aria-labelledby="execution-approval-title"
                      aria-describedby="execution-approval-description"
                    >
                      <header>
                        <WarningCircle size={21} weight="fill" aria-hidden="true" />
                        <div>
                          <span>执行审批</span>
                          <h2 id="execution-approval-title">确认允许受控执行</h2>
                        </div>
                      </header>
                      <p id="execution-approval-description">
                        RepoPilot 将只在当前任务工作区内应用结构化补丁，并按下方固定 Maven Recipe 验证。不会创建提交、推送代码或执行任意 Shell。
                      </p>
                      <dl>
                        <div><dt>可写入范围</dt><dd>{approvalCandidateFiles.length ? `${approvalCandidateFiles.length} 个候选文件` : "计划中的候选文件"}</dd></div>
                        <div><dt>Maven Recipe</dt><dd><code>{approvalRecipe}</code></dd></div>
                        {approvalTargetTest && <div><dt>目标测试</dt><dd><code>{approvalTargetTest}</code></dd></div>}
                      </dl>
                      <p className="approval-confirmation-note">拒绝会停止本次任务并保留已生成的计划与证据，不会写入代码。</p>
                      <footer>
                        <button className="secondary-button" type="button" onClick={() => setExecutionApprovalConfirmation(false)} disabled={approvalBusy}>
                          返回审阅
                        </button>
                        <button className="primary-button" type="button" onClick={() => void approve("approve")} disabled={approvalBusy}>
                          <CheckCircle size={16} weight="bold" />{approvalBusy ? "正在提交" : "确认并执行"}
                        </button>
                      </footer>
                    </section>
                  </div>
                )}
                <div ref={conversationEndRef} aria-hidden="true" />
              </div>
            </div>

            <div className="composer-region">
              {task && taskBlocksNewTurn ? (
                <div className="task-command-bar">
                  <span className="task-command-hint">
                    {task.pending_approval
                      ? "等待你的确认后继续。"
                      : taskIsRunning
                        ? "任务正在运行，可以随时停止。"
                        : "此任务已结束。"}
                  </span>
                  <div className="task-command-actions">
                    {artifacts.length > 0 && (
                      <button className="secondary-button" type="button" onClick={() => setActiveView("review")}>
                        <ListMagnifyingGlass size={16} />查看详情
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
                      <button className="primary-button" type="button" onClick={() => void beginNewConversation()}>
                        <Plus size={16} />新建对话
                      </button>
                    )}
                  </div>
                </div>
              ) : (
                <>
                  <div className="composer">
                    {(requestError ||
                      (composerMode !== "chat" && apiReady && runtimeHealth.status !== "READY") ||
                      (composerMode !== "chat" && Boolean(currentProject) && !operationAllowed) ||
                      (composerMode !== "chat" && mode === "safe-isolated" && safeModeBlockedByProject)) && (
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
                    {composerMode !== "chat" && mode === "full-local" && !confirmed && (
                      <label className="full-access-confirmation">
                        <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
                        <span><b>确认完全本机访问</b>Agent 将直接在当前项目目录中执行已实现的高风险操作。</span>
                      </label>
                    )}
                    {composerMode !== "chat" && attachedDocuments.length > 0 && (
                      <div className="attachment-row">
                        <FileArrowUp size={16} />
                        <span className="attachment-label">已加入当前上下文</span>
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
                    <textarea
                      ref={taskDescriptionRef}
                      value={description}
                      onChange={(event) => setDescription(event.target.value)}
                      onKeyDown={(event) => {
                        if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && canSubmit) {
                          event.preventDefault();
                          sendComposerMessage();
                        }
                      }}
                      placeholder={composerMode === "chat"
                        ? "向 RepoPilot 提问"
                        : composerMode === "research"
                          ? "描述要理解、定位或评估的代码问题"
                          : "描述最终想实现的代码目标"}
                      aria-label={composerMode === "chat" ? "对话消息" : "代码任务描述"}
                    />
                    <div className="composer-toolbar">
                      <div className="composer-tools">
                        <div className="operation-control" role="group" aria-label="输入模式">
                          <button
                            className={composerMode === "chat" ? "active" : ""}
                            type="button"
                            onClick={() => setComposerMode("chat")}
                            aria-label="对话模式"
                            aria-pressed={composerMode === "chat"}
                            title="普通对话，不读取仓库、不创建任务"
                          >
                            <span>对话</span>
                          </button>
                          <button
                            className={composerMode === "change" ? "active" : ""}
                            type="button"
                            disabled={!canSelectChange}
                            onClick={() => selectOperation("change")}
                            aria-label="修改代码模式"
                            aria-pressed={composerMode === "change"}
                            title="围绕一个代码目标完成计划、受控修改和 Maven 验证"
                          >
                            <Target size={15} />
                            <span>修改代码</span>
                          </button>
                          <button
                            className={composerMode === "research" ? "active" : ""}
                            type="button"
                            disabled={!canSelectResearch}
                            onClick={() => selectOperation("research")}
                            aria-label="分析代码模式"
                            aria-pressed={composerMode === "research"}
                            title="只研究代码并输出证据化计划，不写入文件、不运行 Maven"
                          >
                            <ListMagnifyingGlass size={15} />
                            <span>分析代码</span>
                          </button>
                        </div>
                        {composerMode !== "chat" && <button className="icon-button" type="button" title="上传 MD 或 TXT 并直接加入当前上下文" onClick={() => void chooseDocument()} disabled={!projectId || documentBusy || attachedDocumentIds.length >= 4}>
                          <Paperclip size={19} />
                        </button>}
                        {composerMode !== "chat" && <label className={"permission-control mode-" + mode}>
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
                                selectOperation(nextAllowed[0]);
                              }
                              if (nextMode === "safe-isolated") setConfirmed(false);
                            }}
                          >
                            <option value="safe-isolated" disabled={Boolean(safeModeReadiness && safeModeReadiness.status !== "READY")}>
                              安全隔离
                            </option>
                            <option value="full-local">完全本机</option>
                          </select>
                        </label>}
                      </div>
                      <button className="send-button" type="button" title={composerMode === "chat" ? "发送消息（Ctrl + Enter）" : "开始代码任务（Ctrl + Enter）"} onClick={sendComposerMessage} disabled={!canSubmit}>
                        <ArrowUp size={19} weight="bold" />
                      </button>
                    </div>
                  </div>
                  <p className="composer-caption">{composerMode === "chat" ? "普通对话不会读取仓库、创建任务或执行命令。" : currentProject ? projectStatusLabel : "代码任务需要先选择项目。"}</p>
                </>
              )}
            </div>
            </section>
            {task && showTaskInspector && (
              <>
                <button
                  className="task-inspector-backdrop"
                  type="button"
                  aria-label="关闭任务检查器"
                  onClick={() => setShowTaskInspector(false)}
                />
                <TaskInspector
                  task={{
                    title: compactTaskLabel(task),
                    threadId: task.thread_id,
                    status: taskStatus,
                    verdict: task.verdict,
                    pendingApproval: task.pending_approval,
                    mode: activeTaskMode,
                    operation: activeTaskOperation,
                    currentStage: task.progress?.current_stage,
                    progressSummary: task.progress?.summary,
                  }}
                  sources={(contextSnapshot?.sources ?? []).map((source) => ({
                    sourceType: source.source_type,
                    path: source.path,
                    lineStart: source.line_start,
                    lineEnd: source.line_end,
                  }))}
                  attachments={taskAttachments.map((attachment) => ({
                    id: attachment.document_id,
                    name: attachment.display_name,
                    sha256: attachment.content_sha256,
                  }))}
                  evidence={inspectorEvidence}
                  artifacts={artifacts.map((artifact) => ({
                    kind: artifact.kind,
                    label: artifactLabels[artifact.kind] ?? artifact.kind,
                    sizeBytes: artifact.size_bytes,
                  }))}
                  selectedSkillCount={contextSnapshot?.selected_skills.length ?? 0}
                  boundToolCount={contextSnapshot?.bound_tool_ids.length ?? 0}
                  totalTokens={telemetry?.model.total_tokens}
                  terminalCommands={terminalCommands}
                  onClose={() => setShowTaskInspector(false)}
                  onOpenContext={() => setActiveView("context")}
                  onOpenArtifact={(kind) => {
                    if (kind) {
                      setSelectedArtifact(kind);
                      setSelectedArtifactVersion(null);
                    }
                    setActiveView("review");
                  }}
                  onCopyTerminalCommand={copyTerminalCommand}
                />
              </>
            )}
            {task && showTaskTerminal && (
              <TaskTerminalDock
                threadId={task.thread_id}
                commands={terminalCommands}
                onClose={() => setShowTaskTerminal(false)}
                onCopy={copyTerminalCommand}
                onRun={runTerminalCommand}
              />
            )}
          </div>
        )}

        {(activeView === "context" || activeView === "settings") && (
          <section className={activeView === "settings" ? "utility-view settings-view" : "utility-view"}>
            <header className="utility-header">
              <div>
                <h2>{activeView === "settings" ? "设置" : "上下文与扩展"}</h2>
                <p>{activeView === "settings" ? "模型与本机检索服务" : currentProject?.display_name ?? "尚未选择项目"}</p>
              </div>
              <span>{activeView === "settings" ? "仅本机保存" : contextSnapshot ? "已冻结任务快照" : "项目级配置"}</span>
            </header>

            {activeView === "settings" && (
              <div className="settings-intro">
                <ShieldCheck size={18} />
                <p>API Key 仅写入 RepoPilot 桌面应用的本机配置。已保存的密钥不会回显，也不会进入任务证据、日志或导出文件。</p>
              </div>
            )}

            <section hidden={activeView !== "context"} className="settings-section capability-directory-section">
              <div className="settings-title">
                <ShieldCheck size={19} />
                <div><h3>能力目录</h3><p>来源、风险和权限由本机策略统一裁决。目录不代表模型已获得执行权限。</p></div>
              </div>
              <div className="settings-content capability-directory-content">
                <div className="capability-directory-summary">
                  <span>{capabilityDirectory?.capabilities.length ?? 0} 项已登记能力</span>
                  <span>{(capabilityDirectory?.plugins ?? []).filter((plugin) => plugin.active).length} 个已验证插件</span>
                  {task && <span>任务模式：{mode === "safe-isolated" ? "安全隔离" : "完全本机"}</span>}
                </div>
                <div className="capability-filters" role="group" aria-label="筛选能力类型">
                  {(
                    [
                      ["all", "全部"],
                      ["builtin_tool", "工具"],
                      ["skill", "Skills"],
                      ["mcp_tool", "MCP"],
                    ] as const
                  ).map(([filter, label]) => (
                    <button
                      key={filter}
                      className={capabilityFilter === filter ? "active" : ""}
                      type="button"
                      aria-pressed={capabilityFilter === filter}
                      onClick={() => setCapabilityFilter(filter)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                {!projectId ? (
                  <p className="capability-empty">选择项目后查看可供任务使用的受控能力。</p>
                ) : !capabilityDirectory ? (
                  <p className="capability-empty">当前本机 API 尚未提供能力目录。升级后端后可查看策略投影。</p>
                ) : capabilityItems.length === 0 ? (
                  <p className="capability-empty">当前筛选条件下没有能力。MCP 工具需先在下方显式探测。</p>
                ) : (
                  <div className="capability-list">
                    {capabilityItems.map((item) => {
                      const state = capabilityState(item);
                      const allowedTools = Array.isArray(item.details.allowed_tools)
                        ? item.details.allowed_tools.filter((value): value is string => typeof value === "string")
                        : [];
                      return (
                        <article key={item.capability_id} className="capability-row">
                          <span className={`capability-kind capability-kind-${item.kind}`} title={capabilityKindLabels[item.kind]}>
                            {item.kind === "builtin_tool" ? <TerminalWindow size={16} /> : item.kind === "skill" ? <Stack size={16} /> : <PuzzlePiece size={16} />}
                          </span>
                          <div className="capability-main">
                            <div><b>{item.name}</b><span>{capabilityKindLabels[item.kind]} · {item.source_label}</span></div>
                            <p>{item.description}</p>
                            <div className="capability-meta">
                              {item.risks.map((risk) => <span key={risk}>{capabilityRiskLabels[risk] ?? risk}</span>)}
                              {allowedTools.length > 0 && <code>允许：{allowedTools.join(", ")}</code>}
                            </div>
                          </div>
                          <span className={`capability-state ${state.tone}`} title={state.detail}>{state.label}</span>
                        </article>
                      );
                    })}
                  </div>
                )}
                {(capabilityDirectory?.issues ?? []).map((issue) => (
                  <p className="capability-issue" key={issue.code}>{issue.code}：{issue.message}</p>
                ))}
              </div>
            </section>

            <section hidden={activeView !== "settings"} className="settings-section runtime-settings-section settings-single-column">
              <div className="settings-content runtime-configuration">
                {!runtimeConfiguration && (
                  <p className="configuration-notice">当前本地 API 尚未支持应用内运行配置。重启 RepoPilot Desktop 后重试。</p>
                )}
                {runtimeConfiguration && (
                  <>
                    <div className="runtime-settings-overview">
                      <div className="runtime-settings-copy">
                        <span className={runtimeConfiguration.writable ? "ready" : "blocked"}>
                          {runtimeConfiguration.writable ? "可保存到桌面配置" : "当前连接只读"}
                        </span>
                        <p>密钥仅保存在此设备，不会显示在任务证据、日志或导出文件中。</p>
                      </div>
                      <div className="runtime-status-summary" aria-label="运行配置完成情况">
                        <span className={runtimeConfiguration.chat?.api_key_configured && chatBaseUrl && chatModel ? "complete" : "incomplete"}>
                          <b>{runtimeConfiguration.chat?.api_key_configured && chatBaseUrl && chatModel ? "已完成" : "待配置"}</b>对话模型
                        </span>
                        <span className={runtimeConfiguration.embedding?.api_key_configured && embeddingBaseUrl && embeddingModel && embeddingDimensions ? "complete" : "incomplete"}>
                          <b>{runtimeConfiguration.embedding?.api_key_configured && embeddingBaseUrl && embeddingModel && embeddingDimensions ? "已完成" : "待配置"}</b>向量检索
                        </span>
                        <span className={runtimeDependencies.find((dependency) => dependency.component === "qdrant")?.status === "READY" ? "complete" : "incomplete"}>
                          <b>{runtimeDependencies.find((dependency) => dependency.component === "qdrant")?.status === "READY" ? "已连接" : "需处理"}</b>Qdrant
                        </span>
                      </div>
                    </div>
                    <div className="runtime-diagnostics">
                      <details>
                        <summary>查看本机依赖检查</summary>
                        <div className="runtime-diagnostics-body">
                          {runtimeDependencies.length === 0 ? (
                            <p className="runtime-dependency-empty">本机 API 尚未返回依赖详情，请刷新状态或重启 RepoPilot Desktop。</p>
                          ) : (
                            <div className="runtime-dependency-list" aria-label="本机依赖状态">
                              {runtimeDependencies.map((dependency) => (
                                <div key={dependency.component} className="runtime-dependency-row">
                                  <span className={dependency.status === "READY" ? "ready" : "blocked"}>{dependency.status === "READY" ? "已就绪" : "已阻断"}</span>
                                  <div>
                                    <b>{runtimeDependencyLabels[dependency.component] ?? dependency.component}</b>
                                    <p>{dependency.message}</p>
                                  </div>
                                  <code>{dependency.code}</code>
                                </div>
                              ))}
                            </div>
                          )}
                        </div>
                      </details>
                      <button className="secondary-button" type="button" onClick={() => void checkApiHealth()} disabled={runtimeHealthChecking}>
                        <ArrowClockwise size={15} />{runtimeHealthChecking ? "检查中" : "刷新状态"}
                      </button>
                    </div>
                    <div className="runtime-config-groups">
                      <section className="runtime-config-group">
                        <header>
                          <b>对话模型</b>
                          <span>用于任务分析、工具调用和修改计划。</span>
                          <em className={runtimeConfiguration.chat?.api_key_configured && chatBaseUrl && chatModel ? "configuration-state complete" : "configuration-state incomplete"}>
                            {runtimeConfiguration.chat?.api_key_configured && chatBaseUrl && chatModel ? "配置完整" : "待配置"}
                          </em>
                        </header>
                        <div className="model-preset-row" aria-label="对话模型预设">
                          <span>快速填入</span>
                          {chatModelPresets.map((preset) => (
                            <button
                              key={preset.id}
                              type="button"
                              className={chatBaseUrl === preset.baseUrl && chatModel === preset.model ? "active" : ""}
                              title={preset.description}
                              aria-pressed={chatBaseUrl === preset.baseUrl && chatModel === preset.model}
                              onClick={() => applyChatModelPreset(preset)}
                              disabled={!runtimeConfiguration.writable}
                            >
                              {preset.label}
                            </button>
                          ))}
                          <small>仅填地址与模型，不写入 Key</small>
                        </div>
                        <div className="runtime-config-grid">
                          <label>Base URL<input value={chatBaseUrl} onChange={(event) => setChatBaseUrl(event.target.value)} placeholder="https://api.deepseek.com" disabled={!runtimeConfiguration.writable} /></label>
                          <label>Model<input value={chatModel} onChange={(event) => setChatModel(event.target.value)} placeholder="deepseek-chat" disabled={!runtimeConfiguration.writable} /></label>
                          <label className="runtime-secret-field runtime-config-wide">
                            API Key
                            <input type="password" value={chatApiKey} onChange={(event) => { setChatApiKey(event.target.value); setClearChatApiKey(false); }} placeholder={runtimeConfiguration.chat?.api_key_configured ? "已配置，输入新值才会替换" : "未配置"} autoComplete="off" disabled={!runtimeConfiguration.writable || clearChatApiKey} />
                            <small>{runtimeConfiguration.chat?.api_key_configured ? "已配置，值不会显示。" : "尚未配置。"}</small>
                          </label>
                        </div>
                      </section>
                      <section className="runtime-config-group">
                        <header>
                          <b>Embedding 模型</b>
                          <span>用于代码、研发文档和项目记忆的向量检索。</span>
                          <em className={runtimeConfiguration.embedding?.api_key_configured && embeddingBaseUrl && embeddingModel && embeddingDimensions ? "configuration-state complete" : "configuration-state incomplete"}>
                            {runtimeConfiguration.embedding?.api_key_configured && embeddingBaseUrl && embeddingModel && embeddingDimensions ? "配置完整" : "待配置"}
                          </em>
                        </header>
                        <div className="model-preset-row" aria-label="Embedding 模型预设">
                          <span>快速填入</span>
                          {embeddingModelPresets.map((preset) => (
                            <button
                              key={preset.id}
                              type="button"
                              className={embeddingBaseUrl === preset.baseUrl && embeddingModel === preset.model && embeddingDimensions === preset.dimensions ? "active" : ""}
                              title={preset.description}
                              aria-pressed={embeddingBaseUrl === preset.baseUrl && embeddingModel === preset.model && embeddingDimensions === preset.dimensions}
                              onClick={() => applyEmbeddingModelPreset(preset)}
                              disabled={!runtimeConfiguration.writable}
                            >
                              {preset.label}
                            </button>
                          ))}
                          <small>请确认服务端支持对应维度</small>
                        </div>
                        <div className="runtime-config-grid">
                          <label>Base URL<input value={embeddingBaseUrl} onChange={(event) => setEmbeddingBaseUrl(event.target.value)} placeholder="OpenAI-compatible embedding endpoint" disabled={!runtimeConfiguration.writable} /></label>
                          <label>Model<input value={embeddingModel} onChange={(event) => setEmbeddingModel(event.target.value)} placeholder="text-embedding-3-small" disabled={!runtimeConfiguration.writable} /></label>
                          <label>Dimensions<input inputMode="numeric" value={embeddingDimensions} onChange={(event) => setEmbeddingDimensions(event.target.value)} placeholder="1536" disabled={!runtimeConfiguration.writable} /></label>
                          <label className="runtime-secret-field">
                            API Key
                            <input type="password" value={embeddingApiKey} onChange={(event) => { setEmbeddingApiKey(event.target.value); setClearEmbeddingApiKey(false); }} placeholder={runtimeConfiguration.embedding?.api_key_configured ? "已配置，输入新值才会替换" : "未配置"} autoComplete="off" disabled={!runtimeConfiguration.writable || clearEmbeddingApiKey} />
                            <small>{runtimeConfiguration.embedding?.api_key_configured ? "已配置，值不会显示。" : "尚未配置。"}</small>
                          </label>
                        </div>
                      </section>
                      <section className="runtime-config-group">
                        <header>
                          <b>本机检索服务</b>
                          <span>Qdrant 仅用于保存项目级向量索引和已验证记忆。</span>
                          <em className={qdrantUrl ? "configuration-state complete" : "configuration-state incomplete"}>{qdrantUrl ? "已填写" : "待配置"}</em>
                        </header>
                        <div className="runtime-config-grid">
                          <label className="runtime-config-wide">Qdrant URL<input value={qdrantUrl} onChange={(event) => setQdrantUrl(event.target.value)} placeholder="http://127.0.0.1:6333" disabled={!runtimeConfiguration.writable} /></label>
                        </div>
                      </section>
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

            <section hidden={activeView !== "context"} className="settings-section">
              <div className="settings-title">
                <FileArrowUp size={19} />
                <div><h3>研发文档</h3><p>MD / TXT · {documents.length} 份已索引文档</p></div>
              </div>
              <div className="settings-content">
                <div className="inline-form">
                  <button className="secondary-button" type="button" onClick={() => void chooseDocument()} disabled={!projectId || documentBusy}>
                    <FileArrowUp size={16} />{documentBusy ? "上传并索引中" : "上传文档"}
                  </button>
                </div>
                <div className="document-list">
                  {documents.length === 0 ? <p>暂无项目文档</p> : documents.map((document) => (
                    <span key={document.document_id}><FileCode size={15} />{document.display_name}</span>
                  ))}
                </div>
              </div>
            </section>

            <section hidden={activeView !== "context"} className="settings-section">
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

            <section hidden={activeView !== "context"} className="settings-section">
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

            {activeView === "context" && (contextSnapshot || telemetry || taskAttachments.length > 0) && (
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
                  <p>{task ? compactTaskLabel(task) + " · " + taskStateLabel(task.status, task.verdict, task.pending_approval) : "尚未选择任务"}</p>
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
              <ReviewDecisionSummary
                task={task ? {
                  status: task.status,
                  verdict: task.verdict,
                  pendingApproval: task.pending_approval,
                  taskMode: task.task_mode,
                  diagnostic: task.diagnostic,
                } : null}
                artifacts={artifacts.map((artifact) => ({ kind: artifact.kind }))}
                onOpenArtifact={(kind) => {
                  setSelectedArtifact(kind);
                  setSelectedArtifactVersion(null);
                }}
              />
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
      <CommandPalette
        open={showCommandPalette}
        items={commandItems}
        onClose={() => setShowCommandPalette(false)}
      />
    </main>
  );
}
