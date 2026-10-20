import { useMemo } from "react";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { Modal } from "./ui";
import guide from "../../docs/studio_user_guide.md?raw";
import "./userGuide.css";

type Destination = "creation" | "planning" | "analysis" | "library";
export default function UserGuide({ onClose, onNavigate }: {
  onClose: () => void;
  onNavigate: (destination: Destination) => boolean;
}) {
  const html = useMemo(() => {
    const page = new DOMParser().parseFromString(DOMPurify.sanitize(marked.parse(guide, { async: false }) as string), "text/html");
    // Repository document links are useful in Markdown, but are not web routes.
    page.querySelectorAll("a[href]").forEach((link) => {
      if (!/^(https?:\/\/|#)/i.test(link.getAttribute("href") || "")) link.replaceWith(document.createTextNode(link.textContent || ""));
    });
    return page.body.innerHTML;
  }, []);
  return <Modal title="Studio 使用手册" subtitle="按创作流程查找入口；关闭手册会回到当前页面。" onClose={onClose} wide>
    <div className="guide-shortcuts" aria-label="常用功能入口">
      {([ ["creation", "从零构思"], ["planning", "创建规划包"], ["analysis", "写前包与审稿"], ["library", "资料与参考书"] ] as const).map(([destination, label]) =>
        <button type="button" className="button secondary small" key={destination} onClick={() => { if (onNavigate(destination)) onClose(); }}>{label}</button>)}
    </div>
    <article className="markdown-body guide-document" dangerouslySetInnerHTML={{ __html: html }} />
  </Modal>;
}
