import { useState } from "react";
import { Link } from "react-router-dom";
import { getMpPhotoUrl } from "../utils/markdown";
import type { Motion } from "../types";

// Preview length for the collapsed motion text. Motions run 2k–25k chars, so we
// show the first few paragraphs and let the reader expand if it looks relevant.
const PREVIEW_CHARS = 900;

/** Truncate at the last paragraph or word boundary before the limit, for a clean cut. */
function previewText(text: string): string {
	if (text.length <= PREVIEW_CHARS) return text;
	const slice = text.slice(0, PREVIEW_CHARS);
	const para = slice.lastIndexOf("\n\n");
	if (para > PREVIEW_CHARS * 0.5) return slice.slice(0, para);
	const space = slice.lastIndexOf(" ");
	return space > 0 ? slice.slice(0, space) : slice;
}

/**
 * Shared rendering of a motion's contents (authors, metadata, yrkanden, text).
 * Used by both MotionView (standalone page) and the TalkDrawer motion branch,
 * so the two stay in sync. Reuses talk-view__* classes for visual parity with
 * anföranden, adding a few motion-view__* rules for the parts speeches don't have.
 */
export function MotionBody({ motion }: { motion: Motion }) {
	const [expanded, setExpanded] = useState(false);
	const authors = motion.authors ?? [];
	const primary = authors[0];
	const coSigners = authors.slice(1);
	const primaryPhoto =
		primary?.image_url_medium?.replace("http://", "https://") ||
		(primary?.person_id ? getMpPhotoUrl(primary.person_id) : undefined);

	return (
		<div className="talk-view motion-view">
			{/* Primary author card, mirroring the talk speaker card */}
			<div className="talk-view__speaker">
				{primaryPhoto && (
					<img
						src={primaryPhoto}
						alt={primary?.name ?? ""}
						className="talk-view__speaker-photo talk-view__speaker-photo--enhanced"
						onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }}
					/>
				)}
				<div className="talk-view__speaker-info">
					<div className="talk-view__speaker-row">
						{primary?.person_id ? (
							<Link to={`/mp/${primary.person_id}`} className="talk-view__speaker-name-link">
								<h1>{primary?.name}</h1>
							</Link>
						) : (
							<h1>{primary?.name ?? motion.speaker_name}</h1>
						)}
						{primary?.party && (
							<span className="party-chip" data-party={primary.party} style={{ "--party-color": `var(--party-${primary.party ?? ""})` } as React.CSSProperties}>{primary.party}</span>
						)}
					</div>
					<div className="talk-view__speaker-meta">
						{primary?.constituency && <span className="talk-view__speaker-detail">{primary.constituency}</span>}
						{primary?.status && <span className="talk-view__speaker-detail">{primary.status}</span>}
					</div>
					{primary?.person_id && (
						<Link
							to={`/mp/${primary.person_id}?doc_id=${motion.doc_id}`}
							className="secondary-button talk-view__chat-btn"
						>
							Chatta med {primary?.first_name || primary?.name}
						</Link>
					)}
				</div>
			</div>

			<h2 className="motion-view__title">{motion.title}</h2>

			{coSigners.length > 0 && (
				<div className="motion-view__cosigners">
					<span className="motion-view__cosigners-label">Medundertecknare:</span>{" "}
					{coSigners.map((a, i) => (
						<span key={i} className="motion-view__cosigner">
							{a.person_id ? (
								<Link to={`/mp/${a.person_id}`}>{a.name}</Link>
							) : (
								a.name
							)}
							{a.party ? ` (${a.party})` : ""}
							{i < coSigners.length - 1 ? ", " : ""}
						</span>
					))}
				</div>
			)}

			<dl className="talk-view__meta-grid">
				<dt>Datum</dt>
				<dd>{motion.date}</dd>

				<dt>Riksmöte</dt>
				<dd>{motion.session_label}{motion.designation ? `:${motion.designation}` : ""}</dd>

				{motion.subtype && (<><dt>Typ</dt><dd>{motion.subtype}</dd></>)}
				{motion.committee && (<><dt>Utskott</dt><dd>{motion.committee}</dd></>)}
				{motion.status && (<><dt>Status</dt><dd>{motion.status}</dd></>)}
			</dl>

			{motion.yrkanden?.length > 0 && (
				<div className="motion-view__yrkanden">
					<h3>Förslag till riksdagsbeslut ({motion.yrkanden.length})</h3>
					<table className="motion-view__yrkanden-table">
						<thead>
							<tr>
								<th>Nr</th>
								<th>Förslag</th>
								<th>Utskottet</th>
								<th>Kammaren</th>
							</tr>
						</thead>
						<tbody>
							{motion.yrkanden.map((y, i) => (
								<tr key={i}>
									<td>{y.number}</td>
									<td>{y.text}</td>
									<td>{y.committee_recommendation ?? "—"}</td>
									<td>{y.chamber_decision ?? "—"}</td>
								</tr>
							))}
						</tbody>
					</table>
				</div>
			)}

			{motion.text ? (
				(() => {
					const text = motion.text;
					const isLong = text.length > PREVIEW_CHARS;
					const showFull = expanded || !isLong;
					return (
						<div className={`motion-view__text-wrap${!showFull ? " motion-view__text-wrap--collapsed" : ""}`}>
							<div className="talk-view__content motion-view__content">
								{showFull ? text : previewText(text) + "…"}
							</div>
							{isLong && (
								<button
									type="button"
									className="secondary-button motion-view__expand"
									onClick={() => setExpanded((v) => !v)}
								>
									{expanded ? "Visa mindre ▲" : "Se hela motionen ▼"}
								</button>
							)}
						</div>
					);
				})()
			) : (
				<p className="motion-view__no-text">
					Motionens fulltext saknas i databasen (endast inskannad PDF). Se länkarna nedan.
				</p>
			)}

			{(motion.url_html || motion.url_pdf) && (
				<div className="talk-view__link-group">
					{motion.url_html && (
						<a href={motion.url_html} target="_blank" rel="noreferrer" className="primary">
							Öppna på riksdagen.se →
						</a>
					)}
					{motion.url_pdf && (
						<a href={motion.url_pdf} target="_blank" rel="noreferrer" className="primary">
							PDF →
						</a>
					)}
				</div>
			)}
		</div>
	);
}
