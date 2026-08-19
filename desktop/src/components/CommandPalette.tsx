import { useEffect, useRef, useState, type ReactNode } from "react";
import { MagnifyingGlass } from "@phosphor-icons/react";

export type CommandPaletteItem = {
  id: string;
  group: string;
  label: string;
  description: string;
  icon: ReactNode;
  shortcut?: string;
  disabled?: boolean;
  keywords?: string;
  onSelect: () => void;
};

type CommandPaletteProps = {
  open: boolean;
  items: CommandPaletteItem[];
  onClose: () => void;
};

export function CommandPalette({ open, items, onClose }: CommandPaletteProps) {
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visibleItems = items.filter((item) => {
    if (!normalizedQuery) return true;
    return `${item.label} ${item.description} ${item.group} ${item.keywords ?? ""}`
      .toLocaleLowerCase()
      .includes(normalizedQuery);
  });

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActiveIndex(0);
    window.requestAnimationFrame(() => inputRef.current?.focus());
  }, [open]);

  useEffect(() => {
    if (activeIndex < visibleItems.length) return;
    setActiveIndex(Math.max(visibleItems.length - 1, 0));
  }, [activeIndex, visibleItems.length]);

  if (!open) return null;

  function select(item: CommandPaletteItem | undefined) {
    if (!item || item.disabled) return;
    onClose();
    item.onSelect();
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((current) =>
        visibleItems.length === 0 ? 0 : (current + 1) % visibleItems.length,
      );
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((current) =>
        visibleItems.length === 0
          ? 0
          : (current - 1 + visibleItems.length) % visibleItems.length,
      );
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      select(visibleItems[activeIndex]);
    }
  }

  let previousGroup = "";
  return (
    <div className="command-palette-layer" role="presentation" onMouseDown={onClose}>
      <section
        className="command-palette"
        role="dialog"
        aria-modal="true"
        aria-label="RepoPilot 命令面板"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <label className="command-palette-search">
          <MagnifyingGlass size={17} />
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setActiveIndex(0);
            }}
            onKeyDown={handleKeyDown}
            placeholder="搜索操作、项目或任务"
            aria-label="搜索命令"
          />
          <kbd>Esc</kbd>
        </label>
        <div className="command-palette-results" role="listbox" aria-label="可用命令">
          {visibleItems.length === 0 && (
            <div className="command-palette-empty">
              <strong>没有匹配结果</strong>
              <span>尝试输入项目名称、任务标题或操作名称。</span>
            </div>
          )}
          {visibleItems.map((item, index) => {
            const showGroup = item.group !== previousGroup;
            previousGroup = item.group;
            return (
              <div className="command-palette-row" key={item.id}>
                {showGroup && <span className="command-palette-group">{item.group}</span>}
                <button
                  className={index === activeIndex ? "active" : ""}
                  type="button"
                  role="option"
                  aria-selected={index === activeIndex}
                  disabled={item.disabled}
                  onMouseEnter={() => setActiveIndex(index)}
                  onClick={() => select(item)}
                >
                  <span className="command-palette-icon">{item.icon}</span>
                  <span className="command-palette-copy">
                    <b>{item.label}</b>
                    <small>{item.description}</small>
                  </span>
                  {item.shortcut && <kbd>{item.shortcut}</kbd>}
                </button>
              </div>
            );
          })}
        </div>
        <footer>
          <span><kbd>↑↓</kbd> 选择</span>
          <span><kbd>Enter</kbd> 打开</span>
          <span>权限仍由 PolicyGuard 裁决</span>
        </footer>
      </section>
    </div>
  );
}
