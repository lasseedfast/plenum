import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState, type KeyboardEvent, type MouseEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";
import type { ChatMessage, ChatRequest, ChatResponse, ChatResponseTable, ChatSource, ChatTurn, LiveCard, LiveInsightCard, LiveSearchCard, LiveStatsCard, ResearchCard } from "../types";
import { getSessionHeaders } from "../api";
import { useLLMSettings } from "../context/LLMSettingsContext";
import { convertMarkdownToHtml, getMpPhotoUrl as getMpPhotoUrlUtil } from "../utils/markdown";
import { chatAnswerToMarkdown } from "../utils/copyMarkdown";
import { CopyMarkdownButton } from "./CopyMarkdownButton";
import { ResultsTable } from "./ResultsTable";
import { useTalkDrawer } from "../context/TalkDrawerContext";

export const INITIAL_ASSISTANT_MESSAGE = "Hej! Ställ en fråga om protokollen så försöker jag hjälpa till.";
const CHAT_REQUEST_TIMEOUT_MS = 360_000; // Allow up to six minutes for long-running tool calls.

const TOOL_HINTS: Record<string, string> = {
    arango_search: "Söker i anföranden med fulltextsökning…",
    vector_search: "Gör semantisk sökning i databasen…",
    vector_search_debates: "Söker efter relevanta debatter…",
    fetch_debate: "Hämtar debattens tal…",
    aql_query: "Kör strukturerad databasfråga…",
    database_query: "Kör strukturerad databasfråga…",
    search_documents: "Analyserar och söker i dokumenten…",
    fetch_documents: "Hämtar fullständiga dokument…",
};

type Props = {
    messages: ChatMessage[];
    focusIds: string[];
    turns: ChatTurn[];
    onTurnsChange: (turns: ChatTurn[]) => void;
    onMessagesChange: (messages: ChatMessage[], tables?: ChatResponseTable[], focusIds?: string[]) => void;
    onPendingChange?: (pending: boolean) => void;
};

export type ChatPanelHandle = {
    submitPrompt: (prompt: string, displayQuestion?: string) => boolean;
};

const getMpPhotoUrl = getMpPhotoUrlUtil;

const newCardId = (): string =>
    typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : `card-${Date.now()}-${Math.random()}`;

// ── Sub-views used inside ResearchCardView ────────────────────────────────────

const SpeakerHighlights = ({ speaker_ids, speaker_ids_context }: { speaker_ids?: string[]; speaker_ids_context?: string }) => {
    if (!speaker_ids?.length) return null;
    return (
        <div className="rc-speakers">
            {speaker_ids_context && <p className="rc-speakers__context">{speaker_ids_context}</p>}
            <div className="rc-speakers__avatars">
                {speaker_ids.map(id => (
                    // Link to the MP profile page so users can click to learn more
                    <Link key={id} to={`/mp/${id}`} className="rc-speakers__link">
                        <img
                            className="rc-speakers__avatar rc-speakers__avatar--featured talk-view__speaker-photo--enhanced"
                            src={getMpPhotoUrl(id)}
                            alt=""
                            loading="lazy"
                            onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }}
                        />
                    </Link>
                ))}
            </div>
        </div>
    );
};

const SearchResultView = ({ card }: { card: LiveSearchCard }) => {
    const { openTalk } = useTalkDrawer();

    return (
        <div className="rc-search">
            <div className="rc-search__header">
                {card.query && (
                    <span className="rc-search__query">
                        Sökning: <em>{card.query}</em>
                    </span>
                )}
                <span className="rc-search__count">
                    {card.total} träff{card.total !== 1 ? "ar" : ""}
                    {card.limit_reached ? " (fler finns)" : ""}
                </span>
            </div>
            {card.results.slice(0, 3).map((result, i) => {
                const talkId = result._id ?? result.id;
                const handleOpen = () => { if (talkId) openTalk(talkId); };
                return (
                    <div
                        key={i}
                        className="rc-search__row"
                        role={talkId ? "button" : undefined}
                        tabIndex={talkId ? 0 : undefined}
                        onClick={talkId ? handleOpen : undefined}
                        onKeyDown={talkId ? (e: KeyboardEvent<HTMLDivElement>) => {
                            if (e.key === "Enter" || e.key === " ") { e.preventDefault(); handleOpen(); }
                        } : undefined}
                    >
                        {result.intressent_id && (
                            <img
                                className="rc-search__avatar talk-view__speaker-photo--enhanced"
                                src={getMpPhotoUrl(result.intressent_id)}
                                alt={result.speaker ?? ""}
                                loading="lazy"
                                onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }}
                            />
                        )}
                        <div className="rc-search__body">
                            {result.speaker && <span className="rc-search__speaker">{result.speaker}</span>}
                            {result.party && <span className="party-chip rc-search__party-chip" data-party={result.party} style={{ "--party-color": `var(--party-${result.party ?? ""})` } as React.CSSProperties}>{result.party}</span>}
                            {result.date && <span className="rc-search__date">{result.date}</span>}
                            {result.snippet && <p className="rc-search__snippet">{result.snippet}</p>}
                        </div>
                    </div>
                );
            })}
            {/* SpeakerHighlights omitted: each row already shows the speaker with avatar, name, party */}
        </div>
    );
};

