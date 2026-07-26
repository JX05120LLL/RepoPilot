import {
  FileMagnifyingGlass,
  FileText,
  SlidersHorizontal,
  WarningCircle,
} from "@phosphor-icons/react";

type Diagnostic = {
  tone: "neutral" | "success" | "warning" | "danger";
  code: string;
  title: string;
  summary: string;
  recommended_action: string;
};

type Artifact = {
  kind: string;
};

type TaskDiagnosticPanelProps = {
  diagnostic: Diagnostic | null | undefined;
  artifacts: Artifact[];
  onOpenArtifact: (kind: string) => void;
  onOpenRuntimeConfiguration: () => void;
};

/** 展示服务端已脱敏的诊断投影，前端不解析或回显原始异常。 */
export function TaskDiagnosticPanel({
  diagnostic,
  artifacts,
  onOpenArtifact,
  onOpenRuntimeConfiguration,
}: TaskDiagnosticPanelProps) {
  if (!diagnostic || diagnostic.tone === "neutral" || diagnostic.tone === "success") {
    return null;
  }

  const hasArtifact = (kind: string) => artifacts.some((artifact) => artifact.kind === kind);
  const action = resolveAction(diagnostic.recommended_action, hasArtifact);

  return (
    <section className={`task-diagnostic task-diagnostic-${diagnostic.tone}`} aria-label="任务诊断">
      <WarningCircle size={18} weight="fill" aria-hidden="true" />
      <div className="task-diagnostic-content">
        <div className="task-diagnostic-heading">
          <strong>{diagnostic.title}</strong>
          <code>{diagnostic.code}</code>
        </div>
        <p>{diagnostic.summary}</p>
        {action && (
          <button
            className="task-diagnostic-action"
            type="button"
            onClick={() => {
              if (action.kind === "runtime") onOpenRuntimeConfiguration();
              if (action.kind === "artifact") onOpenArtifact(action.artifact);
            }}
          >
            {action.kind === "runtime" ? <SlidersHorizontal size={15} /> : action.artifact === "plan_markdown" ? <FileText size={15} /> : <FileMagnifyingGlass size={15} />}
            {action.label}
          </button>
        )}
      </div>
    </section>
  );
}

function resolveAction(
  recommendedAction: string,
  hasArtifact: (kind: string) => boolean,
): { kind: "runtime"; label: string } | { kind: "artifact"; artifact: string; label: string } | null {
  if (recommendedAction === "OPEN_RUNTIME_CONFIGURATION") {
    return { kind: "runtime", label: "检查运行配置" };
  }
  if (recommendedAction === "OPEN_VERIFICATION" && hasArtifact("verification")) {
    return { kind: "artifact", artifact: "verification", label: "查看验证记录" };
  }
  if (recommendedAction === "OPEN_PLAN" && hasArtifact("plan_markdown")) {
    return { kind: "artifact", artifact: "plan_markdown", label: "查看修改计划" };
  }
  if (recommendedAction === "OPEN_TASK_EVIDENCE") {
    if (hasArtifact("report")) return { kind: "artifact", artifact: "report", label: "查看任务报告" };
    if (hasArtifact("verification")) return { kind: "artifact", artifact: "verification", label: "查看验证记录" };
  }
  return null;
}
