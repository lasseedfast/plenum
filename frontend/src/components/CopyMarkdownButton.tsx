/**
 * Copies an answer or report to the clipboard in two flavours at once: markdown
 * as text/plain, and the same content as text/html so Notes, Word and Google Docs
 * paste it as formatted text with live links. Both are built on click (not on
 * render) so the conversion costs nothing until the user asks for it.
 */
import { useEffect, useRef, useState, type MouseEvent } from "react";
import { copyRichText } from "../utils/clipboard";
import { markdownToClipboardHtml } from "../utils/copyMarkdown";

type Props = {
	getMarkdown: () => string;
	/** Visible text next to the icon; icon-only when omitted. */
	label?: string;
	className?: string;
};

const RESET_MS = 2000;

const TITLES = {
	idle: "Kopiera — klistras in som markdown eller formaterad text",
	copied: "Kopierat",
	error: "Kunde inte kopiera",
} as const;

/** 24×24 stroke icons, drawn with currentColor so they follow the button state. */
const ICON_PATHS = {
	// The familiar copy glyph: a rounded square in front, a second one behind it.
	idle: (
		<>
			<rect x="8" y="8" width="14" height="14" rx="2.5" />
			<path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2" />
		</>
	),
	copied: <path d="M20 6 9 17l-5-5" />,
	error: (
		<>
			<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3" />
			<path d="M12 9v4" />
			<path d="M12 17h.01" />
		</>
	),
} as const;

export function CopyMarkdownButton({ getMarkdown, label, className }: Props) {
	const [state, setState] = useState<keyof typeof TITLES>("idle");
	const timer = useRef<number | null>(null);

	useEffect(() => () => {
		if (timer.current) window.clearTimeout(timer.current);
	}, []);

	const handleClick = async (e: MouseEvent<HTMLButtonElement>) => {
		// Chat cards select their turn when clicked — copying must not do that too.
		e.stopPropagation();
		try {
			const markdown = getMarkdown();
			await copyRichText(markdownToClipboardHtml(markdown), markdown);
			setState("copied");
		} catch {
			setState("error");
		}
		if (timer.current) window.clearTimeout(timer.current);
		timer.current = window.setTimeout(() => setState("idle"), RESET_MS);
	};

	return (
		<button
			type="button"
			className={className ? `copy-md-button ${className}` : "copy-md-button"}
			data-state={state}
			onClick={handleClick}
			title={TITLES[state]}
			aria-label={TITLES[state]}
		>
			<svg
				className="copy-md-button__icon"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				strokeWidth="2"
				strokeLinecap="round"
				strokeLinejoin="round"
				aria-hidden="true"
			>
				{ICON_PATHS[state]}
			</svg>
			{label && (
				<span className="copy-md-button__label">{state === "copied" ? "Kopierat" : label}</span>
			)}
			<span className="visually-hidden" aria-live="polite">
				{state === "copied" ? TITLES.copied : state === "error" ? TITLES.error : ""}
			</span>
		</button>
	);
}