const StatsResultView = ({ card }: { card: LiveStatsCard }) => {
    if (!card.rows.length) return null;
    const keys = Object.keys(card.rows[0]);
    return (
        <div className="rc-stats">
            <table className="rc-stats__table">
                <thead><tr>{keys.map(k => <th key={k}>{k}</th>)}</tr></thead>
                <tbody>
                    {card.rows.slice(0, 10).map((row, i) => (
                        <tr key={i}>{keys.map(k => <td key={k}>{String(row[k] ?? "")}</td>)}</tr>
                    ))}
                </tbody>
            </table>
            <SpeakerHighlights speaker_ids={card.speaker_ids} speaker_ids_context={card.speaker_ids_context} />
        </div>
    );
};

const renderInsightMessage = (message: string, sources: Record<string, string> = {}) => {
    const parts = message.split(/(\[src:[A-Za-z0-9_-]+\])/g);
    let footnoteIndex = 0;
    const seen: Record<string, number> = {};
    return parts.map((part, i) => {
        const m = part.match(/^\[src:([A-Za-z0-9_-]+)\]$/);
        if (!m) return part;
        const id = m[1];
        if (!(id in seen)) { seen[id] = ++footnoteIndex; }
        const n = seen[id];
        const url = sources[id];
        return url
            ? <a key={i} href={url} target="_blank" rel="noopener noreferrer" className="rc-insight__footnote">[{n}]</a>
            : <span key={i} className="rc-insight__footnote">[{n}]</span>;
    });
};

const InsightResultView = ({ card }: { card: LiveInsightCard }) => (
    <div className="rc-insight">
        <p className="rc-insight__message">{renderInsightMessage(card.message, card.sources)}</p>
        <SpeakerHighlights speaker_ids={card.speaker_ids} speaker_ids_context={card.speaker_ids_context} />
    </div>
);

/**
 * Renders one research card. A card evolves through three states:
 *   thinking  → shows spinner + LLM narration message (isActive = latest card)
 *   result    → message + inner styled container with search/stats result
 *   answer    → direct answer text
 */
/** Renders the final answer HTML: /talk/ references open in the side drawer, /mp/ links navigate. */
const AnswerText = ({ html }: { html: string }) => {
    const navigate = useNavigate();
    const { openTalk } = useTalkDrawer();
    const handleClick = useCallback((e: MouseEvent<HTMLDivElement>) => {
        const target = (e.target as HTMLElement).closest("a");
        const href = target?.getAttribute("href");
        if (href?.startsWith("/talk/")) {
            e.preventDefault();
            openTalk(href.slice("/talk/".length));
        } else if (href?.startsWith("/motion/")) {
            e.preventDefault();
            openTalk(`motions/${href.slice("/motion/".length)}`);
        } else if (href?.startsWith("/mp/")) {
            e.preventDefault();
            navigate(href);
        }
    }, [navigate, openTalk]);
    return (
        <div
            className="chat-view__answerText"
            dangerouslySetInnerHTML={{ __html: html }}
            onClick={handleClick}
        />
    );
};

