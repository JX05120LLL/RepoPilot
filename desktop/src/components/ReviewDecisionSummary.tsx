import {
  CheckCircle,
  FileCode,
  FileMagnifyingGlass,
  GitDiff,
  WarningCircle,
} from "@phosphor-icons/react";

type ReviewTask = {
  status: string;
  verdict?: string | null;
  pendingApproval: boolean;
  taskMode?: string | null;
};

type ReviewArtifact = {
  kind: string;
};

type ReviewDecisionSummaryProps = {
  task: ReviewTask | null;
  artifacts: ReviewArtifact[];
  onOpenArtifact: (kind: string) => void;
};

type ReviewDecision = {
  tone: "success" | "warning" | "danger" | "neutral";
  title: string;
  detail: string;
};

function resolveDecision(task: ReviewTask | null): ReviewDecision {
  if (!task) {
    return {
      tone: "neutral",
      title: "尚未选择任务",
      detail: "选择一个任务后，RepoPilot 才会展示对应的补丁、验证和证据结论。",
    };
  }
  if (task.pendingApproval) {
    return {
      tone: "warning",
      title: "等待人工审批",
      detail: "后续写入或验证仍被暂停，审批前不会继续执行。",
    };
  }
  if (task.verdict === "PASSED") {
    return {
      tone: "success",
      title: "已通过真实验证",
      detail: "结论以真实 Diff 和受控 Maven Recipe 的成功证据为准。",
    };
  }
  if (task.verdict === "FAILED") {
    return {
      tone: "danger",
      title: "验证未通过",
      detail: "本次任务不能标记为已修复；请从验证记录和证据中定位失败原因。",
    };
  }
  if (task.status === "BLOCKED" || task.verdict === "BLOCKED") {
    return {
      tone: "danger",
      title: "已被安全策略阻断",
      detail: "RepoPilot 没有继续执行越权或不满足前置条件的操作。",
    };
  }
  return {
    tone: "neutral",
    title: "尚未验证",
    detail: "当前产物可供审阅，但尚无可证明修复成功的 Maven 验证证据。",
  };
}

function artifactState(artifacts: ReviewArtifact[], kind: string, available: string): string {
  return artifacts.some((artifact) => artifact.kind === kind) ? available : "未生成";
}

/** 只用任务快照与产物元数据生成审阅导航，不读取产物正文或推测执行结果。 */
export function ReviewDecisionSummary({
  task,
  artifacts,
  onOpenArtifact,
}: ReviewDecisionSummaryProps) {
  const decision = resolveDecision(task);
  const preferredArtifact = task?.pendingApproval
    ? artifacts.find((artifact) => artifact.kind === "plan_markdown")?.kind
    : artifacts.find((artifact) => artifact.kind === "verification")?.kind ??
      artifacts.find((artifact) => artifact.kind === "git_diff")?.kind ??
      artifacts.find((artifact) => artifact.kind === "plan_markdown")?.kind;
  const preferredLabel = preferredArtifact === "verification"
    ? "查看验证记录"
    : preferredArtifact === "git_diff"
      ? "查看变更 Diff"
      : "查看修改计划";
  const VerdictIcon = decision.tone === "success" ? CheckCircle : WarningCircle;

  return (
    <section className={`review-decision review-decision-${decision.tone}`} aria-label="审阅结论">
      <div className="review-decision-verdict">
        <VerdictIcon size={20} weight="fill" aria-hidden="true" />
        <div>
          <span>审阅结论</span>
          <strong>{decision.title}</strong>
          <p>{decision.detail}</p>
        </div>
      </div>
      <dl className="review-decision-facts">
        <div><dt>变更 Diff</dt><dd>{artifactState(artifacts, "git_diff", "已生成")}</dd></div>
        <div><dt>Maven 验证</dt><dd>{artifactState(artifacts, "verification", "已记录")}</dd></div>
        <div><dt>执行模式</dt><dd>{task?.taskMode === "full-local" ? "完全本机控制" : "安全隔离修复"}</dd></div>
      </dl>
      {preferredArtifact && (
        <button className="review-decision-action" type="button" onClick={() => onOpenArtifact(preferredArtifact)}>
          {preferredArtifact === "git_diff" ? <GitDiff size={15} /> : preferredArtifact === "verification" ? <FileMagnifyingGlass size={15} /> : <FileCode size={15} />}
          {preferredLabel}
        </button>
      )}
    </section>
  );
}
