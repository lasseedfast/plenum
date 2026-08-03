import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { ChatMessage, ChatSource, EncSessionPayload, EncTitlePayload, MpChatTurn, PersonDetail, Uppdrag } from "../types";
import { getSession, getSessionHeaders, upsertSession, createSnapshot } from "../api";
import { decryptJson, encryptJson } from "../crypto";
import { useAuth } from "../context/AuthContext";
import { useLLMSettings } from "../context/LLMSettingsContext";
import { convertMarkdownToHtml, getMpPhotoUrl } from "../utils/markdown";
import { chatAnswerToMarkdown } from "../utils/copyMarkdown";
import { copyToClipboard } from "../utils/clipboard";
import { CopyMarkdownButton } from "./CopyMarkdownButton";
import { useTalkDrawer } from "../context/TalkDrawerContext";

const TIMEOUT_MS = 360_000;

type Props = {
    person: PersonDetail;
    initialTalkId?: string;
    sessionId?: string;
};

// ── Flip-card MP bubble ───────────────────────────────────────────────────────

type MpBubbleProps = {
    photoUrl: string;
    answerHtml: string;
    /** Raw markdown of the same answer; absent on chats saved before markdown copy existed. */
    answerMarkdown?: string;
    sources: ChatSource[];
};

function MpBubble({ photoUrl, answerHtml, answerMarkdown, sources }: MpBubbleProps) {
    const [flipped, setFlipped] = useState(false);
    const [closing, setClosing] = useState(false);
    const navigate = useNavigate();
    const { openTalk } = useTalkDrawer();

    const hasSources = sources.length > 0;

    const toggleFlip = () => {
        setClosing(true);
        setTimeout(() => {
            setFlipped(f => !f);
            setClosing(false);
        }, 180);
    };

    const handleAnswerClick = (e: React.MouseEvent<HTMLDivElement>) => {
        const target = (e.target as HTMLElement).closest("a");
        const href = target?.getAttribute("href");
        if (href?.startsWith("/talk/")) {
            e.preventDefault();
            openTalk(href.slice("/talk/".length));
        } else if (href?.startsWith("/motion/")) {
            e.preventDefault();
            openTalk(`documents/${href.slice("/motion/".length)}`);
        } else if (href?.startsWith("/mp/")) {
            e.preventDefault();
            navigate(href);
        }
    };

    return (
        <div className={`mp-chat__bubble mp-chat__bubble--mp mp-bubble-card${closing ? " mp-bubble-card--closing" : ""}`}>
            <img
                className="mp-chat__bubble-avatar"
                src={photoUrl}
                alt=""
                onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }}
            />

            {!flipped ? (
                <div className="mp-bubble-card__front">
                    <div
                        className="mp-chat__answer"
                        dangerouslySetInnerHTML={{ __html: answerHtml }}
                        onClick={handleAnswerClick}
                    />
                    <div className="mp-bubble-card__front-actions">
                        {hasSources && (
                            <button
                                className="mp-bubble-card__flip-btn"
                                onClick={toggleFlip}
                                title="Visa källor"
                                aria-label={`Visa ${sources.length} källo${sources.length === 1 ? "r" : "r"}`}
                            >
                                📎 {sources.length} käll{sources.length === 1 ? "a" : "or"}
                            </button>
                        )}
                        {answerMarkdown && (
                            <CopyMarkdownButton
                                getMarkdown={() => chatAnswerToMarkdown(answerMarkdown, sources)}
                            />
                        )}
                    </div>
                </div>
            ) : (
                <div className="mp-bubble-card__back">
                    <div className="mp-bubble-card__back-header">
                        <button
                            className="mp-bubble-card__flip-btn mp-bubble-card__flip-btn--back"
                            onClick={toggleFlip}
                            aria-label="Tillbaka till svaret"
                        >
                            ← Tillbaka
                        </button>
                        <span className="mp-bubble-card__sources-label">Källor</span>
                    </div>
                    <div className="mp-bubble-card__sources">
                        {sources.map((src, i) => (
                            <div key={`${src._id}-${i}`} className="mp-bubble-card__source">
                                <div className="mp-bubble-card__source-meta">
                                    {src.person_id && (
                                        <img
                                            className="mp-bubble-card__source-avatar"
                                            src={getMpPhotoUrl(src.person_id)}
                                            alt=""
                                            onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }}
                                        />
                                    )}
                                    <div>
                                        {src.speaker && <span className="mp-bubble-card__source-speaker">{src.speaker}</span>}
                                        {src.date && <span className="mp-bubble-card__source-date">{src.date}</span>}
                                    </div>
                                </div>
                                {src.snippet && (
                                    <p className="mp-bubble-card__source-snippet">
                                        {src.snippet.replace(/\*\*(.*?)\*\*/g, "$1")}
                                    </p>
                                )}
                                {src.url_video && (
                                    <a
                                        href={src.url_video}
                                        className="mp-bubble-card__source-link"
                                        target="_blank"
                                        rel="noreferrer"
                                    >
                                        Öppna anförande ↗
                                    </a>
                                )}
                            </div>
                        ))}
                    </div>
                </div>
            )}
        </div>
    );
}

