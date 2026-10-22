import { useEffect, useRef, useState } from "react";
import { Modal } from "./ui";

type ConfirmationOptions = {
  title?: string;
  confirmLabel?: string;
  cancelLabel?: string;
};

type Confirmation = {
  id: number;
  message: string;
  title: string;
  confirmLabel: string;
  cancelLabel: string;
  resolve: (accepted: boolean) => void;
};

const pending: Confirmation[] = [];
const listeners = new Set<() => void>();
let nextId = 0;

function currentConfirmation() {
  return pending[0] || null;
}

function publish() {
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
    // An unmounted application cannot obtain consent. Resolve every waiter as
    // cancelled rather than leaving a navigation or save operation suspended.
    if (!listeners.size) {
      const abandoned = pending.splice(0);
      abandoned.forEach((request) => request.resolve(false));
    }
  };
}

function settleConfirmation(id: number, accepted: boolean) {
  if (pending[0]?.id !== id) return;
  const request = pending.shift()!;
  publish();
  request.resolve(accepted === true);
}

/** Ask in the application UI; unavailable or dismissed confirmation cancels. */
export function requestConfirmation(message: string, options: ConfirmationOptions = {}): Promise<boolean> {
  if (!listeners.size || !message.trim()) return Promise.resolve(false);
  return new Promise((resolve) => {
    pending.push({ id: ++nextId, message,
      title: options.title?.trim() || "确认是否放弃修改",
      confirmLabel: options.confirmLabel?.trim() || "放弃修改",
      cancelLabel: options.cancelLabel?.trim() || "继续编辑",
      resolve });
    publish();
  });
}

/** One host remains mounted after all ordinary dialogs, preserving modal order. */
export function ConfirmationHost() {
  const [request, setRequest] = useState<Confirmation | null>(currentConfirmation);
  const cancelButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const unsubscribe = subscribe(() => setRequest(currentConfirmation()));
    setRequest(currentConfirmation());
    return unsubscribe;
  }, []);
  useEffect(() => {
    if (request) cancelButton.current?.focus();
  }, [request?.id]);
  if (!request) return null;
  const cancel = () => settleConfirmation(request.id, false);
  return <Modal key={request.id} title={request.title} onClose={cancel}>
    <p className="confirmation-message">{request.message}</p>
    <div className="modal-footer">
      <button ref={cancelButton} type="button" className="button secondary" onClick={cancel}>{request.cancelLabel}</button>
      <button type="button" className="button primary" onClick={() => settleConfirmation(request.id, true)}>{request.confirmLabel}</button>
    </div>
  </Modal>;
}
