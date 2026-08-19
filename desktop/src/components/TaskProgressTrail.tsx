import {
  CheckCircle,
  CircleNotch,
  WarningCircle,
} from "@phosphor-icons/react";

type ProgressStage = {
  id: string;
  label: string;
  state:
    | "completed"
    | "current"
    | "pending"
    | "passed"
    | "failed"
    | "blocked"
    | "cancelled"
    | "unverified";
};

type TaskProgressTrailProps = {
  summary: string;
  stages: ProgressStage[];
  running: boolean;
};

/** 任务运行轨迹只展示服务端状态，不参与任务调度或权限判断。 */
export function TaskProgressTrail({
  summary,
  stages,
  running,
}: TaskProgressTrailProps) {
  return (
    <section className="task-progress" aria-label="Agent 任务阶段">
      <div className="task-progress-heading">
        <span>任务阶段</span>
        <small>{summary}</small>
      </div>
      <ol>
        {stages.map((stage) => (
          <li key={stage.id} className={`progress-${stage.state}`}>
            <span className="progress-marker" aria-hidden="true">
              {stage.state === "completed" || stage.state === "passed" ? (
                <CheckCircle size={14} weight="fill" />
              ) : stage.state === "current" ? (
                <CircleNotch className={running ? "spin" : ""} size={14} />
              ) : stage.state === "failed" || stage.state === "blocked" ? (
                <WarningCircle size={14} weight="fill" />
              ) : (
                <i />
              )}
            </span>
            <span>{stage.label}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}
