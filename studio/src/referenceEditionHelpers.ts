export type EditionSegment = { chapter_id: string; start_char: number; end_char: number };
export type EditionChapter = { title: string; kind: "body" | "ancillary"; volume_title: string; segments: EditionSegment[] };

/** Split an ordered composition without dropping or duplicating any source range. */
export function splitEditionChapter(chapter: EditionChapter, segmentIndex: number, at: number): [EditionChapter, EditionChapter] {
  if (!Number.isSafeInteger(segmentIndex) || segmentIndex < 0 || segmentIndex >= chapter.segments.length ||
      !Number.isSafeInteger(at) || chapter.segments.some((segment) => !Number.isSafeInteger(segment.start_char) ||
        !Number.isSafeInteger(segment.end_char) || segment.start_char < 0 || segment.start_char >= segment.end_char)) {
    throw new Error("请选择有效来源段，并填写该段内的 Unicode 码点位置。");
  }
  const segment = chapter.segments[segmentIndex];
  if (at < segment.start_char || at > segment.end_char) {
    throw new Error(`拆分位置须在所选来源段的 ${segment.start_char}–${segment.end_char} 范围内。`);
  }
  const before = chapter.segments.slice(0, segmentIndex).map((item) => ({ ...item }));
  const after = chapter.segments.slice(segmentIndex + 1).map((item) => ({ ...item }));
  if (at > segment.start_char) before.push({ ...segment, end_char: at });
  if (at < segment.end_char) after.unshift({ ...segment, start_char: at });
  if (!before.length || !after.length) throw new Error("拆分后两段都须保留正文；不能在整段的最前或最后拆分。");
  return [
    { ...chapter, segments: before },
    { ...chapter, title: `${Array.from(chapter.title).slice(0, 495).join("")} · 后段`, segments: after },
  ];
}
