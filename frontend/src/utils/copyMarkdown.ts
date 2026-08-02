/**
 * Turns a chat answer or a research answer/report into portable markdown for
 * the clipboard.
 *
 * Both flavours carry citations the browser renders as chips/superscripts, which
 * are meaningless once pasted elsewhere. So on copy we rewrite every citation as
 * a real markdown link and append a numbered footnote section ("Källor") where
 * each entry is a markdown link to the source.
 *
 *   chat:     "…ökade stödet [1]."      → "…ökade stödet [[1]](https://riksdagen.se/…)."
 *   research: "…ökade stödet [källa:H40911]." → same shape, numbered by first appearance
 *
 * Chat sources come with `debateurl` (riksdagen.se) and are linked there;
 * research findings only carry the bare talk id, so those link into this app
 * (`/talk/<id>`), which is where the citation chip points on screen too.
 *
 * Below the footnotes we append a link back to the chat or research board itself,
 * so a pasted answer can always be traced to the conversation it came from.
 *
 * `markdownToClipboardHtml` renders the result a second time as HTML, for the
 * text/html clipboard flavour that Notes, Word and Google Docs paste from.
 */
import { marked } from "marked";
import DOMPurify from "dompurify";
import type { ChatSource } from "../types";
import type { CiteSource } from "./researchMarkdown";

/** One entry in the generated footnote section. */
type Footnote = {
	n: number;
	label: string;
	url: string | null;
	/** Extra context after the link (debate heading), when the label is a person. */
	note?: string | null;
};

const SOURCES_HEADING = /\n#{1,6}[ \t]*K[äa]ll[ao]r?[^\n]*\n?/gi;
const CHAT_CITE = /\[(\d{1,3})\]/g;
const RESEARCH_CITE = /\[källa:\s*([\w-]+)\s*\]/gi;

/**
 * Drop a trailing "Källor" section. The backend appends one deterministically
 * (`provenance.parse_and_renumber_citations`) as plain "[1] Speaker – date"
 * lines; the footnote section we generate instead carries the same information
 * with working links.
 */
function stripSourcesSection(markdown: string): string {
	let cut = -1;
	for (const m of markdown.matchAll(SOURCES_HEADING)) cut = m.index ?? cut;
	return (cut >= 0 ? markdown.slice(0, cut) : markdown).trimEnd();
}

/**
 * Apply `fn` to prose only, leaving fenced code blocks and inline code spans
 * untouched — a `[1]` inside a code sample is not a citation.
 */
