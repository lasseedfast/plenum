export type TalkHit = {
	id: string;  // ArangoDB _id, but named 'id' in TypeScript
	_id?: string;  // Some endpoints (e.g. chat search cards) return the raw ArangoDB _id under this key instead
	doc_id?: string;  // Made optional since it might not always be present
	text: string;
	snippet?: string | null;
	snippet_long?: string | null;
	number?: number | null;
	debate_type?: string | null;
	speaker?: string | null;
	date?: string | null;
	year?: number | null;
	url_session?: string | null;
	party?: string | null;
	url_audio?: string | null;
	audio_start_seconds?: number | null;
	person_id?: string | null;
	title?: string | null;
	activity_type?: string | null;
	related_doc_id?: string | null;
};

export type SearchFilters = {
	parties: string[];
	people: string[];
	debates: string[];
	from_year?: number | null;
	to_year?: number | null;
};

export type SearchRequest = SearchFilters & {
	q: string;
	limit?: number;
	include_snippets?: boolean;
	speaker?: string | null;
	speaker_ids?: string[];  // Accept a list of speaker IDs for filtering
};

export type AggregatedStats = {
	per_party: Record<string, number>;
	per_year: Record<number, number>;
	total: number;
};

export type SearchResponse = {
	results: TalkHit[];
	stats: AggregatedStats;
	active_filters: SearchFilters;
	limit_reached: boolean;
	generated_at: string;
};

export type DebateType = {
	title: string;
	description: string;
};

export type Party = {
	code: string;
	name: string;
	color: string;
	active: boolean;
};

export type MetaResponse = {
	parliament: {
		name: string;
		name_en: string;
		country: string;
		data_start_year: number;
	};
	parties: Party[];
	party_defaults: { unknown_color: string; code_pattern: string };
	/** Brand tokens injected as CSS custom properties; see parliament.yaml `theme:`. */
	theme?: Record<string, string>;
	/** Keyed by the value stored in speeches.activity_type. */
	activity_types: Record<string, DebateType>;
	vocabulary: Record<string, string>;
	urls: Record<string, string>;
	site: {
		title: string;
		tagline: string;
		explainer: string;
		limit_warning: string;
		contact: { email: string | null; url: string | null };
	};
};

export type ChatMessage = {
	role: "system" | "user" | "assistant";
	content: string;
};

export type ChatSource = {
	_id: string;
	chunk_index: number;
	heading: string | null;
	url_video: string | null;
	snippet: string;
	score: number;
	speaker?: string | null;
	party?: string | null;
	person_id?: string | null;
	date?: string | null;
};

export type MotionAuthor = {
	name: string | null;
	party: string | null;
	role: string | null;
	person_id: string | null;
	first_name?: string | null;
	image_url_medium?: string | null;
	constituency?: string | null;
	status?: string | null;
};

export type MotionYrkande = {
	number?: string | null;
	text?: string | null;
	committee_recommendation?: string | null;
	chamber_decision?: string | null;
	handled_in?: string | null;
};

export type Motion = {
	kind: "motion";
	doc_id: string;
	speaker_name: string;
	party: string;
	date: string | null;
	title: string | null;
	subtitle: string | null;
	text: string | null;
	has_text: boolean;
	session_label: string | null;
	designation: string | null;
	subtype: string | null;
	committee: string | null;
	status: string | null;
	parties: string[];
	authors: MotionAuthor[];
	yrkanden: MotionYrkande[];
	url_pdf: string | null;
	url_html: string | null;
	person?: {
		image_url_medium?: string | null;
		first_name?: string | null;
		person_id?: string | null;
		constituency?: string | null;
		status?: string | null;
	} | null;
};

export type ProviderOverride = {
	provider_id: string;
	api_key: string;
	smart_model?: string;  // model for orchestrator + communicator
	fast_model?: string;   // model for summarisation (defaults to smart_model)
	editor_model?: string; // model for the editor pass (defaults to smart_model)
};

/**
 * Extras every research endpoint that spawns a job accepts. Both ride the
 * backend's stdin-only "secrets" channel and are never persisted: board_key
 * decrypts the board, llm points the job at the user's own provider.
 */
export type ResearchSpawnExtras = {
	board_key?: string;
	llm?: ProviderOverride;
};

