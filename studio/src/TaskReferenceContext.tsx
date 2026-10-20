import { createContext, useContext } from "react";
import type { TaskReference } from "./taskReferenceHelpers";
export type { TaskReference } from "./taskReferenceHelpers";
export const TaskReferenceContext = createContext<{pending: TaskReference; consume: (used: TaskReference) => void}>({pending:null,consume:()=>{}});
export function useTaskReference(novelId: string) {
  const context = useContext(TaskReferenceContext);
  const ticket = context.pending?.novelId === novelId ? context.pending : null;
  return {ticket, selection:ticket?.selection || null, consume: (used: TaskReference = ticket) => {if(used?.novelId === novelId) context.consume(used);}};
}
