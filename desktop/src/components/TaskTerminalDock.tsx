import {
  CaretDown,
  CheckCircle,
  CopySimple,
  Play,
  TerminalWindow,
  X,
} from "@phosphor-icons/react";
import { useEffect, useState } from "react";

export type TerminalCommand = {
  id: "status" | "review" | "artifacts";
  label: string;
  command: string;
};

export type TerminalResult = {
  title: string;
  lines: string[];
};

type TerminalEntry = TerminalResult & {
  id: number;
  command: string;
  status: "running" | "completed" | "failed";
};

type TaskTerminalDockProps = {
  threadId: string;
  commands: TerminalCommand[];
  onClose: () => void;
  onCopy: (command: string) => Promise<boolean>;
  onRun: (command: TerminalCommand) => Promise<TerminalResult>;
};

export function TaskTerminalDock({
  threadId,
  commands,
  onClose,
  onCopy,
  onRun,
}: TaskTerminalDockProps) {
  const [commandId, setCommandId] = useState<TerminalCommand["id"]>("status");
  const [entries, setEntries] = useState<TerminalEntry[]>([]);
  const [running, setRunning] = useState(false);
  const [copied, setCopied] = useState(false);

  const selected = commands.find((item) => item.id === commandId) ?? commands[0];

  useEffect(() => {
    setCommandId("status");
    setEntries([]);
    setRunning(false);
  }, [threadId]);

  async function runSelected() {
    if (!selected || running) return;
    const id = Date.now();
    const pending: TerminalEntry = {
      id,
      command: selected.command,
      title: selected.label,
      lines: ["正在读取受控任务摘要..."],
      status: "running",
    };
    setEntries((current) => [...current, pending].slice(-4));
    setRunning(true);
    try {
      const result = await onRun(selected);
      setEntries((current) =>
        current.map((entry) =>
          entry.id === id ? { ...entry, ...result, status: "completed" } : entry,
        ),
      );
    } catch (error) {
      const message = error instanceof Error ? error.message : "任务摘要不可读取";
      setEntries((current) =>
        current.map((entry) =>
          entry.id === id
            ? { ...entry, title: "读取失败", lines: [message], status: "failed" }
            : entry,
        ),
      );
    } finally {
      setRunning(false);
    }
  }

  async function copySelected() {
    if (!selected || !(await onCopy(selected.command))) return;
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1_800);
  }

  return (
    <section className="task-terminal-dock" aria-label="受控终端">
      <header className="task-terminal-header">
        <div>
          <TerminalWindow size={17} />
          <strong>受控终端</strong>
          <span>本机</span>
        </div>
        <button type="button" className="icon-button" title="关闭受控终端 (Ctrl+J)" onClick={onClose}>
          <X size={17} />
        </button>
      </header>

      <div className="task-terminal-commandbar">
        <span className="task-terminal-prompt">repopilot[{threadId.slice(0, 8)}]&gt;</span>
        <label>
          <span className="sr-only">选择受控命令</span>
          <select value={selected?.id ?? "status"} onChange={(event) => setCommandId(event.target.value as TerminalCommand["id"])}>
            {commands.map((command) => <option key={command.id} value={command.id}>{command.command}</option>)}
          </select>
          <CaretDown size={14} />
        </label>
        <button type="button" className="terminal-copy" title="复制当前 CLI 命令" onClick={() => void copySelected()} disabled={!selected}>
          {copied ? <CheckCircle size={16} weight="fill" /> : <CopySimple size={16} />}
          {copied ? "已复制" : "复制"}
        </button>
        <button type="button" className="terminal-run" title="运行受控查询" onClick={() => void runSelected()} disabled={!selected || running}>
          <Play size={14} weight="fill" />
          {running ? "读取中" : "运行"}
        </button>
      </div>

      <div className="task-terminal-output" aria-live="polite">
        {entries.length === 0 ? (
          <p><span>$</span> 选择状态、审阅或产物查询。</p>
        ) : entries.map((entry) => (
          <article key={entry.id} className={`terminal-entry terminal-${entry.status}`}>
            <code>$ {entry.command}</code>
            <strong>{entry.title}</strong>
            {entry.lines.map((line, index) => <p key={`${entry.id}-${index}`}>{line}</p>)}
          </article>
        ))}
      </div>
    </section>
  );
}
