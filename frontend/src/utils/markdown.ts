import { marked } from "marked";
import DOMPurify from "dompurify";
import type { ChatSource } from "../types";

// Set once from /api/meta (urls.person_photo). The Swedish default keeps portraits
// rendering during the first paint, before meta has arrived.
let photoUrlTemplate = "https://data.riksdagen.se/filarkiv/bilder/ledamot/{person_id}_192.jpg";

export const setPhotoUrlTemplate = (template: string): void => {
    if (template) photoUrlTemplate = template;
};

export const getMpPhotoUrl = (personId: string): string =>
    photoUrlTemplate.replace("{person_id}", personId);

/**
 * Converts Markdown to HTML and replaces [1], [2], ... with <sup> citations.
 * When a sources array is provided, each citation becomes a clickable link to
 * /talk/{id} (anföranden) or /motion/{id} (motioner, id prefixed "motions/").
 * Only processes text outside of <a>, <code>, <pre>, <script>, <style>, and <sup> tags.
 */
export const convertMarkdownToHtml = (markdown: string, sources?: ChatSource[]): string => {
    // Build citation-number → /talk/{id} or /motion/{id} map from sources array.
    const talkPathByIndex = new Map<number, string>();
    if (sources) {
        sources.forEach((src, i) => {
            const raw = src._id ?? "";
            if (raw.startsWith("motions/")) {
                talkPathByIndex.set(i + 1, `/motion/${raw.slice("motions/".length)}`);
            } else {
                const key = raw.startsWith("talks/") ? raw.slice(6) : raw;
                if (key) talkPathByIndex.set(i + 1, `/talk/${key}`);
            }
        });
    }

    const rawHtml = marked.parse(markdown) as string;
    const parser = new DOMParser();
    const doc = parser.parseFromString(rawHtml, "text/html");
    const SKIP_TAGS = new Set(["A", "CODE", "PRE", "SCRIPT", "STYLE", "SUP"]);

    // Collect all candidate text nodes BEFORE mutating the DOM.
    // Walking and mutating simultaneously causes the TreeWalker to lose its
    // position and skip sibling nodes after each removal.
    const textNodes: Text[] = [];
    const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT);
    let node = walker.nextNode();
    while (node) {
        const parentEl = (node as Text).parentElement;
        if (
            parentEl &&
            !SKIP_TAGS.has(parentEl.tagName) &&
            node.nodeValue?.trim()
        ) {
            textNodes.push(node as Text);
        }
        node = walker.nextNode();
    }

    for (const textNode of textNodes) {
        const text = textNode.nodeValue!;
        const parts: Node[] = [];
        let lastIndex = 0;
        const regex = /\[(\d+)\]/g;
        let m: RegExpExecArray | null;
        while ((m = regex.exec(text)) !== null) {
            if (m.index > lastIndex) parts.push(doc.createTextNode(text.slice(lastIndex, m.index)));
            const sup = doc.createElement("sup");
            const n = parseInt(m[1], 10);
            const talkPath = talkPathByIndex.get(n);
            if (talkPath) {
                const a = doc.createElement("a");
                a.setAttribute("href", talkPath);
                a.setAttribute("class", "chat-cite-link");
                a.textContent = m[1];
                sup.appendChild(a);
            } else {
                sup.textContent = m[1];
            }
            parts.push(sup);
            lastIndex = m.index + m[0].length;
        }
        if (parts.length > 0) {
            if (lastIndex < text.length) parts.push(doc.createTextNode(text.slice(lastIndex)));
            const parent = textNode.parentNode!;
            for (const p of parts) parent.insertBefore(p, textNode);
            parent.removeChild(textNode);
        }
    }
    // In the "Källor" section, make the descriptive text after each ¹ ² link
    // also clickable (not just the tiny superscript number).
    if (talkPathByIndex.size > 0) {
        const headers = Array.from(doc.querySelectorAll("h1,h2,h3,h4,h5,h6"));
        const kallorHeader = headers.find(h => /k[äa]ll[ao]r?/i.test(h.textContent ?? ""));
        if (kallorHeader) {
            let el = kallorHeader.nextElementSibling;
            while (el && !/^H[1-6]$/.test(el.tagName)) {
                // Each <sup><a class="chat-cite-link" href="X">N</a></sup> followed by
                // text nodes until the next <sup> or <br> — wrap that text in <a href="X">.
                const sups = Array.from(el.querySelectorAll("sup a.chat-cite-link"));
                for (const supA of sups) {
                    const href = supA.getAttribute("href");
                    if (!href) continue;
                    const sup = supA.parentElement!;
                    // Collect text/inline nodes that follow until next <sup> or <br>.
                    const following: ChildNode[] = [];
                    let cur = sup.nextSibling;
                    while (cur && cur.nodeName !== "SUP" && cur.nodeName !== "BR") {
                        following.push(cur);
                        cur = cur.nextSibling;
                    }
                    if (following.length === 0) continue;
                    const a = doc.createElement("a");
                    a.setAttribute("href", href);
                    a.setAttribute("class", "chat-source-link");
                    // Insert the <a> before the first collected node, then move nodes into it.
                    sup.parentNode!.insertBefore(a, following[0]);
                    for (const n of following) a.appendChild(n);
                }
                el = el.nextElementSibling;
            }
        }
    }

    const htmlWithFootnotes = doc.body.innerHTML;
    const sanitized = DOMPurify.sanitize(htmlWithFootnotes, { RETURN_TRUSTED_TYPE: false });
    return typeof sanitized === "string" ? sanitized : String(sanitized);
};
