import { useEffect, useId, useRef } from "react";
import type { ReactNode } from "react";
import { X, FileText, ArrowRight, Check, Minus, Plus } from "lucide-react";
import { diffLines } from "diff";

export function IconButton({
  label,
  children,
  onClick,
  active,
  disabled,
  className = "",
}: {
  label: string;
  children: ReactNode;
  onClick?: () => void;
  active?: boolean;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      aria-pressed={active === undefined ? undefined : active}
      className={`icon-button ${active ? "active" : ""} ${className}`}
      onClick={onClick}
      disabled={disabled}
    >
      {children}
    </button>
  );
}
export function Modal({
  title,
  subtitle,
  children,
  onClose,
  wide = false,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const focusables = () => [
      ...(ref.current?.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex="0"]',
      ) || []),
    ].filter((el) => el.getClientRects().length > 0);
    const initial = focusables();
    (initial.find((el) => el.tagName !== "BUTTON") || initial[0] || ref.current)?.focus();
    const key = (event: KeyboardEvent) => {
      // Nested pickers also use Modal. Let only the visible top dialog handle
      // keyboard dismissal/focus; a parent must not discard a child's edits
      // before its own close confirmation has been accepted.
      const dialogs = document.querySelectorAll<HTMLElement>(
        '.modal-backdrop > [role="dialog"]',
      );
      if (event.defaultPrevented || dialogs[dialogs.length - 1] !== ref.current)
        return;
      if (event.key === "Escape") {
        event.preventDefault();
        closeRef.current();
      }
      if (event.key === "Tab") {
        const elements = focusables();
        const first = elements[0];
        const last = elements.at(-1);
        if (!first) {
          ref.current?.focus();
          event.preventDefault();
        } else if (!ref.current?.contains(document.activeElement)) {
          (event.shiftKey ? last : first)?.focus();
          event.preventDefault();
        } else if (event.shiftKey && document.activeElement === first) {
          last?.focus();
          event.preventDefault();
        } else if (!event.shiftKey && document.activeElement === last) {
          first?.focus();
          event.preventDefault();
        }
      }
    };
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("keydown", key);
      previous?.focus();
    };
  }, []);
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={ref}
        role="dialog"
        tabIndex={-1}
        aria-modal="true"
        aria-labelledby={titleId}
        className={`modal ${wide ? "wide" : ""}`}
      >
        <div className="modal-heading">
          <div>
            <h2 id={titleId}>{title}</h2>
            {subtitle && <p>{subtitle}</p>}
          </div>
          <IconButton label="关闭对话框" onClick={onClose}>
            <X size={19} />
          </IconButton>
        </div>
        {children}
      </div>
    </div>
  );
}
export function Empty({
  icon,
  title,
  description,
  action,
  compact = false,
}: {
  icon?: ReactNode;
  title: string;
  description: string;
  action?: ReactNode;
  compact?: boolean;
}) {
  return (
    <div className={`empty ${compact ? "compact" : ""}`}>
      <div className="empty-icon">
        {icon || <FileText size={24} strokeWidth={1.4} />}
      </div>
      <h3>{title}</h3>
      <p>{description}</p>
      {action}
    </div>
  );
}
export function Diff({ before, after }: { before: string; after: string }) {
  const changes = diffLines(before, after);
  if (before === after)
    return (
      <div className="diff-empty">
        <Check size={16} />
        内容没有变化
      </div>
    );
  return (
    <div className="diff" aria-label="文本差异">
      {changes.map((part, index) => (
        <div
          key={index}
          className={`diff-part ${part.added ? "added" : part.removed ? "removed" : "unchanged"}`}
        >
          <span
            aria-label={part.added ? "新增" : part.removed ? "移除" : "未变更"}
          >
            {part.added ? (
              <Plus size={12} />
            ) : part.removed ? (
              <Minus size={12} />
            ) : (
              " "
            )}
          </span>
          <pre>{part.value}</pre>
        </div>
      ))}
    </div>
  );
}
export function StepLabel({
  number,
  children,
  done = false,
}: {
  number: string;
  children: ReactNode;
  done?: boolean;
}) {
  return (
    <div className={`step-label ${done ? "done" : ""}`}>
      <span>{done ? <Check size={12} /> : number}</span>
      {children}
      <ArrowRight size={13} />
    </div>
  );
}
