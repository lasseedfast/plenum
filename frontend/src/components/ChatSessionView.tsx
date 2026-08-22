import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useModeNavigation } from "./ModeToggle";
import { useQuery } from "@tanstack/react-query";
import { fetchMeta, getSession, upsertSession, setSessionId, createSnapshot } from "../api";
import { decryptJson, encryptJson } from "../crypto";
import { useAuth } from "../context/AuthContext";
import { SearchPanel } from "./SearchPanel";
import { ChatPanel, type ChatPanelHandle, INITIAL_ASSISTANT_MESSAGE } from "./ChatPanel";
import { copyToClipboardWhenReady } from "../utils/clipboard";
import { hydrateChatTurns } from "../utils/turns";
import type { ChatMessage, ChatTurn, EncSessionPayload, EncTitlePayload } from "../types";

export function ChatSessionView() {
	const { uuid } = useParams<{ uuid: string }>();
	const navigate = useNavigate();
	const goToMode = useModeNavigation();
	const { user, dek } = useAuth();

	// Chat state
	const [chatMessages, setChatMessages] = useState<ChatMessage[]>([
		{ role: "assistant", content: INITIAL_ASSISTANT_MESSAGE },
	]);
	const [chatTurns, setChatTurns] = useState<ChatTurn[]>([]);
	const [focusIds, setFocusIds] = useState<string[]>([]);
	const [chatInput, setChatInput] = useState("");
	const [isChatSending, setIsChatSending] = useState(false);
	const [sessionLoaded, setSessionLoaded] = useState(false);
	const [chatMentionedMp, setChatMentionedMp] = useState<{ name: string; person_id: string } | null>(null);
	const [shareToast, setShareToast] = useState<"copying" | "copied" | "error" | null>(null);

	const chatPanelRef = useRef<ChatPanelHandle | null>(null);
	const chatInputRef = useRef<{ getFinalText: () => string } | null>(null);
	const lastSavedTurnsRef = useRef<string>("");

	const meta = useQuery({ queryKey: ["meta"], queryFn: fetchMeta });

	useEffect(() => {
		if (typeof window === "undefined") return;
		const storageKey = "riksdagen-session-id";
		const storedId = window.localStorage.getItem(storageKey);
		const session = storedId ?? (crypto.randomUUID ? crypto.randomUUID() : `session-${Date.now()}`);
		if (!storedId) window.localStorage.setItem(storageKey, session);
		setSessionId(session);
	}, []);

	useEffect(() => {
		if (!uuid) { setSessionLoaded(true); return; }
		getSession(uuid)
			.then(async (data) => {
				if (!data) return;
				if (data.enc_payload && dek) {
					// Owned session: content arrives encrypted, decrypt locally.
					const payload = await decryptJson<EncSessionPayload>(dek, data.enc_payload);
					if ((payload.llm_messages ?? []).length > 0) {
						setChatMessages(payload.llm_messages as ChatMessage[]);
						setChatTurns(hydrateChatTurns(payload.turns as Partial<ChatTurn>[]));
						setFocusIds(payload.focus_ids ?? []);
					}
				} else if (data.llm_messages.length > 0) {
					setChatMessages(data.llm_messages as ChatMessage[]);
					setChatTurns(hydrateChatTurns(data.turns as Partial<ChatTurn>[]));
					setFocusIds(data.focus_ids);
				}
			})
			.catch(() => {})
			.finally(() => setSessionLoaded(true));
	}, [uuid]); // eslint-disable-line react-hooks/exhaustive-deps

	useEffect(() => {
		if (!uuid || !sessionLoaded || isChatSending) return;
		const hasReady = chatTurns.some(t => t.status === "ready");
		if (!hasReady) return;
		const snapshot = JSON.stringify(chatTurns);
		if (snapshot === lastSavedTurnsRef.current) return;
		lastSavedTurnsRef.current = snapshot;
		if (user && dek) {
			// Logged in: everything content-bearing is encrypted before upload.
			const payload: EncSessionPayload = {
				llm_messages: chatMessages,
				turns: chatTurns,
				focus_ids: focusIds,
			};
			const titlePayload: EncTitlePayload = {
				title: (chatTurns[0]?.question ?? "Konversation").slice(0, 80),
			};
			Promise.all([encryptJson(dek, payload), encryptJson(dek, titlePayload)])
				.then(([enc_payload, enc_title]) =>
					upsertSession(uuid, { session_type: "general", enc_payload, enc_title }),
				)
				.catch(() => {});
		} else {
			upsertSession(uuid, {
				session_type: "general",
				llm_messages: chatMessages,
				turns: chatTurns,
				focus_ids: focusIds,
			}).catch(() => {});
		}
	}, [chatTurns, isChatSending, sessionLoaded, uuid, chatMessages, focusIds, user, dek]);

	const handleShare = async () => {
		const readyTurns = chatTurns.filter(t => t.status === "ready");
		if (!readyTurns.length) return;
		setShareToast("copying");
		try {
			// Not awaited here on purpose — the clipboard write has to start
			// inside the click's user gesture or Safari refuses it. See
			// copyToClipboardWhenReady.
			const pendingUrl = createSnapshot({
				session_type: "general",
				llm_messages: chatMessages,
				turns: readyTurns.map(t => ({
					question: t.question,
					answerHtml: t.answerHtml ?? "",
					sources: t.sources ?? [],
				})),
				focus_ids: focusIds,
			}).then(snapshotId => `${window.location.origin}/fork/${snapshotId}`);
			await copyToClipboardWhenReady(pendingUrl);
			setShareToast("copied");
		} catch {
			setShareToast("error");
		} finally {
			setTimeout(() => setShareToast(null), 2500);
		}
	};

	const handleChatSubmit = () => {
		if (isChatSending) return;
		const trimmed = chatInput.trim();
		if (!trimmed) return;
		const finalText = chatInputRef.current?.getFinalText() ?? trimmed;
		const submitted = chatPanelRef.current?.submitPrompt(finalText, trimmed);
		if (submitted) setChatInput("");
	};

	const handleResetChat = () => {
		setChatMessages([{ role: "assistant", content: INITIAL_ASSISTANT_MESSAGE }]);
		setChatTurns([]);
		setFocusIds([]);
		setChatInput("");
		setIsChatSending(false);
		setChatMentionedMp(null);
		lastSavedTurnsRef.current = "";
		const newUuid = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
		navigate(`/chat/${newUuid}`, { replace: true });
	};

	if (!sessionLoaded) {
		return (
			<>
				<header className="page-header"><h1>Vad säger de i Riksdagen?</h1></header>
				<main className="content">
					<div className="chat-loading"><span className="chat-loading__spinner" /></div>
				</main>
			</>
		);
	}

	const hasShareableTurns = chatTurns.some(t => t.status === "ready");

	return (
		<>
			<header className="page-header">
				<h1>Vad säger de i Riksdagen?</h1>
			</header>

			<main className="content">
				<SearchPanel
					meta={meta.data}
					query=""
					filters={{ parties: [], people: [], debates: [] }}
					onQueryChange={() => {}}
					onFiltersChange={() => {}}
					onSubmit={() => {}}
					speakerSuggestions={[]}
					onSelectSpeaker={() => {}}
					isSearching={false}
					mode="chat"
					onModeChange={goToMode}
					chatInput={chatInput}
					onChatInputChange={setChatInput}
					onChatSubmit={handleChatSubmit}
					isChatSending={isChatSending}
					canResetChat={chatTurns.length > 0}
					onResetChat={handleResetChat}
					chatInputRef={chatInputRef}
					onChatMentionSelect={(suggestion) => {
						if (suggestion._key) setChatMentionedMp({ name: suggestion.name, person_id: suggestion._key });
					}}
				/>

				{chatMentionedMp && (
					<div className="mp-chat-shortcut">
						<Link to={`/mp/${chatMentionedMp.person_id}`} className="secondary-button">
							Chatta med {chatMentionedMp.name}
						</Link>
					</div>
				)}

				<ChatPanel
					ref={chatPanelRef}
					messages={chatMessages}
					focusIds={focusIds}
					turns={chatTurns}
					onTurnsChange={setChatTurns}
					onMessagesChange={(nextMessages, _tables, nextFocusIds) => {
						setChatMessages(nextMessages);
						if (nextFocusIds) setFocusIds(nextFocusIds);
					}}
					onPendingChange={setIsChatSending}
				/>

				{hasShareableTurns && (
					<div className="chat-share-row">
						<button
							type="button"
							className="secondary-button chat-share-btn"
							onClick={handleShare}
							disabled={shareToast === "copying"}
						>
							{shareToast === "copying" ? "Skapar länk…" : "Dela konversation"}
						</button>
					</div>
				)}
			</main>

			{shareToast === "copied" && (
				<div className="share-toast share-toast--success">Länk kopierad!</div>
			)}
			{shareToast === "error" && (
				<div className="share-toast share-toast--error">Kunde inte kopiera länken.</div>
			)}
		</>
	);
}