const ResearchCardView = ({
    card,
    isActive,
    copyMarkdown,
}: {
    card: ResearchCard;
    isActive: boolean;
    /** Provided for finished answers only — builds the clipboard markdown on demand. */
    copyMarkdown?: () => string;
}) => {
    // Final answer — just the text, no inner box
    if (card.isAnswer) {
        return (
            <>
                <AnswerText html={card.answerHtml ?? ""} />
                {copyMarkdown && (
                    <div className="chat-card__actions">
                        <CopyMarkdownButton getMarkdown={copyMarkdown} label="Kopiera" />
                    </div>
                )}
            </>
        );
    }

    // Result card (search or stats) — message in its own box + result below
    if (card.result?.type === "search_card") {
        return (
            <>
                {card.message && (
                    <div className="research-card research-card--thinking research-card--done">
                        <span className="research-card__check">✓</span>
                        <div
                            className="research-card__message"
                            dangerouslySetInnerHTML={{ __html: convertMarkdownToHtml(card.message) }}
                        />
                    </div>
                )}
                <div className="research-card research-card--result">
                    <SearchResultView card={card.result} />
                </div>
            </>
        );
    }

    if (card.result?.type === "stats_card") {
        return (
            <>
                {card.message && (
                    <div className="research-card research-card--thinking research-card--done">
                        <span className="research-card__check">✓</span>
                        <div
                            className="research-card__message"
                            dangerouslySetInnerHTML={{ __html: convertMarkdownToHtml(card.message) }}
                        />
                    </div>
                )}
                <div className="research-card research-card--result">
                    <StatsResultView card={card.result} />
                </div>
            </>
        );
    }

    if (card.result?.type === "insight_card") {
        return (
            <div className="research-card research-card--result">
                <InsightResultView card={card.result} />
            </div>
        );
    }

    // Thinking card — show spinner if active
    if (isActive) {
        return (
            <div className="research-card research-card--thinking research-card--active">
                <span className="chat-loading__spinner" />
                <div
                    className="research-card__message"
                    dangerouslySetInnerHTML={{ __html: convertMarkdownToHtml(card.message || "Analyserar frågan…") }}
                />
            </div>
        );
    }

    // Completed thinking card — compact, no result surfaced
    return (
        <div className="research-card research-card--thinking research-card--done">
            <span className="research-card__check">✓</span>
            <div
                className="research-card__message"
                dangerouslySetInnerHTML={{ __html: convertMarkdownToHtml(card.message) }}
            />
        </div>
    );
};

// ── Main component ────────────────────────────────────────────────────────────