export type ChatRequest = {
	messages: ChatMessage[];
	top_k: number;
	focus_ids?: string[]; // Optional narrowing of future searches.
	session_id?: string;  // Propagates the browser session to the backend.
	provider_override?: ProviderOverride; // User-supplied provider; key stored only in browser.
	use_editor?: boolean;                 // Run the editor fact-check + language pass.
};

export type ChatResponseTable = {
	results: TalkHit[];
	stats: {
		per_party: Record<string, number>;
		per_year: Record<number, number>;
		total: number;
	};
	limit_reached: boolean;
	return_snippets: boolean;
	focus_ids: string[];
};

export type LiveSearchCard = {
	type: "search_card";
	message?: string;      // insight text shown as card header (from shadow communicator)
	query: string;
	results: TalkHit[];
	total: number;
	limit_reached: boolean;
	stats?: {
		per_party: Record<string, number>;
		per_year: Record<number, number>;
	};
	speaker_ids?: string[];
	speaker_ids_context?: string;
};

export type LiveStatsCard = {
	type: "stats_card";
	message?: string;      // insight text shown as card header (from shadow communicator)
	rows: Record<string, string | number>[];
	speaker_ids?: string[];
	speaker_ids_context?: string;
};

export type LiveInsightCard = {
	type: "insight_card";
	message: string;
	sources?: Record<string, string>;  // talk ID → url_video
	speaker_ids: string[];
	speaker_ids_context?: string;
};

export type LiveCard = LiveSearchCard | LiveStatsCard | LiveInsightCard;

/**
 * One research step shown to the user during (and after) LLM research.
 * Starts as a "thinking" card with just a message, then gets upgraded to
 * a result card (search/stats) or the final answer card.
 */
export type ResearchCard = {
	id: string;
	message: string;       // LLM's narration ("Söker efter X…")
	result?: LiveCard;     // upgraded to this when surface_results fires
	isAnswer: boolean;     // upgraded to true when final answer arrives
	answerHtml?: string;   // rendered answer HTML (when isAnswer=true)
};

export type PersonRef = {
	person_id: string;
	name: string;
	party: string;
};

export type ChatReply = {
	answer: string;
	sources: ChatSource[];
	persons?: PersonRef[];
	tables?: ChatResponseTable[];
	focus_ids?: string[];
};

export type ChatResponse = ChatReply;

export type ChatTurn = {
	id: string;
	question: string;
	answer?: string;
	answerHtml?: string;
	sources?: ChatSource[];
	persons?: PersonRef[];
	tables?: ChatResponseTable[];
	liveCards?: ResearchCard[];
	createdAt: string;
	status: "pending" | "ready" | "error";
	errorMessage?: string;
};

export type Uppdrag = {
	typ?: string | null;
	organ_kod?: string | null;
	roll_kod?: string | null;
	status?: string | null;
	uppgift?: string | null;
	from?: string | null;
	tom?: string | null;
};

export type PersonDetail = {
	person_id: string;
	name: string;
	first_name?: string | null;
	last_name?: string | null;
	party?: string | null;
	constituency?: string | null;
	status?: string | null;
	image_url_medium?: string | null;
	birth_year?: string | null;
	uppdrag?: Uppdrag[] | null;
};

export type MpChatTurn = {
	id: string;
	question: string;
	/** Raw markdown answer, kept alongside the rendered HTML for markdown export. */
	answer?: string;
	answerHtml?: string;
	sources?: ChatSource[];
	status: "pending" | "ready" | "error";
	errorMessage?: string;
};

export type SnapshotTurn = {
	question: string;
	answerHtml: string;
	sources?: ChatSource[];
};

export type SnapshotData = {
	id: string;
	session_type: "general" | "mp";
	person_id?: string | null;
	initial_speech_id?: string | null;
	turns: SnapshotTurn[];
	llm_messages?: ChatMessage[];
	focus_ids?: string[];
	created_at: string;
};

export type ChatSessionData = {
	id: string;
	session_type: "general" | "mp";
	person_id?: string | null;
	initial_speech_id?: string | null;
	llm_messages: ChatMessage[];
	turns: ChatTurn[] | MpChatTurn[];
	focus_ids: string[];
	/** Owned sessions: all content as one client-encrypted blob; plaintext fields empty. */
	enc_payload?: string | null;
};

