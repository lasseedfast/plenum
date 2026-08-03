import { useQuery } from "@tanstack/react-query";
import { useParams, Link, useNavigate } from "react-router-dom";
import { fetchTalk } from "../api";
import { marked } from "marked";
import DOMPurify from "dompurify";

/**
 * TalkView component displays a single talk with full details.
 * 
 * It shows:
 * - Speaker information with photo
 * - Talk metadata (date, debate type, etc.)
 * - Full text of the speech
 * - Links to related resources
 */
export function TalkView() {
	const { id } = useParams<{ id: string }>();
	const navigate = useNavigate();
	
	const { data: talk, isLoading, error } = useQuery({
		queryKey: ["talk", id],
		queryFn: () => fetchTalk(id!),
		enabled: !!id,
	});

	if (isLoading) {
		return (
			<div className="talk-view">
				<div className="panel">
					<p>Laddar anförande...</p>
				</div>
			</div>
		);
	}

	if (error) {
		return (
			<div className="talk-view">
				<div className="panel error-banner">
					Kunde inte ladda anförande: {error.message}
				</div>
			</div>
		);
	}

	if (!talk) {
		return (
			<div className="talk-view">
				<div className="panel">
					<p>Anförande hittades inte.</p>
				</div>
			</div>
		);
	}

	const convertMarkdownToHtml = (markdown: string): string => {
		// Convert Markdown summaries to HTML while keeping the output safe to inject.
		const rawHtml = marked.parse(markdown);
		const sanitized = DOMPurify.sanitize(rawHtml);
		return typeof sanitized === "string" ? sanitized : sanitized.toString();
	};

	// Fix image URLs from http to https
	const imageUrl = talk.person?.image_url_medium?.replace('http://', 'https://');
	const summaryHtml = talk.summary ? convertMarkdownToHtml(talk.summary) : null;
	
	const previousTalk = talk.navigation?.previous ?? null;
	const nextTalk = talk.navigation?.next ?? null;


	// Normalise database IDs so router links stay consistent.
	const normalizeTalkId = (rawId?: string | null): string | null => {
		if (!rawId) {
			return null;
		}
		// Handle both object with _id property and plain string
		const idString = typeof rawId === 'object' && rawId !== null && '_id' in rawId 
			? (rawId as any)._id 
			: String(rawId);
		const result = idString.startsWith("speeches/") ? idString.slice(6) : idString;
		return result;
	};
	const previousId = normalizeTalkId(previousTalk);
	const nextId = normalizeTalkId(nextTalk);
	
	// Go back in browser history — works from both search and chat.
	const handleBackClick = () => {
		if (window.history.length > 1) {
			navigate(-1);
		} else {
			navigate("/");
		}
	};

	return (
		<div className="talk-view">
			{/* Navigation row: previous, back, next */}
			<div className="talk-view__navRow">
				{/* Left: Föregående tal */}
				<div className="talk-view__navCell talk-view__navCell--left talk-view__navCell--minwidth">
					{previousId && previousTalk ? (
						<Link to={`/talk/${previousId}`} className="secondary-button talk-view__navButton">
							{/* Use fontSize 1em for arrow so button height matches others */}
							<span aria-hidden="true" style={{ fontSize: "1em", marginRight: "0.3em" }}>←</span>
							Föregående i protokollet
						</Link>
					) : (
						<span className="secondary-button talk-view__navButton" style={{ visibility: "hidden" }}>&nbsp;</span>
					)}
				</div>
				{/* Center: Tillbaka till sökresultat */}
				<div className="talk-view__navCell talk-view__navCell--center talk-view__navCell--minwidth">
					<button
						type="button"
						className="secondary-button talk-view__navButton"
						onClick={handleBackClick}
					>
						Tillbaka till sökresultat
					</button>
				</div>
				{/* Right: Nästa tal */}
				<div className="talk-view__navCell talk-view__navCell--right talk-view__navCell--minwidth">
					{nextId && nextTalk ? (
						<Link to={`/talk/${nextId}`} className="secondary-button talk-view__navButton">
							Nästa i protokollet
							{/* Use fontSize 1em for arrow so button height matches others */}
							<span aria-hidden="true" style={{ fontSize: "1em", marginLeft: "0.3em" }}>→</span>
						</Link>
					) : (
						<span className="secondary-button talk-view__navButton" style={{ visibility: "hidden" }}>&nbsp;</span>
					)}
				</div>
			</div>

			{/* Speaker card */}
			<div className="panel talk-view__speaker">
				{imageUrl && (
					<img 
						src={imageUrl} 
						alt={talk.speaker_name}
						className="talk-view__speaker-photo talk-view__speaker-photo--enhanced"
					/>
				)}
				<div className="talk-view__speaker-info">
					<div className="talk-view__speaker-row">
						{talk.person?.person_id ? (
							<Link to={`/mp/${talk.person.person_id}`} className="talk-view__speaker-name-link">
								<h1>{talk.speaker_name}</h1>
							</Link>
						) : (
							<h1>{talk.speaker_name}</h1>
						)}
						<span className="party-chip" data-party={talk.party ?? ""} style={{ "--party-color": `var(--party-${talk.party ?? ""})` } as React.CSSProperties}>
							{talk.party}
						</span>
					</div>
					<div className="talk-view__speaker-meta">
						{/* Show constituency if available */}
						{talk.person?.constituency && (
							<span className="talk-view__speaker-detail">
								{talk.person.constituency}
							</span>
						)}
						{/* Show status if available */}
						{talk.person?.status && (
							<span className="talk-view__speaker-detail">
								{talk.person.status}
							</span>
						)}
					</div>
					{talk.person?.person_id && (
						<Link
							to={`/mp/${talk.person.person_id}?speech_id=${id}`}
							className="secondary-button talk-view__chat-btn"
						>
							Chatta med {talk.person?.first_name || talk.speaker_name}
						</Link>
					)}
				</div>
			</div>

			{/* Talk metadata */}
			<div className="panel talk-view__metadata">
				<dl className="talk-view__meta-grid">
					<dt>Datum</dt>
					<dd>{talk.date}</dd>
					
					<dt>Debattyp</dt>
					<dd>{talk.activity_type}</dd>
					
					<dt>Rubrik</dt>
					<dd>{talk.section_title}</dd>
					
					{talk.title && (
						<>
							<dt>Protokoll</dt>
							<dd>{talk.title}</dd>
						</>
					)}
				</dl>
			</div>

			{summaryHtml && (
				<div className="panel talk-view__summary">
					<div className="talk-view__summaryHeading">
						<span role="img" aria-label="AI"></span> AI-genererad sammanfattning
					</div>
					<div
						className="talk-view__summaryContent"
						dangerouslySetInnerHTML={{ __html: summaryHtml }}
					/>
				</div>
			)}

			{/* Talk text */}
			<div className="panel talk-view__text">
				<div className="talk-view__content">
					{talk.text}
				</div>
			</div>

			{/* Links */}
			{(talk.url_session || talk.url_audio) && (
				<div className="panel talk-view__links">
					<h3>Länkar</h3>
					<div className="talk-view__link-group">
						{talk.url_session && (
							<a 
								href={talk.url_session} 
								target="_blank" 
								rel="noreferrer"
								className="primary"
							>
								Webb-TV →
							</a>
						)}
						{talk.url_audio && (
							<a 
								href={talk.url_audio} 
								target="_blank" 
								rel="noreferrer"
								className="primary"
							>
								Ljud →
							</a>
						)}
					</div>
				</div>
			)}
		</div>
	);
}
