import axios from "axios";
import type { MetaResponse, SearchRequest, SearchResponse, ChatRequest, ChatResponse, ChatMessage, ChatReply, PersonDetail, ChatSessionData, SessionUpsertData, SnapshotData, SnapshotTurn, ResearchBoardSummary, ResearchBoardDetail, ResearchEventsResponse, ResearchSpawnExtras, ResearchThread, ResearchLead, AuthResponse, PreloginResponse, MyChatRow } from "./types";

const client = axios.create({
	baseURL: "/api",
});

let activeSessionId: string | null = null;
let authToken: string | null = null;

export const AUTH_TOKEN_STORAGE_KEY = "riksdagen-auth-token";

/**
 * Persist the caller’s session id so every request can be associated with the same browser.
 */
export function setSessionId(sessionId: string): void {
	activeSessionId = sessionId;
	client.defaults.headers.common["X-Session-Id"] = sessionId;
}

/**
 * Attach (or drop) the account bearer token on every subsequent request.
 * The token is opaque — it never contains or reveals any encryption key.
 */
export function setAuthToken(token: string | null): void {
	authToken = token;
	if (token) client.defaults.headers.common["Authorization"] = `Bearer ${token}`;
	else delete client.defaults.headers.common["Authorization"];
}

// Eagerly restore the session id + auth token from localStorage at module
// load, so requests fired before App's mount effect runs (e.g. the /research
// list query) already carry X-Session-Id/Authorization.
try {
	if (typeof window !== "undefined") {
		const stored = window.localStorage.getItem("riksdagen-session-id");
		if (stored) setSessionId(stored);
		const token = window.localStorage.getItem(AUTH_TOKEN_STORAGE_KEY);
		if (token) setAuthToken(token);
	}
} catch {
	/* localStorage unavailable — App's effect will set it */
}

/**
 * Provide fetch-compatible headers carrying the current session id + auth token.
 */
export function getSessionHeaders(): Record<string, string> {
	const headers: Record<string, string> = {};
	if (activeSessionId) headers["X-Session-Id"] = activeSessionId;
	if (authToken) headers["Authorization"] = `Bearer ${authToken}`;
	return headers;
}

/* ── Account auth (zero-knowledge: only derived keys ever sent) ── */

export async function preLogin(username: string): Promise<PreloginResponse> {
	const { data } = await client.get<PreloginResponse>("/auth/prelogin", { params: { username } });
	return data;
}

export async function signupAccount(payload: {
	username: string;
	auth_key: string;
	kdf_salt: string;
	kdf_iterations: number;
	wrapped_dek: string;
}): Promise<AuthResponse> {
	const { data } = await client.post<AuthResponse>("/auth/signup", payload);
	return data;
}

export async function loginAccount(payload: { username: string; auth_key: string }): Promise<AuthResponse> {
	const { data } = await client.post<AuthResponse>("/auth/login", payload);
	return data;
}

export async function logoutAccount(): Promise<void> {
	await client.post("/auth/logout").catch(() => {});
}

export async function fetchMyChats(): Promise<MyChatRow[]> {
	const { data } = await client.get<MyChatRow[]>("/me/chats");
	return data;
}

export async function deleteMyChat(id: string): Promise<void> {
	await client.delete(`/me/chats/${encodeURIComponent(id)}`);
}

export async function fetchMeta(): Promise<MetaResponse> {
	const { data } = await client.get<MetaResponse>("/meta");
	return data;
}

export async function fetchGuide(): Promise<string> {
	const response = await fetch("/api/guide", { headers: getSessionHeaders() });
	if (!response.ok) throw new Error(`${response.status}`);
	return response.text();
}

export async function searchTalks(payload: SearchRequest): Promise<SearchResponse> {
	const { data } = await client.post<SearchResponse>("/search", payload);
	return data;
}

export async function sendFeedback(message: string): Promise<void> {
	await client.post("/feedback", { message });
}

