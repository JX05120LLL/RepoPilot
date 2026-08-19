import {
  CheckCircle,
  CopySimple,
  FileCode,
  GitBranch,
  LinkSimple,
  Paperclip,
  Stack,
  TerminalWindow,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import { useState } from "react";

type InspectorTask = {
  title: string;
  threadId: string;
  status: string;
  verdict?: string | null;
  pendingApproval: boolean;
  mode: "safe-isolated" | "full-local";
  operation: "change" | "research";
  currentStage?: string;
  progressSummary?: string;
};

type InspectorSource = {
  sourceType: string;
  path: string;
  lineStart?: number | null;
  lineEnd?: number | null;
};

type InspectorAttachment = {
  id: string;
  name: string;
  sha256: string;
};

type InspectorEvidence = {
  id: string;
  label: string;
  summary: string;
};

type InspectorArtifact = {
  kind: string;
  label: string;
  sizeBytes: number;
};

type InspectorTerminalCommand = {
  id: string;
  label: string;
  command: string;
};

type InspectorWorkspace = {
  mode: "local" | "worktree";
  lifecycle: "local" | "detached" | "branch";
  branch?: string | null;
  baseCommit?: string | null;
  dirtyFileCount?: number;
  branchCreationAvailable: boolean;
  localHandoffAvailable: boolean;
};

type InspectorSkill = {
  name: string;
  scope: string;
  allowedTools?: string[];
  effectiveTools?: string[];
};

type TaskInspectorProps = {
  task: InspectorTask;
  sources: InspectorSource[];
  attachments: InspectorAttachment[];
  evidence: InspectorEvidence[];
  artifacts: InspectorArtifact[];
  selectedSkillCount: number;
  selectedSkills: InspectorSkill[];
  boundToolCount: number;
  totalTokens?: number;
  terminalCommands: InspectorTerminalCommand[];
  workspace?: InspectorWorkspace | null;
  onClose: () => void;
  onOpenContext: () => void;
  onOpenArtifact: (kind?: string) => void;
  onCopyTerminalCommand: (command: string) => Promise<boolean>;
  onCreateWorkspaceBranch: () => void;
  onHandoffWorkspaceToLocal: () => void;
};

const statusLabels: Record<string, string> = {
  WAITING_APPROVAL: "等待审批",
  REPORT: "任务结束",
  PASSED: "验证通过",
  FAILED: "验证失败",
  BLOCKED: "已阻断",
  CANCELLED: "已取消",
  UNVERIFIED: "未验证",
};

const stageLabels: Record<string, string> = {
  INTAKE: "接收任务",
  WORKSPACE: "准备工作区",
  PREFLIGHT: "运行预检",
  INGEST: "整理上下文",
  RETRIEVE: "检索上下文",
  ANALYZE: "分析代码",
  RESEARCH_TOOLS: "研究代码",
  PLAN: "生成计划",
  PLAN_APPROVAL: "计划审批",
  EXECUTION_APPROVAL: "执行审批",
  PATCH: "应用补丁",
  VERIFY: "运行构建验证",
  VERIFICATION_OBSERVATION: "核对验证结果",
  VERIFICATION_TOOLS: "核对验证证据",
  REVIEW: "审阅结果",
  REPORT: "生成报告",
};

function statusTone(status: string, verdict?: string | null): string {
  const value = verdict || status;
  if (value === "PASSED") return "success";
  if (["FAILED", "BLOCKED", "CANCELLED"].includes(value)) return "danger";
  if (value === "WAITING_APPROVAL") return "warning";
  return "neutral";
}

function sourceLocation(source: InspectorSource): string {
  if (!source.lineStart) return source.path;
  if (source.lineEnd && source.lineEnd !== source.lineStart) {
    return `${source.path}:${source.lineStart}-${source.lineEnd}`;
  }
  return `${source.path}:${source.lineStart}`;
}

function stageLabel(stage?: string, fallback?: string): string {
  const normalized = stage?.trim().toUpperCase();
  return (normalized ? stageLabels[normalized] : undefined) ?? fallback ?? "等待开始";
}

export function TaskInspector({
  task,
  sources,
  attachments,
  evidence,
  artifacts,
  selectedSkillCount,
  selectedSkills,
  boundToolCount,
  totalTokens,
  terminalCommands,
  workspace,
  onClose,
  onOpenContext,
  onOpenArtifact,
  onCopyTerminalCommand,
  onCreateWorkspaceBranch,
  onHandoffWorkspaceToLocal,
}: TaskInspectorProps) {
  const [copiedCommandId, setCopiedCommandId] = useState<string | null>(null);
  const tone = statusTone(task.pendingApproval ? "WAITING_APPROVAL" : task.status, task.verdict);
  const statusLabel = task.pendingApproval
    ? "等待审批"
    : statusLabels[task.verdict || task.status] ?? task.verdict ?? task.status;

  async function copyCommand(command: InspectorTerminalCommand) {
    const copied = await onCopyTerminalCommand(command.command);
    if (!copied) return;
    setCopiedCommandId(command.id);
    window.setTimeout(() => setCopiedCommandId(null), 1_800);
  }

  return (
    <aside className="task-inspector" aria-label="任务检查器">
      <header className="task-inspector-header">
        <div>
          <span>任务检查器</span>
          <strong title={task.title}>{task.title}</strong>
        </div>
        <button className="icon-button" type="button" title="关闭任务检查器" onClick={onClose}>
          <X size={17} />
        </button>
      </header>

      <div className="task-inspector-scroll">
        <section className="inspector-status">
          <div className={`inspector-verdict tone-${tone}`}>
            {tone === "success" ? (
              <CheckCircle size={18} weight="fill" />
            ) : tone === "danger" || tone === "warning" ? (
              <WarningCircle size={18} weight="fill" />
            ) : (
              <span className="inspector-status-mark" />
            )}
            <div>
              <strong>{statusLabel}</strong>
              <span>{task.progressSummary || "任务状态已同步"}</span>
            </div>
          </div>
          <dl className="inspector-facts">
            <div><dt>模式</dt><dd>{task.mode === "safe-isolated" ? "安全隔离修复" : "完全本机控制"}</dd></div>
            <div><dt>类型</dt><dd>{task.operation === "research" ? "计划模式" : "修改代码"}</dd></div>
            <div><dt>阶段</dt><dd>{stageLabel(task.currentStage, statusLabels[task.status] ?? task.status)}</dd></div>
            <div><dt>线程</dt><dd title={task.threadId}>{task.threadId.slice(0, 12)}</dd></div>
          </dl>
        </section>

        <section className="inspector-section inspector-terminal">
          <header><span>受控终端</span><TerminalWindow size={16} /></header>
          <div className="inspector-terminal-commands">
            {terminalCommands.map((command) => (
              <article key={command.id}>
                <span>{command.label}</span>
                <code>{command.command}</code>
                <button
                  type="button"
                  title={`复制${command.label}命令`}
                  onClick={() => void copyCommand(command)}
                >
                  {copiedCommandId === command.id ? <CheckCircle size={15} weight="fill" /> : <CopySimple size={15} />}
                  {copiedCommandId === command.id ? "已复制" : "复制"}
                </button>
              </article>
            ))}
          </div>
        </section>

        {workspace && (
          <section className="inspector-section inspector-workspace">
            <header><span>工作区</span><GitBranch size={16} /></header>
            <div className="inspector-workspace-card">
              <strong>
                {workspace.mode === "worktree"
                  ? workspace.lifecycle === "detached"
                    ? "隔离 Worktree"
                    : "审阅分支"
                  : "本机工作区"}
              </strong>
              {workspace.mode === "worktree" ? (
                <>
                  <p>
                    {workspace.lifecycle === "detached"
                      ? "当前修改仍与源仓库分离。"
                      : `已创建分支 ${workspace.branch ?? ""}。`}
                  </p>
                  <dl>
                    <div><dt>基线</dt><dd>{workspace.baseCommit?.slice(0, 12) ?? "未记录"}</dd></div>
                    <div><dt>改动</dt><dd>{workspace.dirtyFileCount ?? 0} 个文件</dd></div>
                  </dl>
                  {workspace.branchCreationAvailable && (
                    <button type="button" onClick={onCreateWorkspaceBranch}>
                      <GitBranch size={14} />创建审阅分支
                    </button>
                  )}
                  {workspace.localHandoffAvailable && (
                    <button className="inspector-workspace-handoff" type="button" onClick={onHandoffWorkspaceToLocal}>
                      交接到 Local
                    </button>
                  )}
                </>
              ) : (
                <p>此任务直接绑定本机项目；不会创建隔离副本。</p>
              )}
            </div>
          </section>
        )}

        <section className="inspector-section">
          <header><span>上下文</span><b>{sources.length + attachments.length}</b></header>
          <div className="inspector-context-stats">
            <span>{sources.length} 来源</span>
            <span>{attachments.length} 附件</span>
            <span>{selectedSkillCount} Skills</span>
            <span>{boundToolCount} 工具</span>
          </div>
          {sources.length === 0 && attachments.length === 0 ? (
            <p className="inspector-empty">任务上下文尚未生成。</p>
          ) : (
            <div className="inspector-source-list">
              {sources.slice(0, 6).map((source, index) => (
                <div key={`${source.path}-${index}`}>
                  <LinkSimple size={14} />
                  <span title={sourceLocation(source)}>{sourceLocation(source)}</span>
                  <small>{source.sourceType}</small>
                </div>
              ))}
              {attachments.slice(0, 4).map((attachment) => (
                <div key={attachment.id}>
                  <Paperclip size={14} />
                  <span title={attachment.name}>{attachment.name}</span>
                  <small>{attachment.sha256.slice(0, 8)}</small>
                </div>
              ))}
              {sources.length + attachments.length > 10 && (
                <p className="inspector-more">另有 {sources.length + attachments.length - 10} 项</p>
              )}
            </div>
          )}
          {selectedSkills.length > 0 && (
            <div className="inspector-skill-list">
              <p>Skill 工具范围已按任务权限冻结</p>
              {selectedSkills.map((skill) => {
                const requested = skill.allowedTools ?? [];
                const effective = skill.effectiveTools ?? [];
                return (
                  <article key={`${skill.scope}-${skill.name}`}>
                    <strong>{skill.name}</strong>
                    <div>
                      <span>请求</span>
                      <code>{requested.length > 0 ? requested.join(", ") : "未声明"}</code>
                    </div>
                    <div>
                      <span>可用</span>
                      <code>{effective.length > 0 ? effective.join(", ") : "未获得额外工具授权"}</code>
                    </div>
                  </article>
                );
              })}
            </div>
          )}
          <button className="inspector-link" type="button" onClick={onOpenContext}>
            <Stack size={15} />打开上下文与扩展
          </button>
        </section>

        <section className="inspector-section">
          <header><span>关键证据</span><b>{evidence.length}</b></header>
          {evidence.length === 0 ? (
            <p className="inspector-empty">关键 Evidence 尚未写入。</p>
          ) : (
            <div className="inspector-evidence-list">
              {evidence.slice(0, 5).map((item) => (
                <article key={item.id}>
                  <span />
                  <div><b>{item.label}</b><p>{item.summary}</p></div>
                </article>
              ))}
            </div>
          )}
        </section>

        <section className="inspector-section inspector-artifacts">
          <header><span>产物</span><b>{artifacts.length}</b></header>
          {artifacts.length === 0 ? (
            <p className="inspector-empty">任务完成后可在此打开计划、Diff 和报告。</p>
          ) : (
            <div>
              {artifacts.slice(0, 6).map((artifact) => (
                <button type="button" key={artifact.kind} onClick={() => onOpenArtifact(artifact.kind)}>
                  <FileCode size={15} />
                  <span>{artifact.label}</span>
                  <small>{artifact.sizeBytes.toLocaleString()} B</small>
                </button>
              ))}
            </div>
          )}
          {typeof totalTokens === "number" && (
            <p className="inspector-telemetry">本次模型用量 {totalTokens.toLocaleString()} tokens</p>
          )}
        </section>

      </div>

      <footer className="task-inspector-footer">
        <button type="button" onClick={onOpenContext}><Stack size={15} />上下文</button>
        <button type="button" onClick={() => onOpenArtifact()} disabled={artifacts.length === 0}>
          <FileCode size={15} />完整审阅
        </button>
      </footer>
    </aside>
  );
}
