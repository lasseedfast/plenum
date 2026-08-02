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
 * anföranden, adding a few motion-view__* rules for the parts talks don't have.
 */
export function MotionBody({ motion }: { motion: Motion }) {
	const [expanded, setExpanded] = useState(false);
	const authors = motion.authors ?? [];
	const primary = authors[0];
	const coSigners = authors.slice(1);
	const primaryPhoto =
		primary?.bild_url_192?.replace("http://", "https://") ||
		(primary?.intressent_id ? getMpPhotoUrl(primary.intressent_id) : undefined);

	return (
		<div className="talk-view motion-view">
			{/* Primary author card, mirroring the talk speaker card */}
			<div className="talk-view__speaker">
				{primaryPhoto && (
					<img
						src={primaryPhoto}
						alt={primary?.namn ?? ""}
						className="talk-view__speaker-photo talk-view__speaker-photo--enhanced"
						onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }}
					/>
				)}
				<div className="talk-view__speaker-info">
					<div className="talk-view__speaker-row">
						{primary?.intressent_id ? (
							<Link to={`/mp/${primary.intressent_id}`} className="talk-view__speaker-name-link">
								<h1>{primary?.namn}</h1>
							</Link>
						) : (
							<h1>{primary?.namn ?? motion.talare}</h1>
						)}
						{primary?.partibet && (
							<span className="party-chip" data-party={primary.partibet}>{primary.partibet}</span>
						)}
					</div>
					<div className="talk-view__speaker-meta">
						{primary?.valkrets && <span className="talk-view__speaker-detail">{primary.valkrets}</span>}
						{primary?.status && <span className="talk-view__speaker-detail">{primary.status}</span>}
					</div>
					{primary?.intressent_id && (
						<Link
							to={`/mp/${primary.intressent_id}?motion_id=${motion.dok_id}`}
							className="secondary-button talk-view__chat-btn"
						>
							Chatta med {primary?.tilltalsnamn || primary?.namn}
						</Link>
					)}
				</div>
			</div>

			<h2 className="motion-view__title">{motion.titel}</h2>

			{coSigners.length > 0 && (
				<div className="motion-view__cosigners">
					<span className="motion-view__cosigners-label">Medundertecknare:</span>{" "}
					{coSigners.map((a, i) => (
						<span key={i} className="motion-view__cosigner">
							{a.intressent_id ? (
								<Link to={`/mp/${a.intressent_id}`}>{a.namn}</Link>
							) : (
								a.namn
							)}
							{a.partibet ? ` (${a.partibet})` : ""}
							{i < coSigners.length - 1 ? ", " : ""}
						</span>
					))}
				</div>
			)}

			<dl className="talk-view__meta-grid">
				<dt>Datum</dt>
				<dd>{motion.datum}</dd>

				<dt>Riksmöte</dt>
				<dd>{motion.rm}{motion.beteckning ? `:${motion.beteckning}` : ""}</dd>

				{motion.subtyp && (<><dt>Typ</dt><dd>{motion.subtyp}</dd></>)}
				{motion.organ && (<><dt>Utskott</dt><dd>{motion.organ}</dd></>)}
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
									<td>{y.nummer}</td>
									<td>{y.lydelse}</td>
									<td>{y.utskottet ?? "—"}</td>
									<td>{y.kammaren ?? "—"}</td>
								</tr>
							))}
						</tbody>
					</table>
				</div>
			)}

			{motion.anforandetext ? (
				(() => {
					const text = motion.anforandetext;
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

			{(motion.dokument_url_html || motion.pdf_url) && (
				<div className="talk-view__link-group">
					{motion.dokument_url_html && (
						<a href={motion.dokument_url_html} target="_blank" rel="noreferrer" className="primary">
							Öppna på riksdagen.se →
						</a>
					)}
					{motion.pdf_url && (
						<a href={motion.pdf_url} target="_blank" rel="noreferrer" className="primary">
							PDF →
						</a>
					)}
				</div>
			)}
		</div>
	);
}
