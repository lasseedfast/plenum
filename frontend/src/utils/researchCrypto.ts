/**
 * Client-side decryption of encrypted research boards. The server stores and
 * serves ciphertext; everything the user reads is decrypted here with the
 * unwrapped per-board key (see AuthContext.getBoardKey).
 */
import { decryptMaybe, decryptString, isEncrypted } from "../crypto";
import type {
	ResearchBoardDetail,
	ResearchBoardSummary,
	ResearchEvent,
	ResearchThread,
} from "../types";

async function decryptJsonField<T>(key: CryptoKey, value: unknown, fallback: T): Promise<T> {
	if (!isEncrypted(value)) return (value as T) ?? fallback;
	try {
		return JSON.parse(await decryptString(key, value)) as T;
	} catch {
		return fallback;
	}
}

async function tryDecrypt(key: CryptoKey, value: string | null | undefined): Promise<string | null | undefined> {
	if (value == null || !isEncrypted(value)) return value;
	try {
		return await decryptMaybe(key, value);
	} catch {
		return "🔒 (kunde inte avkryptera)";
	}
}

export async function decryptBoardSummary(
	row: ResearchBoardSummary,
	key: CryptoKey,
): Promise<ResearchBoardSummary> {
	return {
		...row,
		title: (await tryDecrypt(key, row.title)) ?? row.title,
		topic: (await tryDecrypt(key, row.topic)) ?? row.topic,
	};
}

export async function decryptThread(thread: ResearchThread, key: CryptoKey): Promise<ResearchThread> {
	return {
		...thread,
		title: (await tryDecrypt(key, thread.title)) ?? thread.title,
		question: (await tryDecrypt(key, thread.question)) ?? thread.question,
		why: (await tryDecrypt(key, thread.why)) ?? thread.why,
		guidance: await tryDecrypt(key, thread.guidance),
		answer: await tryDecrypt(key, thread.answer),
		findings: await decryptJsonField(key, thread.findings, []),
		open_questions: await decryptJsonField(key, thread.open_questions, []),
		leads: await decryptJsonField(key, thread.leads, []),
		hints: await decryptJsonField(key, thread.hints, []),
	};
}

export async function decryptBoardDetail(
	board: ResearchBoardDetail,
	key: CryptoKey,
): Promise<ResearchBoardDetail> {
	const job = board.job
		? {
				...board.job,
				progress: board.job.progress
					? {
							...board.job.progress,
							current: (await tryDecrypt(key, board.job.progress.current)) ?? "",
						}
					: board.job.progress,
			}
		: board.job;
	return {
		...board,
		title: (await tryDecrypt(key, board.title)) ?? board.title,
		topic: (await tryDecrypt(key, board.topic)) ?? board.topic,
		intro: await tryDecrypt(key, board.intro),
		report: await tryDecrypt(key, board.report),
		threads: await Promise.all(board.threads.map((t) => decryptThread(t, key))),
		job,
	};
}

/** Encrypted events carry {done, total, enc}; unfold enc back into the event. */
export async function decryptEvent(event: ResearchEvent, key: CryptoKey | null): Promise<ResearchEvent> {
	if (!event.enc || !key) return event;
	try {
		const content = JSON.parse(await decryptString(key, event.enc)) as Partial<ResearchEvent>;
		return { done: event.done, total: event.total, ...content };
	} catch {
		return { done: event.done, total: event.total };
	}
}
