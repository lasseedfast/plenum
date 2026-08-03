import { useNavigate, Link } from "react-router-dom";
import type { TalkHit } from "../types";

type Props = {
	results: TalkHit[];
	exportResults?: TalkHit[];
	onLoadMore?: () => void;
	nextBatchSize?: number;
	compact?: boolean;
	downloadFilename?: string;
};

const EXPORT_LIMIT = 10_000;

const boldSnippet = (text: string): string =>
	text.replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>");

function exportToCsv(results: TalkHit[], filename: string) {
	if (results.length > EXPORT_LIMIT) {
		const confirmed = window.confirm(
			`Det finns ${results.length} träffar men exporten är begränsad till ${EXPORT_LIMIT.toLocaleString("sv-SE")}. Vill du ladda ner de första ${EXPORT_LIMIT.toLocaleString("sv-SE")} träffarna?`
		);
		if (!confirmed) return;
		results = results.slice(0, EXPORT_LIMIT);
	}
	const headers = ["speaker_name", "party", "text", "date", "title", "activity_type", "related_doc_id"];
	const rows = results.map((r) => [
		r.speaker ?? "",
		r.party ?? "",
		r.text ?? "",
		r.date ?? "",
		r.title ?? "",
		r.activity_type ?? "",
		r.related_doc_id ?? "",
	]);
	const csvContent = [headers, ...rows]
		.map((row) => row.map((cell) => `"${String(cell).replace(/"/g, '""')}"`).join(","))
		.join("\n");
	const blob = new Blob(["\uFEFF" + csvContent], { type: "text/csv;charset=utf-8;" });
	const url = URL.createObjectURL(blob);
	const a = document.createElement("a");
	a.href = url;
	a.download = filename;
	a.click();
	URL.revokeObjectURL(url);
}

export function ResultsTable({ results, exportResults, onLoadMore, nextBatchSize, compact, downloadFilename }: Props) {
	const navigate = useNavigate();

	return (
		<section className={`results-table${compact ? " results-table--compact" : ""}`}>
			<div className="results-table__toolbar">
				<button
					type="button"
					className="results-table__download-btn"
					onClick={() => exportToCsv(exportResults ?? results, downloadFilename ?? "riksdagen-resultat.csv")}
					title="Ladda ner alla träffar som CSV"
				>
					↓ {compact ? "CSV" : "Ladda ner CSV"}
				</button>
			</div>
			<table>
				<thead>
					<tr>
						<th>Datum</th>
						<th>Talare</th>
						<th>Parti</th>
						<th>Debattyp</th>
						<th>Utdrag</th>
						<th>Länkar</th>
					</tr>
				</thead>
				<tbody>
					{results.map((hit) => {
						// Always use _id, and strip "speeches/" prefix for routing
						const talkKey = hit._id?.startsWith("speeches/") ? hit._id.slice(6) : hit._id;
						if (!talkKey) {
							console.warn("Result hit missing _id:", hit);
						}

						// Handler for row click - navigates to talk page
						const handleRowClick = () => {
							navigate(`/talk/${talkKey}`);
						};

						// Handler to stop propagation for nested interactive elements
						const stopPropagation = (e: React.MouseEvent) => {
							e.stopPropagation();
						};

						return (
							<tr
								key={hit._id}
								className="results-table__row"
								data-party={hit.party ?? ""}
								style={{ "--party-color": `var(--party-${hit.party ?? ""})` } as React.CSSProperties}
								onClick={handleRowClick}
								role="button"
								tabIndex={0}
								onKeyDown={(e) => {
									// Allow keyboard navigation with Enter or Space
									if (e.key === 'Enter' || e.key === ' ') {
										e.preventDefault();
										handleRowClick();
									}
								}}
							>
								<td>{hit.date}</td>
								<td>
									{hit.person_id ? (
										<Link
											to={`/mp/${hit.person_id}`}
											className="results-table__speaker-link"
											onClick={stopPropagation}
										>
											{hit.speaker}
										</Link>
									) : (
										hit.speaker
									)}
								</td>
								<td>
									<span className="party-chip" data-party={hit.party ?? ""} style={{ "--party-color": `var(--party-${hit.party ?? ""})` } as React.CSSProperties}>
										{hit.party}
									</span>
								</td>
								<td>{hit.debate_type}</td>
								<td>
									<div className="snippet">
										<p dangerouslySetInnerHTML={{ __html: boldSnippet(hit.snippet) }} />
										{/* Stop propagation so details toggle doesn't trigger row click */}
										<details onClick={stopPropagation}>
											<summary>Längre utdrag</summary>
											<p className="snippet-long" dangerouslySetInnerHTML={{ __html: boldSnippet(hit.snippet_long) }} />
										</details>
									</div>
								</td>
								<td>
									{/* Stop propagation so external links work independently */}
									{hit.url_session && (
										<a
											href={hit.url_session}
											target="_blank"
											rel="noreferrer"
											onClick={stopPropagation}
										>
											Webb-TV
										</a>
									)}
									{hit.url_audio && (
										<a
											href={hit.url_audio}
											target="_blank"
											rel="noreferrer"
											onClick={stopPropagation}
										>
											Ljud
										</a>
									)}
								</td>
							</tr>
						);
					})}
				</tbody>
			</table>
			{onLoadMore && nextBatchSize && nextBatchSize > 0 && (
				<footer className="results-table__footer">
					<button type="button" className="primary" onClick={onLoadMore}>
						Visa {nextBatchSize} fler
					</button>
				</footer>
			)}
		</section>
	);
}
