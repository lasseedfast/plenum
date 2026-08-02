import { useEffect } from "react";
import { useParams, useSearchParams, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchPerson } from "../api";
import { MpChatPanel } from "./MpChatPanel";

export function MpChatView() {
    const { id } = useParams<{ id: string }>();
    const [searchParams, setSearchParams] = useSearchParams();
    const navigate = useNavigate();
    const talkId = searchParams.get("talk_id") ?? undefined;
    const sessionId = searchParams.get("session") ?? undefined;

    // Generate a session UUID on first visit and bake it into the URL
    useEffect(() => {
        if (!searchParams.get("session")) {
            const uuid = crypto.randomUUID
                ? crypto.randomUUID()
                : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
            setSearchParams(
                (prev) => {
                    const next = new URLSearchParams(prev);
                    next.set("session", uuid);
                    return next;
                },
                { replace: true },
            );
        }
    }, []); // eslint-disable-line react-hooks/exhaustive-deps

    const { data: person, isLoading, error } = useQuery({
        queryKey: ["person", id],
        queryFn: () => fetchPerson(id!),
        enabled: !!id,
    });

    return (
        <div className="mp-chat-view">
            <div className="mp-chat-view__back">
                <button
                    type="button"
                    className="secondary-button"
                    onClick={() => navigate(-1)}
                >
                    ← Tillbaka
                </button>
            </div>

            {isLoading && (
                <div className="panel mp-chat-view__loading">
                    <p>Laddar…</p>
                </div>
            )}

            {error && (
                <div className="panel error-banner">
                    Kunde inte ladda personen: {(error as Error).message}
                </div>
            )}

            {person && sessionId && (
                <MpChatPanel
                    person={person}
                    initialTalkId={talkId}
                    sessionId={sessionId}
                />
            )}
        </div>
    );
}