// ── Organ name translations ───────────────────────────────────────────────────

const ORGAN_NAMES: Record<string, string> = {
    kam: "Kammaren",
    eu: "EU-nämnden",
    upu: "Utrikespolitiska committee_recommendation",
    au: "Arbetsmarknadsutskottet",
    civ: "Civilutskottet",
    fiu: "Finansutskottet",
    fru: "Försvarsutskottet",
    ju: "Justitieutskottet",
    kru: "Kulturutskottet",
    ku: "Konstitutionsutskottet",
    mju: "Miljö- och jordbruksutskottet",
    nu: "Näringsutskottet",
    sfu: "Socialförsäkringsutskottet",
    sou: "Socialutskottet",
    sku: "Skatteutskottet",
    tru: "Trafikutskottet",
    uu: "Utrikesutskottet",
    uu2: "Utrikesutskottet",
    agu: "Arbetsgivarutskottet",
    bou: "Bostadsutskottet",
};

function formatDate(d: string | null | undefined): string {
    if (!d) return "–";
    return d.slice(0, 10).replace(/-/g, "\u2011");
}

// ── Uppdrag section ───────────────────────────────────────────────────────────

function UppdragSection({ uppdrag }: { uppdrag: Uppdrag[] }) {
    const [open, setOpen] = useState(false);

    const sorted = [...uppdrag].sort((a, b) => {
        const af = a.from ?? "";
        const bf = b.from ?? "";
        return bf.localeCompare(af);
    });

    return (
        <div className="mp-profile__uppdrag panel">
            <button
                className="mp-profile__uppdrag-toggle"
                onClick={() => setOpen(o => !o)}
                aria-expanded={open}
            >
                <span className="mp-profile__uppdrag-title">Uppdrag i riksdagen</span>
                <span className="mp-profile__uppdrag-count">{uppdrag.length}</span>
                <span className="mp-profile__uppdrag-chevron">{open ? "▲" : "▼"}</span>
            </button>

            {open && (
                <div className="mp-profile__uppdrag-list">
                    {sorted.map((u, i) => {
                        const organName = u.organ_kod
                            ? (ORGAN_NAMES[u.organ_kod.toLowerCase()] ?? u.organ_kod.toUpperCase())
                            : null;
                        return (
                            <div key={i} className="mp-profile__uppdrag-row">
                                <div className="mp-profile__uppdrag-year">
                                    {formatDate(u.from)} – {formatDate(u.tom)}
                                </div>
                                <div className="mp-profile__uppdrag-body">
                                    <span className="mp-profile__uppdrag-role">{u.roll_kod ?? u.status}</span>
                                    {organName && <span className="mp-profile__uppdrag-committee">{organName}</span>}
                                    {u.uppgift && u.uppgift !== u.roll_kod && (
                                        <span className="mp-profile__uppdrag-uppgift">{u.uppgift}</span>
                                    )}
                                </div>
                            </div>
                        );
                    })}
                </div>
            )}
        </div>
    );
}

