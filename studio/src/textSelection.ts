/** Map a textarea's normalized UTF-16 caret back to original Unicode characters.
 * Browsers display CRLF and lone CR as LF; Python's source ranges retain CRLF.
 * A programmatic caret inside a surrogate pair expands to the whole character.
 */
export function originalCharacterOffset(source: string, domOffset: number, edge: "start" | "end" = "start"): number {
  const target = Math.max(0, Math.floor(domOffset));
  let displayed = 0;
  let original = 0;
  for (let index = 0; index < source.length;) {
    if (target <= displayed) return original;
    const point = source.codePointAt(index)!;
    const utf16Width = point > 0xffff ? 2 : 1;
    const crlf = point === 13 && source.charCodeAt(index + 1) === 10;
    const originalWidth = crlf ? 2 : 1;
    const displayedWidth = point === 13 ? 1 : utf16Width;
    if (target < displayed + displayedWidth) return original + (edge === "end" ? originalWidth : 0);
    displayed += displayedWidth;
    original += originalWidth;
    index += crlf ? 2 : utf16Width;
  }
  return original;
}

export function originalSelectionRange(source: string, selectionStart: number, selectionEnd: number) {
  return { start_char: originalCharacterOffset(source, selectionStart, "start"),
           end_char: originalCharacterOffset(source, selectionEnd, "end") };
}

export type BoundTextSelection = { novelId: string; path: string; hash: string; start_char: number; end_char: number };

/** A range belongs to one saved document, never to unsaved or replaced text. */
export function savedSelectionMatches(selection: BoundTextSelection | null, novelId: string | undefined, document: { path: string; hash: string; content: string } | null, currentText: string, listedHash: string | undefined): boolean {
  return !!selection && !!novelId && !!document && selection.novelId === novelId && selection.path === document.path && selection.hash === document.hash && listedHash === document.hash && currentText === document.content && Number.isInteger(selection.start_char) && Number.isInteger(selection.end_char) && 0 <= selection.start_char && selection.start_char < selection.end_char && selection.end_char <= Array.from(document.content).length;
}

/** Match Python str.splitlines(), used by manuscript chapter boundaries. */
export function splitSourceLines(source: string): string[] {
  if (!source) return [];
  const lines = source.split(/\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/);
  if (/[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]$/.test(source)) lines.pop();
  return lines;
}
