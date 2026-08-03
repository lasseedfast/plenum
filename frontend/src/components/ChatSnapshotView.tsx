import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { getSnapshot, fetchPerson } from "../api";
import { getMpPhotoUrl } from "../utils/markdown";
import type { SnapshotData, ChatSource } from "../types";

function SourceList({ sources }: { sources: ChatSource[] }) {
    const [open, setOpen] = useState(false);
    if (!sources.length) return null;
    return (
        <div className="snapshot-sources">
            <button
                type="button"
                className="snapshot-sources__toggle"
                onClick={() => setOpen(o => !o)}
            >
                📎 {sources.length} käll{sources.length === 1 ? "a" : "or"} {open ? "▲" : "▼"}
            </button>
            {open && (
                <div className="snapshot-sources__list">
                    {sources.map((src, i) => (
                        <div key={`${src._id}-${i}`} className="snapshot-sources__item">
                            <div className="snapshot-sources__meta">
                                {src.person_id && (
                                    <img
                                        className="snapshot-sources__avatar"
                                        src={getMpPhotoUrl(src.person_id)}
                                        alt=""
                                        onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }}
                                    />
                                )}
                                <div>
                                    {src.speaker && <span className="snapshot-sources__speaker">{src.speaker}</span>}
                                    {src.date && <span className="snapshot-sources__date">{src.date}</span>}
                                </div>
                            </div>
                            {src.snippet && (
                                <p className="snapshot-sources__snippet">
                                    {src.snippet.replace(/\*\*(.*?)\*\*/g, "$1")}
                                </p>
                            )}
                            {src.url_video && (
                                <a href={src.url_video} className="snapshot-sources__link" target="_blank" rel="noreferrer">
                                    Öppna anförande ↗
                                </a>
                            )}
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}

function MpHeader({ person_id }: { person_id: string }) {
    const { data: person } = useQuery({
        queryKey: ["person", person_id],
        queryFn: () => fetchPerson(person_id),
    });
    if (!person) return null;
    const photoUrl = person.image_url_medium?.replace("http://", "https://") || getMpPhotoUrl(person_id);
    return (
        <div className="snapshot-mp-header panel">
            <img className="snapshot-mp-header__avatar" src={photoUrl} alt={person.name}
                onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }} />
            <div>
                <strong>{person.name}</strong>
                {person.party && <span className="party-chip" data-party={person.party} style={{ "--party-color": `var(--party-${person.party ?? ""})` } as React.CSSProperties}>{person.party}</span>}
                <p className="snapshot-mp-header__disclaimer">
                    Fryst konversation med digital assistent – inte den riktiga personen.
                </p>
            </div>
        </div>
    );
}

export function ChatSnapshotView() {
    const { uuid } = useParams<{ uuid: string }>();
    const navigate = useNavigate();
    const [snapshot, setSnapshot] = useState<SnapshotData | null>(null);
    const [loading, setLoading] = useState(true);
    const [notFound, setNotFound] = useState(false);
    const [copyDone, setCopyDone] = useState(false);

    useEffect(() => {
        if (!uuid) { setLoading(false); setNotFound(true); return; }
        getSnapshot(uuid)
            .then((data) => {
                if (!data) setNotFound(true);
                else setSnapshot(data);
            })
            .catch(() => setNotFound(true))
            .finally(() => setLoading(false));
    }, [uuid]); // eslint-disable-line react-hooks/exhaustive-deps

    const handleCopyLink = useCallback(async () => {
        await navigator.clipboard.writeText(window.location.href);
        setCopyDone(true);
        setTimeout(() => setCopyDone(false), 2000);
    }, []);

    return (
        <>
            <header className="page-header">
                <h1>Vad säger de i Riksdagen?</h1>
            </header>

            <main className="content">
                <div className="snapshot-toolbar">
                    <button type="button" className="secondary-button" onClick={() => navigate(-1)}>
                        ← Tillbaka
                    </button>
                    <button type="button" className="secondary-button chat-share-btn" onClick={handleCopyLink}>
                        {copyDone ? "Kopierat!" : "Kopiera länk"}
                    </button>
                </div>

                {loading && (
                    <div className="chat-loading"><span className="chat-loading__spinner" /></div>
                )}

                {notFound && (
                    <div className="panel snapshot-not-found">
                        <h2>Konversationen hittades inte</h2>
                        <p>Länken kanske är felaktig eller så har konversationen tagits bort.</p>
                    </div>
                )}

                {snapshot && (
                    <div className="snapshot-view">
                        <div className="snapshot-badge">Fryst konversation — skrivskyddad</div>

                        {snapshot.session_type === "mp" && snapshot.person_id && (
                            <MpHeader person_id={snapshot.person_id} />
                        )}

                        <div className="snapshot-turns">
                            {snapshot.turns.map((turn, i) => (
                                <div key={i} className="snapshot-turn">
                                    <div className="snapshot-turn__question">{turn.question}</div>
                                    <div
                                        className="snapshot-turn__answer"
                                        dangerouslySetInnerHTML={{ __html: turn.answerHtml }}
                                    />
                                    <SourceList sources={turn.sources ?? []} />
                                </div>
                            ))}
                        </div>
                    </div>
                )}
            </main>
        </>
    );
}
