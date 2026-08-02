/**
 * Renders a research answer/report (markdown with `[källa:ID]` citation
 * markers) to sanitized HTML. Each marker becomes a clickable
 * `<button class="research-cite-chip" data-talk-id="ID">` — the source id is
 * the same bare talk id used by the finding chips, so the surrounding React
 * component can open the TalkDrawer via event delegation (a handler can't live
 * inside dangerouslySetInnerHTML). Only text outside <a>/<code>/<pre> is
 * processed.
 */
import { marked } from "marked";
import DOMPurify from "dompurify";
import type { ResearchFinding } from "../types";

export type CiteSource = { speaker?: string | null; party?: string | null; date?: string | null };

/** Build id → {speaker, party, date} from a thread's findings for chip labels. */
export function sourcesFromFindings(findings: ResearchFinding[] | undefined): Map<string, CiteSource> {
	const map = new Map<string, CiteSource>();
	for (const f of findings ?? []) {
		if (f.source_id && !map.has(f.source_id)) {
			map.set(f.source_id, { speaker: f.speaker, party: f.party, date: f.date });
		}
	}
	return map;
}

function chipLabel(id: string, src: CiteSource | undefined, fallbackNum: number): string {
	if (src) {
		const who = [src.speaker, src.party ? `(${src.party})` : ""].filter(Boolean).join(" ");
		const label = [who, src.date].filter(Boolean).join(" · ");
		if (label) return label;
	}
	return `källa ${fallbackNum}`;
}

export function renderResearchMarkdown(
	markdown: string,
	sources?: Map<string, CiteSource>,
): string {
	const rawHtml = marked.parse(markdown ?? "") as string;
	const parser = new DOMParser();
	const doc = parser.parseFromString(rawHtml, "text/html");
	const SKIP_TAGS = new Set(["A", "CODE", "PRE", "SCRIPT", "STYLE"]);

	// Collect candidate text nodes before mutating (mutating mid-walk makes the
	// TreeWalker skip siblings).
	const textNodes: Text[] = [];
	const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT);
	let node = walker.nextNode();
	while (node) {
		const parentEl = (node as Text).parentElement;
		if (parentEl && !SKIP_TAGS.has(parentEl.tagName) && /\[källa:/i.test(node.nodeValue ?? "")) {
			textNodes.push(node as Text);
		}
		node = walker.nextNode();
	}

	let seq = 0;
	const numById = new Map<string, number>();
	for (const textNode of textNodes) {
		const text = textNode.nodeValue!;
		const parts: Node[] = [];
		let lastIndex = 0;
		const regex = /\[källa:\s*([\w-]+)\s*\]/gi;
		let m: RegExpExecArray | null;
		while ((m = regex.exec(text)) !== null) {
			if (m.index > lastIndex) parts.push(doc.createTextNode(text.slice(lastIndex, m.index)));
			const id = m[1];
			let num = numById.get(id);
			if (num === undefined) {
				num = ++seq;
				numById.set(id, num);
			}
			const btn = doc.createElement("button");
			btn.setAttribute("type", "button");
			btn.setAttribute("class", "research-cite-chip");
			btn.setAttribute("data-talk-id", id);
			btn.textContent = chipLabel(id, sources?.get(id), num);
			parts.push(btn);
			lastIndex = m.index + m[0].length;
		}
		if (parts.length > 0) {
			if (lastIndex < text.length) parts.push(doc.createTextNode(text.slice(lastIndex)));
			const parent = textNode.parentNode!;
			for (const p of parts) parent.insertBefore(p, textNode);
			parent.removeChild(textNode);
		}
	}

	const sanitized = DOMPurify.sanitize(doc.body.innerHTML, {
		ADD_ATTR: ["data-talk-id"],
		RETURN_TRUSTED_TYPE: false,
	});
	return typeof sanitized === "string" ? sanitized : String(sanitized);
}
