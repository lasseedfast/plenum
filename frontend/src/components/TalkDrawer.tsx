import { useEffect } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { fetchTalk, fetchMotion } from "../api";
import { useTalkDrawer, normalizeTalkId } from "../context/TalkDrawerContext";
import { MotionBody } from "./MotionBody";
import type { Motion } from "../types";

function convertMarkdownToHtml(markdown: string): string {
	const rawHtml = marked.parse(markdown);
	const sanitized = DOMPurify.sanitize(rawHtml);
	return typeof sanitized === "string" ? sanitized : sanitized.toString();
}

/**
 * Slide-in panel showing a full anförande — or a motion — without leaving the
 * chat. Mounted once at the app root; visibility is driven by TalkDrawerContext
 * so any component (chat answers, search cards, MP chat) can open it. A stored
 * id prefixed "motions/" opens the motion branch; anything else is a talk.
 */
export function TalkDrawer() {
	const { openTalkId, openTalk, closeTalk } = useTalkDrawer();

	const isMotion = !!openTalkId && openTalkId.startsWith("motions/");
	const bareId = openTalkId ? openTalkId.replace(/^motions\//, "") : null;

	const { data: talk, isLoading, error } = useQuery({
		queryKey: [isMotion ? "motion" : "talk", openTalkId],
		queryFn: () => (isMotion ? fetchMotion(bareId!) : fetchTalk(openTalkId!)),
		enabled: !!openTalkId,
	});

	useEffect(() => {
		if (!openTalkId) return;
		const onKeyDown = (e: KeyboardEvent) => {
			if (e.key === "Escape") closeTalk();
		};
		document.addEventListener("keydown", onKeyDown);
		return () => document.removeEventListener("keydown", onKeyDown);
	}, [openTalkId, closeTalk]);

	if (!openTalkId) return null;

	const imageUrl = talk?.person?.bild_url_192?.replace("http://", "https://");
	const summaryHtml = talk?.summary ? convertMarkdownToHtml(talk.summary) : null;
	const previousId = !isMotion && talk?.navigation?.previous ? normalizeTalkId(talk.navigation.previous) : null;
	const nextId = !isMotion && talk?.navigation?.next ? normalizeTalkId(talk.navigation.next) : null;
	const expandPath = isMotion ? `/motion/${bareId}` : `/talk/${openTalkId}`;
	const ariaLabel = isMotion ? "Motion" : "Anförande";
	const loadingLabel = isMotion ? "Laddar motion..." : "Laddar anförande...";
	const errorLabel = isMotion ? "Kunde inte ladda motion" : "Kunde inte ladda anförande";

	return (
		<div className="talk-drawer-overlay">
			<div className="talk-drawer-overlay__backdrop" onClick={closeTalk} />
			<aside className="talk-drawer" role="dialog" aria-label={ariaLabel}>
				<div className="talk-drawer__toolbar">
					<div className="talk-drawer__toolbarNav">
						{!isMotion && (
							<>
								<button
									type="button"
									className="talk-drawer__navBtn"
									disabled={!previousId}
									onClick={() => previousId && openTalk(previousId)}
									aria-label="Föregående i protokollet"
									title="Föregående i protokollet"
								>
									←
								</button>
								<button
									type="button"
									className="talk-drawer__navBtn"
									disabled={!nextId}
									onClick={() => nextId && openTalk(nextId)}
									aria-label="Nästa i protokollet"
									title="Nästa i protokollet"
								>
									→
								</button>
							</>
						)}
					</div>
					<div className="talk-drawer__toolbarActions">
						<Link to={expandPath} className="talk-drawer__expandBtn" title="Öppna i eget fönster">
							Öppna i eget fönster ↗
						</Link>
						<button type="button" className="talk-drawer__closeBtn" onClick={closeTalk} aria-label="Stäng">
							×
						</button>
					</div>
				</div>

				<div className="talk-drawer__body">
					{isLoading && <p>{loadingLabel}</p>}
					{!isLoading && error && <p className="error-banner">{errorLabel}: {(error as Error).message}</p>}

					{!isLoading && !error && talk && isMotion && (
						<MotionBody motion={talk as Motion} />
					)}

					{!isLoading && !error && talk && !isMotion && (
						<div className="talk-view talk-drawer__talkView">
							<div className="talk-view__speaker">
								{imageUrl && (
									<img
										src={imageUrl}
										alt={talk.talare}
										className="talk-view__speaker-photo talk-view__speaker-photo--enhanced"
									/>
								)}
								<div className="talk-view__speaker-info">
									<div className="talk-view__speaker-row">
										{talk.person?.intressent_id ? (
											<Link to={`/mp/${talk.person.intressent_id}`} className="talk-view__speaker-name-link">
												<h1>{talk.talare}</h1>
											</Link>
										) : (
											<h1>{talk.talare}</h1>
										)}
										<span className="party-chip" data-party={talk.parti ?? ""}>
											{talk.parti}
										</span>
									</div>
									<div className="talk-view__speaker-meta">
										{talk.person?.valkrets && (
											<span className="talk-view__speaker-detail">{talk.person.valkrets}</span>
										)}
										{talk.person?.status && (
											<span className="talk-view__speaker-detail">{talk.person.status}</span>
										)}
									</div>
									{talk.person?.intressent_id && (
										<Link
											to={`/mp/${talk.person.intressent_id}?talk_id=${openTalkId}`}
											className="secondary-button talk-view__chat-btn"
										>
											Chatta med {talk.person?.tilltalsnamn || talk.talare}
										</Link>
									)}
								</div>
							</div>

							<dl className="talk-view__meta-grid">
								<dt>Datum</dt>
								<dd>{talk.datum}</dd>

								<dt>Debattyp</dt>
								<dd>{talk.kammaraktivitet}</dd>

								<dt>Rubrik</dt>
								<dd>{talk.avsnittsrubrik}</dd>

								{talk.titel && (
									<>
										<dt>Protokoll</dt>
										<dd>{talk.titel}</dd>
									</>
								)}
							</dl>

							{summaryHtml && (
								<div className="talk-view__summary">
									<div className="talk-view__summaryHeading">
										<span role="img" aria-label="AI"></span> AI-genererad sammanfattning
									</div>
									<div className="talk-view__summaryContent" dangerouslySetInnerHTML={{ __html: summaryHtml }} />
								</div>
							)}

							<div className="talk-view__content">{talk.anforandetext}</div>

							{(talk.url_session || talk.url_audio) && (
								<div className="talk-view__link-group">
									{talk.url_session && (
										<a href={talk.url_session} target="_blank" rel="noreferrer" className="primary">
											Webb-TV →
										</a>
									)}
									{talk.url_audio && (
										<a href={talk.url_audio} target="_blank" rel="noreferrer" className="primary">
											Ljud →
										</a>
									)}
								</div>
							)}
						</div>
					)}
				</div>
			</aside>
		</div>
	);
}
