/**
 * Turning stored turns back into turns the UI can render.
 *
 * A snapshot deliberately keeps only what a share has to carry — question,
 * answerHtml, sources (see SnapshotTurn). A ChatTurn is a richer thing: the
 * views switch on `status`, and ChatPanel renders the answer out of a
 * `liveCards` answer card rather than out of `answerHtml` directly. Forking
 * casts the stored shape straight to ChatTurn, which type-checks and lies —
 * the turn arrives with no status and no cards, matches none of the
 * pending/error/ready branches, and draws as a question with nothing under it.
 *
 * Hydration is idempotent: a turn that already carries a status is a live turn
 * from the owner's own saved session and passes through untouched.
 */
import type { ChatTurn, MpChatTurn, ResearchCard } from "../types";

function newId(): string {
	return crypto.randomUUID
		? crypto.randomUUID()
		: `turn-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function hydrateChatTurns(stored: readonly Partial<ChatTurn>[] | undefined): ChatTurn[] {
	return (stored ?? []).map(turn => {
		if (turn.status) return turn as ChatTurn;
		// ChatPanel reads the answer off a card, so rebuild the one the live
		// path would have left behind when the answer landed.
		const answerCard: ResearchCard = {
			id: newId(),
			message: "",
			isAnswer: true,
			answerHtml: turn.answerHtml ?? "",
		};
		return {
			...turn,
			id: turn.id ?? newId(),
			question: turn.question ?? "",
			createdAt: turn.createdAt ?? new Date().toISOString(),
			status: "ready",
			liveCards: turn.liveCards?.length
				? turn.liveCards
				: turn.answerHtml
					? [answerCard]
					: [],
		} as ChatTurn;
	});
}

/** MpChatPanel renders turn.answerHtml directly, so only the status is missing. */
export function hydrateMpChatTurns(stored: readonly Partial<MpChatTurn>[] | undefined): MpChatTurn[] {
	return (stored ?? []).map(turn =>
		turn.status
			? (turn as MpChatTurn)
			: ({
					...turn,
					id: turn.id ?? newId(),
					question: turn.question ?? "",
					status: "ready",
				} as MpChatTurn),
	);
}