export const ChatPanel = forwardRef<ChatPanelHandle, Props>(function ChatPanel(
    { messages, onMessagesChange, onPendingChange, focusIds, turns: initialTurns, onTurnsChange }: Props,
    ref,
) {
    const { providerOverride, useEditor } = useLLMSettings();
    const [turns, setTurns] = useState<ChatTurn[]>(initialTurns);
    const [selectedTurnId, setSelectedTurnId] = useState<string | null>(null);
    const [lastError, setLastError] = useState<string | null>(null);

    // Research cards accumulate as the LLM works. Newest card is at index 0.
    const [researchCards, setResearchCards] = useState<ResearchCard[]>([]);
    const researchCardsRef = useRef<ResearchCard[]>([]);
    const pendingDisplayQuestionRef = useRef<string>("");
    researchCardsRef.current = researchCards; // always current — safe to read in onSuccess

    const carouselRef = useRef<HTMLDivElement>(null);
    const cardRefs = useRef<Map<string, HTMLElement>>(new Map());
    const suppressObserverRef = useRef(false);
    const suppressTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    const scrollCarouselToTurn = useCallback((turnIndex: number) => {
        const carousel = carouselRef.current;
        if (!carousel || turnIndex < 0) return;

        const card = carousel.children[turnIndex] as HTMLElement | undefined;
        if (!card) return;

        const targetLeft = card.offsetLeft - (carousel.clientWidth - card.clientWidth) / 2;
        const maxScrollLeft = Math.max(0, carousel.scrollWidth - carousel.clientWidth);
        const clampedLeft = Math.min(Math.max(targetLeft, 0), maxScrollLeft);

        carousel.scrollTo({ left: clampedLeft, behavior: "smooth" });
    }, []);

    // Keep local turn cache aligned with the parent store.
    useEffect(() => {
        setTurns(initialTurns);
        if (!initialTurns.length) {
            setSelectedTurnId(null);
            setLastError(null);
            return;
        }
        if (selectedTurnId && !initialTurns.some((turn) => turn.id === selectedTurnId)) {
            setSelectedTurnId(initialTurns[initialTurns.length - 1]?.id ?? null);
        }
    }, [initialTurns, selectedTurnId]);

    const updateTurns = useCallback(
        (mutator: (prev: ChatTurn[]) => ChatTurn[]) => {
            setTurns((prev) => {
                const next = mutator(prev);
                onTurnsChange(next);
                return next;
            });
        },
        [onTurnsChange],
    );

    const activeTurn = useMemo(() => {
        if (turns.length === 0) return null;
        if (selectedTurnId) {
            const selected = turns.find((turn) => turn.id === selectedTurnId);
            if (selected) return selected;
        }
        return turns[turns.length - 1];
    }, [turns, selectedTurnId]);

    const activeTurnIndex = useMemo(() => {
        if (!activeTurn) return -1;
        return turns.findIndex((turn) => turn.id === activeTurn.id);
    }, [activeTurn, turns]);

    // Scroll the carousel to the active card whenever it changes.
    useEffect(() => {
        if (!carouselRef.current || activeTurnIndex < 0) return;
        suppressObserverRef.current = true;
        if (suppressTimerRef.current) clearTimeout(suppressTimerRef.current);
        suppressTimerRef.current = setTimeout(() => {
            suppressObserverRef.current = false;
        }, 600);
        scrollCarouselToTurn(activeTurnIndex);
    }, [activeTurnIndex, scrollCarouselToTurn]);

    // Update selected turn as the user swipes/scrolls through the carousel.
    useEffect(() => {
        const carousel = carouselRef.current;
        if (!carousel) return;
        const observer = new IntersectionObserver(
            (entries) => {
                if (suppressObserverRef.current) return;
                for (const entry of entries) {
                    if (entry.isIntersecting && entry.intersectionRatio >= 0.5) {
                        const id = (entry.target as HTMLElement).dataset.turnId;
                        if (id) setSelectedTurnId(id);
                    }
                }
            },
            { root: carousel, threshold: 0.5 },
        );
        cardRefs.current.forEach((el) => observer.observe(el));
        return () => observer.disconnect();
    }, [turns]);

    const createTurnId = () =>
        typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `chat-turn-${Date.now()}`;

    // ── Helpers for card state machine ──────────────────────────────────────

    /** True if the latest (index 0) card is still "thinking" (no result, no answer). */
    const latestIsThinking = (cards: ResearchCard[]) =>
        cards.length > 0 && cards[0].result === undefined && !cards[0].isAnswer;

    /** Create a new thinking card and prepend it. */
    const prependCard = (cards: ResearchCard[], message: string): ResearchCard[] =>
        [{ id: newCardId(), message, isAnswer: false }, ...cards];

    /**
     * Prepend a result card along with an empty thinking placeholder above it,
     * so the UI immediately signals "more is coming" instead of leaving the
     * result card looking like the final answer while the LLM decides its
     * next step. If the current top card is already an empty placeholder
     * (leftover from a prior result), drop it so we don't stack placeholders.
     */
    const prependResultCard = (
        cards: ResearchCard[],
        result: LiveCard,
        resultMessage: string,
    ): ResearchCard[] => {
        const base = cards.length > 0
            && cards[0].result === undefined
            && !cards[0].isAnswer
            && !cards[0].message
            ? cards.slice(1)
            : cards;
        return [
            { id: newCardId(), message: "", isAnswer: false },
            { id: newCardId(), message: resultMessage, result, isAnswer: false },
            ...base,
        ];
    };

    // ── SSE stream consumer ──────────────────────────────────────────────────

    const callChatApi = useCallback(async (payload: ChatRequest): Promise<ChatResponse> => {
        const controller = new AbortController();
        // Inactivity timeout: reset on every received chunk (including keepalives).
        // This lets long-running LLM sessions finish as long as the connection is alive.
        let timeoutId = setTimeout(() => controller.abort(), CHAT_REQUEST_TIMEOUT_MS);
        const resetTimeout = () => {
            clearTimeout(timeoutId);
            timeoutId = setTimeout(() => controller.abort(), CHAT_REQUEST_TIMEOUT_MS);
        };
        try {
            const response = await fetch("/api/chat/stream", {
                method: "POST",
                headers: { "Content-Type": "application/json", ...getSessionHeaders() },
                body: JSON.stringify(payload),
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

                    if (event.type === "status") {
                        // LLM narration — create a new thinking card or update the current one.
                        // Exception: if the latest card already has a result, always create a new card
                        // to avoid the impression that the result card is the final answer.
                        const msg: string = event.message ?? "";
                        if (!msg) continue;
                        setResearchCards(prev => {
                            if (prev.length > 0 && prev[0].result !== undefined && !prev[0].isAnswer) {
                                // Latest card has a result → create a new thinking card
                                return prependCard(prev, msg);
                            }
                            // No result yet → update message on current card or create one
                            return latestIsThinking(prev)
                                ? [{ ...prev[0], message: msg }, ...prev.slice(1)]
                                : prependCard(prev, msg);
                        });

                    } else if (event.type === "tool_call") {
                        // Fallback: create a card if none exists yet or if the latest is done.
                        const hint = TOOL_HINTS[event.tool] ?? `Kör ${event.tool}…`;
                        setResearchCards(prev => {
                            if (prev.length === 0) return prependCard(prev, hint);
                            if (!latestIsThinking(prev)) return prependCard(prev, hint);
                            // Already has a thinking card — only fill message if empty
                            if (!prev[0].message) return [{ ...prev[0], message: hint }, ...prev.slice(1)];
                            return prev; // status message already set, don't override
                        });

                    } else if (event.type === "tool_speakers") {
                        // Speaker IDs previewed during loading; photos are shown in search cards.
                        // Intentionally ignored here — search result cards show photos inline.

                    } else if (event.type === "search_card") {
                        // Shadow communicator insight — always its own new card.
                        // card.message = insight text shown as ✓ header above results.
                        const result: LiveSearchCard = {
                            type: "search_card",
                            message: event.message ?? "",
                            query: event.query ?? "",
                            results: event.results ?? [],
                            total: event.total ?? 0,
                            limit_reached: event.limit_reached ?? false,
                            stats: event.stats,
                            speaker_ids: event.speaker_ids ?? [],
                            speaker_ids_context: event.speaker_ids_context ?? "",
                        };
                        setResearchCards(prev => prependResultCard(prev, result, event.message ?? ""));

                    } else if (event.type === "stats_card") {
                        // Shadow communicator insight — always its own new card.
                        const result: LiveStatsCard = {
                            type: "stats_card",
                            message: event.message ?? "",
                            rows: event.rows ?? [],
                            speaker_ids: event.speaker_ids ?? [],
                            speaker_ids_context: event.speaker_ids_context ?? "",
                        };
                        setResearchCards(prev => prependResultCard(prev, result, event.message ?? ""));

                    } else if (event.type === "insight") {
                        // The LLM voluntarily shares an observation — always gets its own card.
                        const result: LiveInsightCard = {
                            type: "insight_card",
                            message: event.message ?? "",
                            sources: event.sources ?? {},
                            speaker_ids: event.speaker_ids ?? [],
                            speaker_ids_context: event.speaker_ids_context ?? "",
                        };
                        setResearchCards(prev => prependResultCard(prev, result, ""));

                    } else if (event.type === "answer") {
                        return event as ChatResponse;

                    } else if (event.type === "error") {
                        throw new Error(event.message ?? "Okänt serverfel");
                    }
                }
            }
            throw new Error("Streamen avslutades utan svar.");
        } catch (error) {
            if (error instanceof DOMException && error.name === "AbortError") {
                throw new Error("timeout");
            }
            throw error as Error;
        } finally {
            clearTimeout(timeoutId);
        }
    }, []);

    // ── Mutation ─────────────────────────────────────────────────────────────

    const chatMutation = useMutation<ChatResponse, Error, ChatRequest, { turnId: string }>({
        mutationFn: callChatApi,
        onMutate: async (payload) => {
            const latestUserMessage = payload.messages[payload.messages.length - 1];
            const turnId = createTurnId();
            const newTurn: ChatTurn = {
                id: turnId,
                question: pendingDisplayQuestionRef.current || (latestUserMessage?.content ?? ""),
                createdAt: new Date().toISOString(),
                status: "pending",
            };
            updateTurns((prev) => [...prev, newTurn]);
            setSelectedTurnId(turnId);
            setLastError(null);
            setResearchCards([]);
            return { turnId };
        },

        onSuccess: (data, variables, context) => {
            if (!context?.turnId) return;
            const answerHtml = convertMarkdownToHtml(data.answer, data.sources);

            // Merge the final answer into the card stack:
            // - If there are prior research cards, upgrade the latest thinking card
            //   (or create a new card if the latest already has a result).
            // - If there are no prior cards (direct answer, no tools), leave the
            //   card array empty and render the answer text directly.
            const prior = researchCardsRef.current;
            let finalCards: ResearchCard[];
            if (prior.length === 0) {
                finalCards = [];
            } else if (latestIsThinking(prior)) {
                // Upgrade latest thinking card to answer
                finalCards = [{ ...prior[0], isAnswer: true, answerHtml }, ...prior.slice(1)];
            } else {
                // Latest already has a result → prepend a new answer card
                finalCards = [{ id: newCardId(), message: "", isAnswer: true, answerHtml }, ...prior];
            }

            setResearchCards(finalCards);

            const nextFocusIds = data.focus_ids ?? focusIds;
            updateTurns((prev) =>
                prev.map((turn) =>
                    turn.id === context.turnId
                        ? {
                              ...turn,
                              status: "ready",
                              answer: data.answer,
                              answerHtml,
                              sources: data.sources,
                              tables: data.tables ?? [],
                              liveCards: finalCards,
                          }
                        : turn,
                ),
            );
            onMessagesChange(
                [...variables.messages, { role: "assistant", content: data.answer }],
                data.tables,
                nextFocusIds,
            );
        },

        onError: (error, _variables, context) => {
            const isTimeout = error.message === "timeout";
            const fallbackMessage = isTimeout
                ? "Chatten tog för lång tid och avbröts. Försök igen eller snävra in frågan."
                : error.message || "Kunde inte hämta svar. Försök igen.";
            if (context?.turnId) {
                updateTurns((prev) =>
                    prev.map((turn) =>
                        turn.id === context.turnId
                            ? {
                                  ...turn,
                                  status: "error",
                                  errorMessage: isTimeout
                                      ? "Förfrågan avbröts efter tidsgränsen."
                                      : fallbackMessage,
                              }
                            : turn,
                    ),
                );
            }
            setLastError(fallbackMessage);
        },
    });

    useEffect(() => {
        onPendingChange?.(chatMutation.isPending);
    }, [chatMutation.isPending, onPendingChange]);

    const submitPrompt = (passedPrompt: string, displayQuestion?: string): boolean => {
        const trimmed = passedPrompt.trim();
        if (!trimmed || chatMutation.isPending) return false;
        const displayText = (displayQuestion ?? passedPrompt).split("\nINTRESSENT_IDS")[0].trim();
        pendingDisplayQuestionRef.current = displayText || trimmed;

        let realMessages = messages;
        if (
            messages.length === 1 &&
            messages[0].role === "assistant" &&
            messages[0].content === INITIAL_ASSISTANT_MESSAGE
        ) {
            realMessages = [];
        }

        const nextMessages: ChatMessage[] = [...realMessages, { role: "user", content: trimmed }];
        onMessagesChange(nextMessages);
        setLastError(null);
        chatMutation.mutate({ messages: nextMessages, top_k: 5, focus_ids: focusIds, session_id: getSessionHeaders()["X-Session-Id"], provider_override: providerOverride, use_editor: useEditor });
        return true;
    };

    useImperativeHandle(ref, () => ({ submitPrompt }));

    // ── Render helpers ───────────────────────────────────────────────────────

    /** Cards to show in the completed state: only result + answer cards (hide pure thinking cards). */
    const substantiveCards = (cards: ResearchCard[]) =>
        cards.filter(c => c.result !== undefined || c.isAnswer);

    /**
     * Cards to show while streaming: keep the top card (active thinking or latest result)
     * plus all result/answer cards below it. Old completed thinking-only cards are hidden
     * because they add noise without adding information — their message was already shown
     * transiently while they were active.
     */
    const pendingCards = (cards: ResearchCard[]) => {
        if (cards.length === 0) return cards;
        const [top, ...rest] = cards;
        return [top, ...rest.filter(c => c.result !== undefined || c.isAnswer)];
    };

    // ── JSX ──────────────────────────────────────────────────────────────────

    return (
        <section className="chat-view">
            {lastError && <div className="error-banner">{lastError}</div>}

            {activeTurn && (
                <div className="chat-question-header">
                    <p className="chat-question-header__text">{activeTurn.question}</p>
                </div>
            )}

            {turns.length > 1 && activeTurn && (
                <div className="chat-dots" role="tablist" aria-label="Svar">
                    {turns.map((turn, i) => (
                        <button
                            key={turn.id}
                            role="tab"
                            aria-selected={turn.id === activeTurn?.id}
                            className="chat-dot"
                            data-active={turn.id === activeTurn?.id}
                            onClick={() => setSelectedTurnId(turn.id)}
                            aria-label={`Svar ${i + 1}: ${turn.question}`}
                        />
                    ))}
                </div>
            )}

            {turns.length > 0 && (
                <div className="chat-carousel-shell">
                <div className="chat-carousel" ref={carouselRef} aria-live="polite">
                    {turns.map((turn) => {
                        // Determine which cards to show
                        const cardsToShow = turn.status === "pending" ? researchCards : (turn.liveCards ?? []);
                        const cardsList = turn.status === "pending"
                            ? pendingCards(cardsToShow)   // hide old done thinking-only cards while streaming
                            : substantiveCards(cardsToShow);

                        // For pending state with no cards yet, show a default thinking card
                        const displayCards = cardsList.length === 0 && turn.status === "pending"
                            ? [{ id: "default", message: "Analyserar frågan…", isAnswer: false } as ResearchCard]
                            : cardsList;

                        return (
                            <div
                                key={turn.id}
                                className="chat-turn-stack"
                                data-active={turn.id === activeTurn?.id}
                                data-turn-id={turn.id}
                                ref={(el) => {
                                    if (el) cardRefs.current.set(turn.id, el);
                                    else cardRefs.current.delete(turn.id);
                                }}
                                onClick={() => {
                                    if (turn.id !== activeTurn?.id) setSelectedTurnId(turn.id);
                                }}
                            >
                                {/* ── PENDING STATE ─────────────────────────────── */}
                                {turn.status === "pending" && (
                                    <>
                                        {displayCards.map((card, i) => (
                                            <article key={card.id} className="chat-card panel">
                                                <div className="chat-card__body">
                                                    <ResearchCardView card={card} isActive={i === 0} />
                                                </div>
                                            </article>
                                        ))}
                                    </>
                                )}

                                {/* ── ERROR STATE ───────────────────────────────── */}
                                {turn.status === "error" && (
                                    <article className="chat-card panel">
                                        <div className="chat-card__body">
                                            <p className="chat-card__error">
                                                {turn.errorMessage ?? "Kunde inte hämta svar."}
                                            </p>
                                        </div>
                                    </article>
                                )}

                                {/* ── READY STATE ───────────────────────────────── */}
                                {turn.status === "ready" && (
                                    <>
                                        {/* Research cards (if any) */}
                                        {displayCards.map((card) => (
                                            <article key={card.id} className="chat-card panel">
                                                <div className="chat-card__body">
                                                    <ResearchCardView
                                                        card={card}
                                                        isActive={false}
                                                        copyMarkdown={
                                                            card.isAnswer && turn.answer
                                                                ? () => chatAnswerToMarkdown(turn.answer!, turn.sources)
                                                                : undefined
                                                        }
                                                    />
                                                </div>
                                            </article>
                                        ))}

                                        {/* Sources are shown inline in the answer text via [1], [2] citation links */}

                                        {/* Tables */}
                                        {turn.tables && turn.tables.length > 0 && (
                                            <article className="chat-card panel">
                                                <div className="chat-view__tables">
                                                    {turn.tables.map((table, tableIndex) => (
                                                        <section key={`table-${turn.id}-${tableIndex}`} className="chat-table">
                                                            <header>
                                                                <h4>Sökresultat</h4>
                                                                <p>
                                                                    Visar {table.results.length} träffar
                                                                    {table.limit_reached && " (fler finns, använd ord-sök för fullständig lista)."}
                                                                </p>
                                                            </header>
                                                            <ResultsTable results={table.results} compact />
                                                        </section>
                                                    ))}
                                                </div>
                                            </article>
                                        )}
                                    </>
                                )}
                            </div>
                        );
                    })}
                </div>
                </div>
            )}

        </section>
    );
});
