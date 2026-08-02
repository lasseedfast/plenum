import { useCallback } from "react";
import { useNavigate } from "react-router-dom";

export type SearchMode = "search" | "chat" | "research";

/**
 * The Sök / Chat / Research tab bar. Extracted from SearchPanel so the
 * research pages — which don't render a SearchPanel — can show it too; before,
 * navigating to /research made the tab bar that got you there disappear.
 */
export function ModeToggle({
	mode,
	onModeChange,
}: {
	mode: SearchMode;
	onModeChange: (mode: SearchMode) => void;
}) {
	return (
		<div className="mode-toggle" role="tablist" aria-label="Välj sökläge">
			<button
				type="button"
				role="tab"
				data-active={mode === "search"}
				aria-selected={mode === "search"}
				onClick={() => onModeChange("search")}
			>
				Sök
			</button>
			<button
				type="button"
				role="tab"
				data-active={mode === "chat"}
				aria-selected={mode === "chat"}
				onClick={() => onModeChange("chat")}
			>
				Chat
			</button>
			<button
				type="button"
				role="tab"
				data-active={mode === "research"}
				aria-selected={mode === "research"}
				onClick={() => onModeChange("research")}
			>
				Research
			</button>
		</div>
	);
}

/**
 * Where each mode lives. Chat gets a fresh session id, matching what the
 * search page's "start a chat" already did.
 *
 * Deliberately not a no-op for the current mode: picking "Research" while on
 * /research/:id takes you back to the board list, which is the useful move.
 */
export function useModeNavigation(): (mode: SearchMode) => void {
	const navigate = useNavigate();
	return useCallback(
		(mode: SearchMode) => {
			if (mode === "search") navigate("/");
			else if (mode === "research") navigate("/research");
			else {
				const uuid = crypto.randomUUID
					? crypto.randomUUID()
					: `${Date.now()}-${Math.random().toString(36).slice(2)}`;
				navigate(`/chat/${uuid}`);
			}
		},
		[navigate],
	);
}