/** PUT body for /sessions — either the plaintext fields or the enc_* pair. */
export type SessionUpsertData = {
	session_type: "general" | "mp";
	person_id?: string | null;
	initial_speech_id?: string | null;
	llm_messages?: ChatMessage[];
	turns?: ChatTurn[] | MpChatTurn[] | SnapshotTurn[];
	focus_ids?: string[];
	enc_payload?: string;
	enc_title?: string;
};

/** Decrypted contents of ChatSessionData.enc_payload. */
export type EncSessionPayload = {
	llm_messages: ChatMessage[];
	turns: ChatTurn[] | MpChatTurn[];
	focus_ids: string[];
	person_id?: string | null;
	initial_speech_id?: string | null;
};

/** Decrypted contents of enc_title (list labels + MP link target). */
export type EncTitlePayload = {
	title: string;
	person_id?: string | null;
};

/* ── Accounts (zero-knowledge login) ───────────────────────────── */

export type PreloginResponse = {
	kdf_salt: string;
	kdf_iterations: number;
};

export type AuthResponse = {
	token: string;
	user_id: string;
	username: string;
	wrapped_dek: string;
	kdf_salt: string;
	kdf_iterations: number;
};

export type MyChatRow = {
	id: string;
	session_type: "general" | "mp";
	enc_title: string | null;
	created_at: string;
	last_activity: string;
};

/* ── Deep research ─────────────────────────────────────────────── */

export type ResearchFinding = {
	label: string;
	detail?: string;
	quote?: string;
	source_id?: string;
	speaker?: string | null;
	party?: string | null;
	date?: string | null;
};

export type ResearchLead = {
	kind: "search" | "person" | "debate";
	target: string;
	lead?: string;
	label?: string | null;
};

export type ResearchThread = {
	id: string;
	title: string;
	question: string;
	why?: string;
	origin: "auto" | "seed";
	depth: number;
	status: "proposed" | "active" | "archived";
	pinned: boolean;
	findings: ResearchFinding[];
	open_questions: string[];
	leads: ResearchLead[];
	/** User's free-text steer for the thread (feeds its trips). */
	guidance?: string | null;
	/** Synthesized markdown answer with [källa:ID] citations; null until dug. */
	answer?: string | null;
	/** Depth at which `answer` was last written — answer is stale when depth > answer_depth. */
	answer_depth?: number;
	/** Discovery search hints, used until the thread's first trip. */
	hints?: string[];
	created_at: string;
	updated_at: string;
};

export type ResearchJob = {
	job_id: string;
	kind: string;
	status: string;
	progress?: { done: number; total: number; current: string };
	started_at?: string;
};

export type ResearchBoardSummary = {
	id: string;
	title: string;
	topic: string;
	status: "new" | "scouting" | "awaiting" | "digging" | "reporting" | "ready" | "failed";
	revision: number;
	thread_count: number;
	updated_at: string;
	created_at: string;
	/** Encrypted board: title/topic are "v1:..." ciphertext; decrypt with the unwrapped board key. */
	enc?: boolean;
	wrapped_board_key?: string | null;
};

export type ResearchBoardDetail = {
	id: string;
	title: string;
	topic: string;
	intro?: string | null;
	status: "new" | "scouting" | "awaiting" | "digging" | "reporting" | "ready" | "failed";
	revision: number;
	target_depth: number;
	threads: ResearchThread[];
	job?: ResearchJob | null;
	/** Single regeneratable markdown report woven from the thread answers. */
	report?: string | null;
	report_generated_at?: string | null;
	created_at: string;
	updated_at: string;
	enc?: boolean;
	wrapped_board_key?: string | null;
};

export type ResearchEvent = {
	done: number;
	total: number;
	current?: string;
	message?: string;
	level?: string;
	finding?: { label: string; detail?: string };
	/** Encrypted boards: the content fields above arrive as one ciphertext JSON blob. */
	enc?: string;
};

export type ResearchEventsResponse = {
	events: ResearchEvent[];
	is_done: boolean;
	offset: number;
	status: string;
	progress: { done: number; total: number; current: string };
	errors: string[];
};
