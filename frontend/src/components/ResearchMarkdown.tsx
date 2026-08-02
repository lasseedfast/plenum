/**
 * Renders a research answer or report (markdown + [källa:ID] markers) with
 * clickable citation chips. The chips are plain buttons injected via
 * dangerouslySetInnerHTML, so clicks are caught by delegation on the wrapper
 * and routed to the TalkDrawer.
 */
import { useMemo } from "react";
import { useTalkDrawer } from "../context/TalkDrawerContext";
import { renderResearchMarkdown, type CiteSource } from "../utils/researchMarkdown";

type Props = {
	md: string;
	sources?: Map<string, CiteSource>;
	className?: string;
};

export function ResearchMarkdown({ md, sources, className }: Props) {
	const { openTalk } = useTalkDrawer();
	const html = useMemo(() => renderResearchMarkdown(md, sources), [md, sources]);

	const onClick = (e: React.MouseEvent<HTMLDivElement>) => {
		const chip = (e.target as HTMLElement).closest<HTMLElement>("[data-talk-id]");
		const id = chip?.getAttribute("data-talk-id");
		if (id) {
			e.preventDefault();
			openTalk(id);
		}
	};

	return (
		<div
			className={className ?? "research-markdown"}
			onClick={onClick}
			// eslint-disable-next-line react/no-danger
			dangerouslySetInnerHTML={{ __html: html }}
		/>
	);
}
