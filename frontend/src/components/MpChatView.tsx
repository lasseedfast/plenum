import { useEffect, useState } from "react";
import { useParams, useSearchParams, useNavigate, Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchPerson } from "../api";
import { isCanonicalMpParam, mpPath, parseMpParam } from "../utils/mpLink";
import { MpChatPanel } from "./MpChatPanel";

/** A conversation id for this visit, without touching the URL. */
function newSessionId(): string {
    return crypto.randomUUID
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function MpChatView() {
    const { id: routeParam } = useParams<{ id: string }>();
    const [searchParams] = useSearchParams();
    const navigate = useNavigate();
    const personId = parseMpParam(routeParam);
    const talkId = searchParams.get("speech_id") ?? undefined;

    // Opening a profile is not starting a conversation. The session id used to be
    // written into the address bar on arrival, which made the URL people copied a
    // link to their own chat rather than to the member — so it is held in state,
    // and only read from the URL when a saved chat is deliberately being resumed
    // (from "Mina chattar", or a forked snapshot).
    const [sessionId] = useState(() => searchParams.get("session") ?? newSessionId());

    const { data: person, isLoading, error } = useQuery({
        queryKey: ["person", personId],
        queryFn: () => fetchPerson(personId),
        enabled: !!personId,
    });

    // Rewrite a bare id to the named form once the name is known, so the URL in
    // the address bar is always the one worth copying. Replace, not push: the
    // back button should leave the profile, not undo the rewrite.
    useEffect(() => {
        if (!person || isCanonicalMpParam(routeParam, person.person_id, person.name)) return;
        const search = searchParams.toString();
        navigate(`${mpPath(person.person_id, person.name)}${search ? `?${search}` : ""}`, { replace: true });
    }, [person, routeParam]); // eslint-disable-line react-hooks/exhaustive-deps

    return (
        <div className="mp-chat-view">
            <div className="mp-chat-view__back">
                <button
                    type="button"
                    className="secondary-button"
                    // A shared profile link is often the visitor's first page, with
                    // nothing behind it to go back to.
                    onClick={() => (window.history.length > 1 ? navigate(-1) : navigate("/"))}
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
                    Kunde inte ladda personen: {(error as Error).message}{" "}
                    <Link to="/">Till sök</Link>
                </div>
            )}

            {person && (
                <MpChatPanel
                    person={person}
                    initialTalkId={talkId}
                    sessionId={sessionId}
                />
            )}
        </div>
    );
}
