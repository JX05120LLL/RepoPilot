import {
  CheckCircle,
  FileCode,
  LinkSimple,
  Paperclip,
  Stack,
  WarningCircle,
  X,
} from "@phosphor-icons/react";

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

type TaskInspectorProps = {
  task: InspectorTask;
  sources: InspectorSource[];
  attachments: InspectorAttachment[];
  evidence: InspectorEvidence[];
  artifacts: InspectorArtifact[];
  selectedSkillCount: number;
  boundToolCount: number;
  totalTokens?: number;
  onClose: () => void;
  onOpenContext: () => void;
  onOpenArtifact: (kind?: string) => void;
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

export function TaskInspector({
  task,
  sources,
  attachments,
  evidence,
  artifacts,
  selectedSkillCount,
  boundToolCount,
  totalTokens,
  onClose,
  onOpenContext,
  onOpenArtifact,
}: TaskInspectorProps) {
  const tone = statusTone(task.pendingApproval ? "WAITING_APPROVAL" : task.status, task.verdict);
  const statusLabel = task.pendingApproval
    ? "等待审批"
    : statusLabels[task.verdict || task.status] ?? task.verdict ?? task.status;

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
            <div><dt>阶段</dt><dd>{task.currentStage || task.status}</dd></div>
            <div><dt>线程</dt><dd title={task.threadId}>{task.threadId.slice(0, 12)}</dd></div>
          </dl>
        </section>

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