export async function chatWithRiksdagen(payload: ChatRequest): Promise<ChatResponse> {
	const response = await fetch("/api/chat", {
		method: "POST",
		headers: {
			"Content-Type": "application/json",
			...getSessionHeaders(),
		},
		body: JSON.stringify(payload),
	});
	if (!response.ok) {
		throw new Error("Chat request failed");
	}
	return response.json();
}

export async function sendChat(
	messages: ChatMessage[],
	focusIds: string[],
): Promise<ChatReply> {
	const response = await fetch("/api/chat", {
		method: "POST",
		headers: {
			"Content-Type": "application/json",
			...getSessionHeaders(),
		},
		body: JSON.stringify({
			messages,
			focus_ids: focusIds,
			session_id: activeSessionId,
		}),
	});
	if (!response.ok) {
		throw new Error("Chat request failed");
	}
	const data = await response.json();
	return {
		answer: data.answer,
		sources: data.sources ?? [],
		tables: data.tables ?? [],
		focus_ids: data.focus_ids ?? [],
	};
}

/**
 * Fetches a single talk by its ID, including person information.
 *
 * @param id - The talk ID (e.g., "H40911")
 * @returns Promise containing the talk data with person info
 */
export async function fetchTalk(id: string): Promise<any> {
	const response = await fetch(`/api/talk/${encodeURIComponent(id)}`, {
		headers: getSessionHeaders(),
	});
	if (!response.ok) {
		throw new Error(`Failed to fetch talk: ${response.status}`);
	}
	return response.json();
}

/**
 * Fetches a single motion by its doc_id, including authors and yrkanden.
 *
 * @param id - The motion doc_id (e.g., "HD02846"), with or without "documents/" prefix
 */
export async function fetchMotion(id: string): Promise<any> {
	const bare = id.startsWith("documents/") ? id.slice("documents/".length) : id;
	const response = await fetch(`/api/motion/${encodeURIComponent(bare)}`, {
		headers: getSessionHeaders(),
	});
	if (!response.ok) {
		throw new Error(`Failed to fetch motion: ${response.statusText}`);
	}
	return response.json();
}

/**
 * Load a chat session by UUID. Returns null if not found or expired.
 */
export async function getSession(uuid: string): Promise<ChatSessionData | null> {
	const response = await fetch(`/api/sessions/${encodeURIComponent(uuid)}`, {
		headers: getSessionHeaders(),
	});
	if (response.status === 404) return null;
	if (!response.ok) throw new Error(`Failed to load session: ${response.statusText}`);
	return response.json();
}

/**
 * Create or update a chat session. Fire-and-forget safe (errors are logged only).
 * Logged-in callers pass enc_payload/enc_title (client-encrypted) instead of
 * the plaintext fields.
 */
export async function upsertSession(uuid: string, data: SessionUpsertData): Promise<void> {
	await fetch(`/api/sessions/${encodeURIComponent(uuid)}`, {
		method: "PUT",
		headers: { "Content-Type": "application/json", ...getSessionHeaders() },
		body: JSON.stringify(data),
	});
}

/**
 * Create a frozen shareable snapshot of a chat. Returns the new snapshot UUID.
 */
export async function createSnapshot(data: {
	session_type: "general" | "mp";
	person_id?: string | null;
	initial_speech_id?: string | null;
	llm_messages?: unknown[];
	turns: SnapshotTurn[];
	focus_ids?: string[];
}): Promise<string> {
	const response = await fetch("/api/snapshots", {
		method: "POST",
		headers: { "Content-Type": "application/json", ...getSessionHeaders() },
		body: JSON.stringify(data),
	});
	if (!response.ok) throw new Error(`Failed to create snapshot: ${response.statusText}`);
	const json = await response.json();
	return json.id as string;
}

/**
 * Fork a snapshot into a new editable session. Returns the new session UUID.
 */
export async function forkSnapshot(snapshotId: string): Promise<string> {
	const response = await fetch(`/api/snapshots/${encodeURIComponent(snapshotId)}/fork`, {
		method: "POST",
		headers: getSessionHeaders(),
	});
	if (!response.ok) throw new Error(`Failed to fork snapshot: ${response.statusText}`);
	const json = await response.json();
	return json.session_id as string;
}