function mapProse(markdown: string, fn: (text: string) => string): string {
	const parts = markdown.split(/(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`)/g);
	return parts.map((part, i) => (i % 2 === 1 ? part : fn(part))).join("");
}

/** Angle-bracket form for urls that would break `[text](url)` parsing. */
function mdUrl(url: string): string {
	return /[\s()<>]/.test(url) ? `<${url}>` : url;
}

function mdText(text: string): string {
	return text.replace(/([[\]])/g, "\\$1");
}

/** Absolute in-app url for a source id — "talks/H40911", "motions/HA02123" or bare. */
function appUrl(rawId: string | null | undefined): string | null {
	if (!rawId) return null;
	const origin = typeof window !== "undefined" ? window.location.origin : "";
	if (rawId.startsWith("motions/")) return `${origin}/motion/${rawId.slice("motions/".length)}`;
	const bare = rawId.startsWith("talks/") ? rawId.slice("talks/".length) : rawId;
	return bare ? `${origin}/talk/${bare}` : null;
}

/** "Anna Andersson (S) · 2021-03-04", falling back to heading, date, then id. */
function sourceLabel(
	who: { speaker?: string | null; party?: string | null; date?: string | null },
	fallback: string,
): { label: string; hasPerson: boolean } {
	const person = [who.speaker, who.party ? `(${who.party})` : ""].filter(Boolean).join(" ");
	if (person) return { label: [person, who.date].filter(Boolean).join(" · "), hasPerson: true };
	return { label: who.date || fallback, hasPerson: false };
}

/**
 * Trailing link back to where the copy came from, so a pasted answer can be
 * traced to its conversation. It is the live chat/board url (origin + path,
 * query and hash dropped) — not the public `/share/:uuid` snapshot. Copy buttons
 * only render on `/chat/:uuid`, `/mp/:id` and `/research/:id`, never on a
 * snapshot page, so the current location is always the real thing.
 */
function permalinkSection(linkLabel: string): string {
	if (typeof window === "undefined") return "";
	const url = `${window.location.origin}${window.location.pathname}`;
	return `\n\n---\n\n[${mdText(linkLabel)}](${mdUrl(url)})\n`;
}

function footnoteSection(footnotes: Footnote[]): string {
	if (footnotes.length === 0) return "";
	const lines = footnotes.map((f) => {
		const link = f.url ? `[${mdText(f.label)}](${mdUrl(f.url)})` : mdText(f.label);
		return `${f.n}. ${link}${f.note ? ` — ${mdText(f.note)}` : ""}`;
	});
	return `\n\n## Källor\n\n${lines.join("\n")}\n`;
}

/**
 * The markdown from the exporters above, rendered as standalone HTML for the
 * clipboard's text/html flavour. Links are already absolute, so pasted rich text
 * keeps working hyperlinks; inline citation links are superscripted so they read
 * as citations rather than stray brackets in a word processor.
 */
export function markdownToClipboardHtml(markdown: string): string {
	const doc = new DOMParser().parseFromString(marked.parse(markdown) as string, "text/html");
	for (const link of Array.from(doc.querySelectorAll("a"))) {
		const text = link.textContent?.trim() ?? "";
		if (!/^\[\d{1,3}\]$/.test(text)) continue;
		if (link.parentElement?.tagName === "SUP") continue;
		const sup = doc.createElement("sup");
		link.replaceWith(sup);
		sup.appendChild(link);
		link.textContent = text.slice(1, -1);
	}
	const sanitized = DOMPurify.sanitize(doc.body.innerHTML, { RETURN_TRUSTED_TYPE: false });
	return typeof sanitized === "string" ? sanitized : String(sanitized);
}

/**
 * Chat answer (`[1]`, `[2]` markers) → markdown. Citation numbers are 1-based
 * indexes into `sources`, the same mapping the on-screen renderer uses.
 */
export function chatAnswerToMarkdown(answer: string, sources?: ChatSource[]): string {
	const body = stripSourcesSection(answer ?? "");
	const footnotes: Footnote[] = (sources ?? []).map((src, i) => {
		const { label, hasPerson } = sourceLabel(src, src.heading || `Källa ${i + 1}`);
		return {
			n: i + 1,
			label,
			url: src.debateurl || appUrl(src._id),
			note: hasPerson ? src.heading : null,
		};
	});
	const byNumber = new Map(footnotes.map((f) => [f.n, f]));

	const linked = mapProse(body, (text) =>
		text.replace(CHAT_CITE, (whole, digits: string) => {
			const footnote = byNumber.get(Number(digits));
			if (!footnote?.url) return whole;
			return `[[${footnote.n}]](${mdUrl(footnote.url)})`;
		}),
	);
	return linked.trimEnd() + footnoteSection(footnotes) + permalinkSection("Öppna chatten");
}

/**
 * Research answer or report (`[källa:ID]` markers) → markdown. Footnotes are
 * numbered by first appearance, matching the fallback numbering of the chips.
 */
export function researchAnswerToMarkdown(
	markdown: string,
	sources?: Map<string, CiteSource>,
): string {
	const body = stripSourcesSection(markdown ?? "");
	const footnotes: Footnote[] = [];
	const numById = new Map<string, number>();

	const linked = mapProse(body, (text) =>
		text.replace(RESEARCH_CITE, (_whole, id: string) => {
			let n = numById.get(id);
			if (n === undefined) {
				n = footnotes.length + 1;
				numById.set(id, n);
				const { label } = sourceLabel(sources?.get(id) ?? {}, `Källa ${n}`);
				footnotes.push({ n, label, url: appUrl(id) });
			}
			const url = footnotes[n - 1].url;
			return url ? `[[${n}]](${mdUrl(url)})` : `[${n}]`;
		}),
	);
	return linked.trimEnd() + footnoteSection(footnotes) + permalinkSection("Öppna researchen");
}
