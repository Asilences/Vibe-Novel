export type TaskReference = {id: string; novelId: string; selection: Record<string, unknown>} | null;

export function taskReferenceIdentity(reference: TaskReference): string {
  return reference ? JSON.stringify([reference.id, reference.novelId, reference.selection]) : "";
}

export function consumeTaskReference(current: TaskReference, used: TaskReference): TaskReference {
  if (!current || !used) return current;
  return current.id === used.id && current.novelId === used.novelId && taskReferenceIdentity(current) === taskReferenceIdentity(used) ? null : current;
}

export function referenceUsedBySelection(reference: TaskReference, selection: Record<string, unknown> | null): TaskReference {
  if (!reference || !selection) return null;
  return Object.entries(reference.selection).every(([key, value]) => JSON.stringify(selection[key]) === JSON.stringify(value)) ? reference : null;
}

/** Retiring a pending ticket also retires its copied retrieval, not other materials. */
export function retireReferenceRetrieval(selection: Record<string, unknown> | null, previous: TaskReference, current: TaskReference): Record<string, unknown> | null {
  if (selection === null || !previous || taskReferenceIdentity(previous) === taskReferenceIdentity(current)) return selection;
  const oldRetrieval = previous.selection.retrieval_selection;
  if (!oldRetrieval || JSON.stringify(selection.retrieval_selection) !== JSON.stringify(oldRetrieval)) return selection;
  if (current?.novelId === previous.novelId && JSON.stringify(current.selection.retrieval_selection) === JSON.stringify(selection.retrieval_selection)) return selection;
  const next = { ...selection };
  delete next.retrieval_selection;
  // {} deliberately overrides the global pending selection; null would follow it again.
  return next;
}