/**
 * Load a snapshot by UUID. Returns null if not found.
 */
export async function getSnapshot(uuid: string): Promise<SnapshotData | null> {
	const response = await fetch(`/api/snapshots/${encodeURIComponent(uuid)}`, {
		headers: getSessionHeaders(),
	});
	if (response.status === 404) return null;
	if (!response.ok) throw new Error(`Failed to load snapshot: ${response.statusText}`);
	return response.json();
}

/**
 * Fetches basic info for a Riksdag member by person_id.
 */
export async function fetchPerson(person_id: string): Promise<PersonDetail> {
	const { data } = await client.get<PersonDetail>(`/person/${encodeURIComponent(person_id)}`);
	return data;
}

/* ── Account settings (client-encrypted AI config) ──────────────── */

export async function getMySettings(): Promise<{ enc_settings: string | null; updated_at: string | null }> {
	const { data } = await client.get("/me/settings");
	return data;
}

export async function putMySettings(encSettings: string): Promise<void> {
	await client.put("/me/settings", { enc_settings: encSettings });
}

export async function deleteMySettings(): Promise<void> {
	await client.delete("/me/settings");
}

/* ── Deep research ─────────────────────────────────────────────── */

export async function createResearch(
	payload: { topic: string; title?: string; wrapped_board_key?: string } & ResearchSpawnExtras,
): Promise<{ board_id: string; job_id: string }> {
	const { data } = await client.post("/research", payload);
	return data;
}

export async function listResearch(): Promise<ResearchBoardSummary[]> {
	const { data } = await client.get<ResearchBoardSummary[]>("/research");
	return data;
}

export async function getResearchBoard(boardId: string): Promise<ResearchBoardDetail> {
	const { data } = await client.get<ResearchBoardDetail>(`/research/${encodeURIComponent(boardId)}`);
	return data;
}

export async function getResearchEvents(
	boardId: string,
	jobId: string,
	offset: number,
): Promise<ResearchEventsResponse> {
	const { data } = await client.get<ResearchEventsResponse>(
		`/research/${encodeURIComponent(boardId)}/events`,
		{ params: { job_id: jobId, offset } },
	);
	return data;
}

export async function seedResearchThread(
	boardId: string,
	payload: { text: string } & ResearchSpawnExtras,
): Promise<{ thread: ResearchThread; job_id: string | null }> {
	const { data } = await client.post(`/research/${encodeURIComponent(boardId)}/threads`, payload);
	return data;
}

export async function deepenResearch(
	boardId: string,
	payload: { thread_id?: string; lead?: ResearchLead; sweep?: boolean } & ResearchSpawnExtras,
): Promise<{ job_id: string }> {
	const { data } = await client.post(`/research/${encodeURIComponent(boardId)}/deepen`, payload);
	return data;
}

export async function activateResearchThreads(
	boardId: string,
	payload: { selections: { thread_id: string; guidance?: string }[]; dig?: boolean } & ResearchSpawnExtras,
): Promise<{ activated: string[]; job_id: string | null }> {
	const { data } = await client.post(
		`/research/${encodeURIComponent(boardId)}/threads/activate`,
		{ dig: true, ...payload },
	);
	return data;
}

export async function archiveResearchThread(
	boardId: string,
	threadId: string,
): Promise<{ archived: boolean }> {
	const { data } = await client.post(
		`/research/${encodeURIComponent(boardId)}/threads/${encodeURIComponent(threadId)}/archive`,
	);
	return data;
}

export async function generateResearchReport(
	boardId: string,
	payload: ResearchSpawnExtras = {},
): Promise<{ job_id: string }> {
	const { data } = await client.post(`/research/${encodeURIComponent(boardId)}/report`, payload);
	return data;
}

export async function cancelResearch(boardId: string): Promise<{ cancelled: boolean }> {
	const { data } = await client.post(`/research/${encodeURIComponent(boardId)}/cancel`);
	return data;
}

export async function deleteResearch(boardId: string): Promise<void> {
	await client.delete(`/research/${encodeURIComponent(boardId)}`);
}