// ── Main panel ────────────────────────────────────────────────────────────────

export function MpChatPanel({ person, initialTalkId, sessionId }: Props) {
    const { user, dek } = useAuth();
    const { providerOverride } = useLLMSettings();
    const [turns, setTurns] = useState<MpChatTurn[]>([]);
    const [messages, setMessages] = useState<ChatMessage[]>([]);
    const [input, setInput] = useState("");
    const [isPending, setIsPending] = useState(false);
    const [shareToast, setShareToast] = useState<"copying" | "copied" | "error" | null>(null);
    const messagesEndRef = useRef<HTMLDivElement>(null);
    const textareaRef = useRef<HTMLTextAreaElement>(null);
    const lastSavedTurnsRef = useRef<string>("");

    const first_name = person.first_name || person.name.split(" ")[0];
    const photoUrl = person.image_url_medium?.replace("http://", "https://")
        || getMpPhotoUrl(person.person_id);
    const isActive = person.status === "Tjänstgörande riksdagsledamot";

    useEffect(() => {
        messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }, [turns]);

    // Logged in: restore a saved (encrypted) MP chat, e.g. opened from "Mina
    // chattar". Anonymous MP chats are save-only, as before.
    useEffect(() => {
        if (!sessionId || !dek) return;
        getSession(sessionId)
            .then(async (data) => {
                if (!data?.enc_payload || data.session_type !== "mp") return;
                const payload = await decryptJson<EncSessionPayload>(dek, data.enc_payload);
                if ((payload.turns ?? []).length > 0) {
                    lastSavedTurnsRef.current = JSON.stringify(payload.turns);
                    setTurns(payload.turns as MpChatTurn[]);
                    setMessages(payload.llm_messages ?? []);
                }
            })
            .catch(() => {});
    }, [sessionId]); // eslint-disable-line react-hooks/exhaustive-deps

    useEffect(() => {
        if (!sessionId || isPending) return;
        const hasReady = turns.some(t => t.status === "ready");
        if (!hasReady) return;
        const snapshot = JSON.stringify(turns);
        if (snapshot === lastSavedTurnsRef.current) return;
        lastSavedTurnsRef.current = snapshot;
        if (user && dek) {
            // Encrypt everything, including WHICH MP was talked to.
            const payload: EncSessionPayload = {
                llm_messages: messages,
                turns,
                focus_ids: [],
                person_id: person.person_id,
                initial_speech_id: initialTalkId ?? null,
            };
            const titlePayload: EncTitlePayload = {
                title: `${person.name}: ${(turns[0]?.question ?? "").slice(0, 60)}`,
                person_id: person.person_id,
            };
            Promise.all([encryptJson(dek, payload), encryptJson(dek, titlePayload)])
                .then(([enc_payload, enc_title]) =>
                    upsertSession(sessionId, { session_type: "mp", enc_payload, enc_title }),
                )
                .catch(() => {});
        } else {
            upsertSession(sessionId, {
                session_type: "mp",
                person_id: person.person_id,
                initial_speech_id: initialTalkId,
                llm_messages: messages,
                turns,
                focus_ids: [],
            }).catch(() => {});
        }
    }, [turns, isPending, sessionId, person.person_id, person.name, initialTalkId, messages, user, dek]);

    const handleShare = async () => {
        const readyTurns = turns.filter(t => t.status === "ready");
        if (!readyTurns.length) return;
        setShareToast("copying");
        try {
            const snapshotId = await createSnapshot({
                session_type: "mp",
                person_id: person.person_id,
                initial_speech_id: initialTalkId,
                llm_messages: messages,
                turns: readyTurns.map(t => ({
                    question: t.question,
                    answerHtml: t.answerHtml ?? "",
                    sources: t.sources ?? [],
                })),
            });
            await copyToClipboard(`${window.location.origin}/fork/${snapshotId}`);
            setShareToast("copied");
        } catch {
            setShareToast("error");
        } finally {
            setTimeout(() => setShareToast(null), 2500);
        }
    };

    const submitPrompt = useCallback(async (prompt: string) => {
        const trimmed = prompt.trim();
        if (!trimmed || isPending) return;

        const turnId = crypto.randomUUID ? crypto.randomUUID() : `turn-${Date.now()}`;
        const nextMessages: ChatMessage[] = [...messages, { role: "user", content: trimmed }];

        setMessages(nextMessages);
        setTurns(prev => [...prev, { id: turnId, question: trimmed, status: "pending" }]);
        setIsPending(true);
        setInput("");
        if (textareaRef.current) textareaRef.current.style.height = "auto";

        const controller = new AbortController();
        let timeoutId = setTimeout(() => controller.abort(), TIMEOUT_MS);
        const resetTimeout = () => {
            clearTimeout(timeoutId);
            timeoutId = setTimeout(() => controller.abort(), TIMEOUT_MS);
        };

        try {
            const response = await fetch("/api/chat/mp/stream", {
                method: "POST",
                headers: { "Content-Type": "application/json", ...getSessionHeaders() },
                body: JSON.stringify({
                    messages: nextMessages,
                    person_id: person.person_id,
                    initial_speech_id: initialTalkId ?? null,
                    provider_override: providerOverride,
                }),
                signal: controller.signal,
            });

            if (!response.ok) {
                const maybeJson = await response.json().catch(() => null);
                throw new Error(maybeJson?.detail ?? `Serverfel (${response.status})`);
            }

            const reader = response.body!.getReader();
            const decoder = new TextDecoder();
            let buffer = "";

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                resetTimeout();
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split("\n");
                buffer = lines.pop() ?? "";

                for (const line of lines) {
                    if (!line.startsWith("data: ")) continue;
                    const event = JSON.parse(line.slice(6));

                    if (event.type === "answer") {
                        const sources: ChatSource[] = event.sources ?? [];
                        const answerHtml = convertMarkdownToHtml(event.answer ?? "", sources);
                        setTurns(prev =>
                            prev.map(t =>
                                t.id === turnId
                                    ? { ...t, status: "ready", answer: event.answer ?? "", answerHtml, sources }
                                    : t
                            )
                        );
                        setMessages(prev => [...prev, { role: "assistant", content: event.answer ?? "" }]);
                        return;
                    } else if (event.type === "error") {
                        throw new Error(event.message ?? "Okänt serverfel");
                    }
                }
            }
            throw new Error("Anslutningen avslutades utan svar.");
        } catch (err: any) {
            const isTimeout = err?.name === "AbortError";
            const msg = isTimeout
                ? "Chatten tog för lång tid och avbröts."
                : err?.message || "Kunde inte hämta svar.";
            setTurns(prev =>
                prev.map(t =>
                    t.id === turnId ? { ...t, status: "error", errorMessage: msg } : t
                )
            );
        } finally {
            clearTimeout(timeoutId);
            setIsPending(false);
        }
    }, [isPending, messages, person.person_id, initialTalkId, providerOverride]);

    const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            submitPrompt(input);
        }
    };

    const resizeTextarea = (el: HTMLTextAreaElement) => {
        el.style.height = "auto";
        el.style.height = `${el.scrollHeight}px`;
    };

    return (
        <div className="mp-profile">
            {/* Profile hero */}
            <div className="mp-profile__hero panel">
                <img
                    className="mp-profile__photo"
                    src={photoUrl}
                    alt={person.name}
                    onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }}
                />
                <div className="mp-profile__info">
                    <div className="mp-profile__name-row">
                        <h1 className="mp-profile__name">{person.name}</h1>
                        {person.party && (
                            <span className="party-chip" data-party={person.party} style={{ "--party-color": `var(--party-${person.party ?? ""})` } as React.CSSProperties}>
                                {person.party}
                            </span>
                        )}
                        {isActive && (
                            <span className="mp-profile__active-badge">Tjänstgörande</span>
                        )}
                    </div>
                    <dl className="mp-profile__meta">
                        {person.constituency && (
                            <>
                                <dt>Valkrets</dt>
                                <dd>{person.constituency}</dd>
                            </>
                        )}
                        {person.birth_year && (
                            <>
                                <dt>Född</dt>
                                <dd>{person.birth_year}</dd>
                            </>
                        )}
                        {person.status && !isActive && (
                            <>
                                <dt>Status</dt>
                                <dd>{person.status}</dd>
                            </>
                        )}
                    </dl>
                </div>
            </div>

            {/* Uppdrag */}
            {person.uppdrag && person.uppdrag.length > 0 && (
                <UppdragSection uppdrag={person.uppdrag} />
            )}

            {/* Chat section */}
            <div className="mp-profile__chat panel">
                <div className="mp-profile__chat-header">
                    <h2 className="mp-profile__chat-title">Chatta med {first_name}</h2>
                    <div className="mp-profile__chat-disclaimer">
                        <span>Digital assistent – inte den riktiga {first_name}. Svaren bygger på anföranden i riksdagen.</span>
                        <span className="mp-chat__experimental">Experimentell</span>
                    </div>
                </div>

                <div className="mp-chat__messages" aria-live="polite">
                    {turns.length === 0 && (
                        <div className="mp-chat__empty">
                            Ställ en fråga till {first_name}!
                        </div>
                    )}

                    {turns.map(turn => (
                        <div key={turn.id} className="mp-chat__turn">
                            <div className="mp-chat__bubble mp-chat__bubble--user">
                                {turn.question}
                            </div>

                            {turn.status === "pending" && (
                                <div className="mp-chat__bubble mp-chat__bubble--mp">
                                    <img
                                        className="mp-chat__bubble-avatar"
                                        src={photoUrl}
                                        alt=""
                                        onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }}
                                    />
                                    <div className="mp-chat__typing">
                                        <span className="chat-loading__spinner" />
                                    </div>
                                </div>
                            )}

                            {turn.status === "error" && (
                                <div className="mp-chat__bubble mp-chat__bubble--mp">
                                    <img
                                        className="mp-chat__bubble-avatar"
                                        src={photoUrl}
                                        alt=""
                                        onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }}
                                    />
                                    <p className="mp-chat__error">{turn.errorMessage}</p>
                                </div>
                            )}

                            {turn.status === "ready" && turn.answerHtml && (
                                <MpBubble
                                    photoUrl={photoUrl}
                                    answerHtml={turn.answerHtml}
                                    answerMarkdown={turn.answer}
                                    sources={turn.sources ?? []}
                                />
                            )}
                        </div>
                    ))}

                    <div ref={messagesEndRef} />
                </div>

                <div className="mp-chat__input-row">
                    <textarea
                        ref={textareaRef}
                        className="mp-chat__input"
                        placeholder={`Skriv en fråga till ${first_name}…`}
                        value={input}
                        disabled={isPending}
                        rows={1}
                        onChange={e => {
                            setInput(e.target.value);
                            resizeTextarea(e.target);
                        }}
                        onKeyDown={handleKeyDown}
                    />
                    <button
                        className="mp-chat__send"
                        disabled={isPending || !input.trim()}
                        onClick={() => submitPrompt(input)}
                        aria-label="Skicka"
                    >
                        {isPending ? <span className="chat-loading__spinner" /> : "→"}
                    </button>
                </div>
            </div>

            {turns.some(t => t.status === "ready") && (
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

            {shareToast === "copied" && (
                <div className="share-toast share-toast--success">Länk kopierad!</div>
            )}
            {shareToast === "error" && (
                <div className="share-toast share-toast--error">Kunde inte kopiera länken.</div>
            )}
        </div>
    );
}
