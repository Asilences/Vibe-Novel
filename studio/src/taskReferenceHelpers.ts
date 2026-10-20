export type TaskReference = {id: string; novelId: string; selection: Record<string, unknown>} | null;

export function taskReferenceIdentity(reference: TaskReference): string {
  return reference ? JSON.stringify([reference.id, reference.novelId, reference.selection]) : "";
}

export function consumeTaskReference(current: TaskReference, used: TaskReference): TaskReference {
  if (!current || !used) return current;
  return current.id === used.id && current.novelId === used.novelId && taskReferenceIdentity(current) === taskReferenceIdentity(used) ? null : current;
}
