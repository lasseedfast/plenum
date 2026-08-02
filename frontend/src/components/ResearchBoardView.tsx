import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ModeToggle, useModeNavigation } from "./ModeToggle";
import { useLLMSettings } from "../context/LLMSettingsContext";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
	activateResearchThreads,
	archiveResearchThread,
	cancelResearch,
	deepenResearch,
	generateResearchReport,
	getResearchBoard,
	getResearchEvents,
	seedResearchThread,
} from "../api";
import { useAuth } from "../context/AuthContext";
import { useTalkDrawer } from "../context/TalkDrawerContext";
import { decryptBoardDetail, decryptEvent } from "../utils/researchCrypto";
import { ResearchMarkdown } from "./ResearchMarkdown";
import { CopyMarkdownButton } from "./CopyMarkdownButton";
import { sourcesFromFindings, type CiteSource } from "../utils/researchMarkdown";
import { researchAnswerToMarkdown } from "../utils/copyMarkdown";
import type { ResearchFinding, ResearchLead, ResearchSpawnExtras, ResearchThread } from "../types";

const STATUS_LABELS: Record<string, string> = {
	new: "Ny",
	scouting: "Kartlägger…",
	awaiting: "Väntar på dina val",
	digging: "Gräver…",
	reporting: "Skriver rapport…",
	ready: "Klar",
	failed: "Misslyckades",
};

const BUSY_STATUSES = new Set(["scouting", "digging", "reporting"]);

function DepthDots({ depth, target }: { depth: number; target: number }) {
	const total = Math.max(target, depth);
	return (
		<span className="research-depth" title={`Djup ${depth} av ${target}`}>
			{Array.from({ length: total }, (_, i) => (
				<span key={i} className={i < depth ? "research-depth__dot research-depth__dot--filled" : "research-depth__dot"} />
			))}
		</span>
	);
}

function FindingRow({ finding }: { finding: ResearchFinding }) {
	const { openTalk } = useTalkDrawer();
	return (
		<li className="research-finding">
			<div className="research-finding__label">{finding.label}</div>
			{finding.detail && <div className="research-finding__detail">{finding.detail}</div>}
			{finding.quote && <blockquote className="research-finding__quote">”{finding.quote}”</blockquote>}
			{finding.source_id && (
				<button
					type="button"
					className="research-source-chip"
					onClick={() => openTalk(finding.source_id!)}
					title="Öppna talet"
				>
					{finding.speaker ?? "Källa"}
					{finding.party ? ` (${finding.party})` : ""}
					{finding.date ? ` · ${finding.date}` : ""}
				</button>
			)}
		</li>
	);
}

function LeadRow({
	lead,
	onDig,
	disabled,
}: {
	lead: ResearchLead;
	onDig: () => void;
	disabled: boolean;
}) {
	const kindLabel = lead.kind === "search" ? "Sök" : lead.kind === "person" ? "Person" : "Debatt";
	return (
		<li className="research-lead">
			<span className="research-lead__kind">{kindLabel}</span>
			<span className="research-lead__text">
				{lead.lead || lead.label || lead.target}
			</span>
			<button type="button" className="secondary-button" onClick={onDig} disabled={disabled}>
				Gräv vidare
			</button>
		</li>
	);
}

function ThreadCard({
	thread,
	targetDepth,
	busy,
	flash,
	onDeepenThread,
	onDeepenLead,
}: {
	thread: ResearchThread;
	targetDepth: number;
	busy: boolean;
	flash: boolean;
	onDeepenThread: () => void;
	onDeepenLead: (lead: ResearchLead) => void;
}) {
	const sources = useMemo(() => sourcesFromFindings(thread.findings), [thread.findings]);
	const answerStale = thread.depth > (thread.answer_depth ?? 0);

	const findingsBlock =
		thread.findings.length > 0 ? (
			<ul className="research-findings">
				{thread.findings.map((f, i) => (
					<FindingRow key={`${f.source_id}-${i}`} finding={f} />
				))}
			</ul>
		) : (
			<p className="research-thread__empty">{busy ? "Inga fynd ännu — gräver…" : "Inga fynd ännu."}</p>
		);

	return (
		<div className={`panel research-thread${flash ? " research-thread--flash" : ""}`}>
			<header className="panel-header">
				<h3>
					{thread.pinned && <span title="Egen tråd">📌 </span>}
					{thread.title}
				</h3>
				<DepthDots depth={thread.depth} target={targetDepth} />
			</header>
			<p className="research-thread__question">{thread.question}</p>
			{thread.guidance && <p className="research-thread__guidance">Medskick: {thread.guidance}</p>}
			{thread.why && <p className="research-thread__why">{thread.why}</p>}

			{thread.answer ? (
				<>
					<div className="research-answer">
						<ResearchMarkdown md={thread.answer} sources={sources} />
						<div className="research-answer__actions">
							<CopyMarkdownButton
								getMarkdown={() => researchAnswerToMarkdown(thread.answer!, sources)}
								label="Kopiera"
							/>
						</div>
					</div>
					{answerStale && busy && (
						<p className="research-thread__stale">Svaret uppdateras efter grävningen…</p>
					)}
					<details className="research-underlag">
						<summary>Underlag ({thread.findings.length} fynd)</summary>
						{findingsBlock}
					</details>
				</>
			) : (
				findingsBlock
			)}

			{thread.open_questions.length > 0 && (
				<div className="research-questions">
					<h4>Öppna frågor</h4>
					<ul>
						{thread.open_questions.map((q, i) => (
							<li key={i}>{q}</li>
						))}
					</ul>
				</div>
			)}

			{thread.leads.length > 0 && (
				<div className="research-leads">
					<h4>Spår att följa</h4>
					<ul>
						{thread.leads.map((l, i) => (
							<LeadRow key={`${l.kind}-${l.target}-${i}`} lead={l} onDig={() => onDeepenLead(l)} disabled={busy} />
						))}
					</ul>
				</div>
			)}

			<div className="research-thread__actions">
				<button type="button" className="secondary-button" onClick={onDeepenThread} disabled={busy}>
					Gräv djupare i tråden
				</button>
			</div>
		</div>
	);
}

type Selection = { checked: boolean; guidance: string };

function ProposalSection({
	proposals,
	heading,
	busy,
	onActivate,
	onDismiss,
	pending,
}: {
	proposals: ResearchThread[];
	heading: string;
	busy: boolean;
	onActivate: (selections: { thread_id: string; guidance?: string }[]) => void;
	onDismiss: (threadId: string) => void;
	pending: boolean;
}) {
	const [state, setState] = useState<Map<string, Selection>>(new Map());

	const setChecked = (id: string, checked: boolean) =>
		setState((prev) => {
			const next = new Map(prev);
			next.set(id, { guidance: prev.get(id)?.guidance ?? "", checked });
			return next;
		});
	const setGuidance = (id: string, guidance: string) =>
		setState((prev) => {
			const next = new Map(prev);
			next.set(id, { checked: prev.get(id)?.checked ?? false, guidance });
			return next;
		});

	const selected = proposals.filter((p) => state.get(p.id)?.checked);

	return (
		<form
			className="panel research-proposals"
			aria-busy={busy || pending}
			onSubmit={(e) => {
				e.preventDefault();
				if (busy || pending || selected.length === 0) return;
				onActivate(
					selected.map((p) => ({
						thread_id: p.id,
						guidance: state.get(p.id)?.guidance?.trim() || undefined,
					})),
				);
			}}
		>
			<header className="panel-header">
				<h2>{heading}</h2>
			</header>
			<div className="research-proposals__list">
				{proposals.map((p) => {
					const sel = state.get(p.id);
					return (
						<div key={p.id} className={`research-proposal${sel?.checked ? " research-proposal--checked" : ""}`}>
							<label className="research-proposal__head">
								<input
									type="checkbox"
									checked={sel?.checked ?? false}
									onChange={(e) => setChecked(p.id, e.target.checked)}
								/>
								<span className="research-proposal__title">{p.title}</span>
							</label>
							<p className="research-proposal__question">{p.question}</p>
							{p.why && <p className="research-proposal__why">{p.why}</p>}
							{sel?.checked && (
								<textarea
									className="research-proposal__guidance"
									value={sel.guidance}
									onChange={(e) => setGuidance(p.id, e.target.value)}
									placeholder="Eget medskick (valfritt) — t.ex. fokusera på 2019–2022 eller på Miljöpartiet"
									rows={2}
								/>
							)}
							<button
								type="button"
								className="research-proposal__dismiss"
								onClick={() => onDismiss(p.id)}
							>
								Avfärda
							</button>
						</div>
					);
				})}
			</div>
			<div className="research-proposals__footer">
				<button
					type="submit"
					className="primary"
					disabled={busy || pending || selected.length === 0}
				>
					{selected.length > 0 ? `Gräv i ${selected.length} valda trådar` : "Välj trådar att gräva i"}
				</button>
			</div>
		</form>
	);
}

export default function ResearchBoardView() {
	const { id } = useParams<{ id: string }>();
	const { providerOverride } = useLLMSettings();
	const goToMode = useModeNavigation();
	const queryClient = useQueryClient();
	const [seedText, setSeedText] = useState("");
	const [ticker, setTicker] = useState<string[]>([]);
	const eventsOffset = useRef(0);
	const prevCounts = useRef<Record<string, number>>({});
	const [flashIds, setFlashIds] = useState<Set<string>>(new Set());
	const { dek, getBoardKey } = useAuth();

	const board = useQuery({
		queryKey: ["research", id],
		queryFn: async () => {
			const data = await getResearchBoard(id!);
			if (data.enc && data.wrapped_board_key && dek) {
				const bk = await getBoardKey(data.id, data.wrapped_board_key);
				return decryptBoardDetail(data, bk.key);
			}
			return data;
		},
		enabled: Boolean(id),
		refetchInterval: (query) => (BUSY_STATUSES.has(query.state.data?.status ?? "") ? 3000 : false),
	});

	const busy = BUSY_STATUSES.has(board.data?.status ?? "");
	const jobId = board.data?.job?.job_id ?? null;
	const wrappedBoardKey = board.data?.enc ? board.data?.wrapped_board_key : null;

	/** Raw board key (base64) for job-spawning requests on encrypted boards. */
	const spawnKey = async (): Promise<string | undefined> => {
		if (!id || !wrappedBoardKey || !dek) return undefined;
		return (await getBoardKey(id, wrappedBoardKey)).rawB64;
	};

	/** Everything a spawn request needs beyond its own payload: the board key
	    and the user's chosen provider. Both ride the backend's secrets channel. */
	const spawnExtras = async (): Promise<ResearchSpawnExtras> => ({
		board_key: await spawnKey(),
		llm: providerOverride,
	});

	// Activity ticker: poll incremental job events while a job runs.
	useEffect(() => {
		if (!id || !jobId) return;
		eventsOffset.current = 0;
		setTicker([]);
		let stopped = false;
		const tick = async () => {
			try {
				const res = await getResearchEvents(id, jobId, eventsOffset.current);
				if (stopped) return;
				eventsOffset.current = res.offset;
				const key = wrappedBoardKey && dek ? (await getBoardKey(id, wrappedBoardKey)).key : null;
				const events = await Promise.all(res.events.map((e) => decryptEvent(e, key)));
				const msgs = events.map((e) => e.message).filter(Boolean) as string[];
				if (msgs.length) setTicker((prev) => [...prev.slice(-30), ...msgs]);
				if (res.is_done) {
					queryClient.invalidateQueries({ queryKey: ["research", id] });
					return;
				}
			} catch {
				/* transient poll errors are fine */
			}
			if (!stopped) timer = window.setTimeout(tick, 2500);
		};
		let timer = window.setTimeout(tick, 800);
		return () => {
			stopped = true;
			window.clearTimeout(timer);
		};
	}, [id, jobId, queryClient, wrappedBoardKey, dek, getBoardKey]);

	// Flash threads whose finding count grew since the previous poll.
	useEffect(() => {
		if (!board.data) return;
		const next = new Set<string>();
		for (const t of board.data.threads) {
			const prev = prevCounts.current[t.id] ?? 0;
			if (t.findings.length > prev) next.add(t.id);
			prevCounts.current[t.id] = t.findings.length;
		}
		if (next.size) {
			setFlashIds(next);
			const timer = window.setTimeout(() => setFlashIds(new Set()), 2500);
			return () => window.clearTimeout(timer);
		}
	}, [board.data]);

	const seed = useMutation({
		mutationFn: async (text: string) => seedResearchThread(id!, { text, ...(await spawnExtras()) }),
		onSuccess: () => {
			setSeedText("");
			queryClient.invalidateQueries({ queryKey: ["research", id] });
		},
	});

	const deepen = useMutation({
		mutationFn: async (payload: { thread_id?: string; lead?: ResearchLead; sweep?: boolean }) =>
			deepenResearch(id!, { ...payload, ...(await spawnExtras()) }),
		onSuccess: () => queryClient.invalidateQueries({ queryKey: ["research", id] }),
	});

	const activate = useMutation({
		mutationFn: async (selections: { thread_id: string; guidance?: string }[]) =>
			activateResearchThreads(id!, { selections, ...(await spawnExtras()) }),
		onSuccess: () => queryClient.invalidateQueries({ queryKey: ["research", id] }),
	});

	const dismiss = useMutation({
		mutationFn: (threadId: string) => archiveResearchThread(id!, threadId),
		onSuccess: () => queryClient.invalidateQueries({ queryKey: ["research", id] }),
	});

	const report = useMutation({
		mutationFn: async () => generateResearchReport(id!, await spawnExtras()),
		onSuccess: () => queryClient.invalidateQueries({ queryKey: ["research", id] }),
	});

	const cancel = useMutation({
		mutationFn: () => cancelResearch(id!),
		onSuccess: () => queryClient.invalidateQueries({ queryKey: ["research", id] }),
	});

	// Union of all active threads' findings for report citation chips. Kept
	// above the early returns so the hook order never changes between renders.
	const reportSources = useMemo(() => {
		const map = new Map<string, CiteSource>();
		for (const t of board.data?.threads ?? []) {
			if (t.status !== "active") continue;
			for (const f of t.findings) {
				if (f.source_id && !map.has(f.source_id)) {
					map.set(f.source_id, { speaker: f.speaker, party: f.party, date: f.date });
				}
			}
		}
		return map;
	}, [board.data]);

	// Shared shell for the loading/error branches: without it the H1 and the
	// mode toggle vanish while the board loads, and a failed load leaves the
	// user with no way back.
	const shell = (body: React.ReactNode) => (
		<>
			<header className="page-header">
				<h1>Research</h1>
				<p className="tagline">
					<Link to="/research">← Alla utforskningar</Link>
				</p>
			</header>
			<main className="content">
				<div className="panel mode-toggle-bar">
					<ModeToggle mode="research" onModeChange={goToMode} />
				</div>
				{body}
			</main>
		</>
	);

	if (board.isLoading) return shell(<div className="panel">Laddar…</div>);
	if (board.isError || !board.data) {
		return shell(
			<div className="panel error-banner">
				Kunde inte ladda utforskningen. <Link to="/research">Till listan</Link>
			</div>,
		);
	}

	const b = board.data;
	const lastTick = ticker.length ? ticker[ticker.length - 1] : null;
	const proposals = b.threads.filter((t) => t.status === "proposed");
	const activeThreads = b.threads.filter((t) => t.status === "active");
	const hasFindings = activeThreads.some((t) => t.findings.length > 0);
	const canSeed = seedText.trim().length >= 3 && !seed.isPending;

	return (
		<>
			<header className="page-header research-board__header">
				<div>
					<h1>{b.title}</h1>
					<p className="tagline">
						<Link to="/research">← Alla utforskningar</Link>
					</p>
				</div>
				<div className="research-board__status">
					<span className={`research-status research-status--${b.status}`}>
						{STATUS_LABELS[b.status] ?? b.status}
						{busy && <span className="research-spinner" aria-hidden="true" />}
					</span>
					{busy && (
						<button type="button" className="secondary-button" onClick={() => cancel.mutate()}>
							Avbryt
						</button>
					)}
					{!busy && activeThreads.length > 0 && (
						<button
							type="button"
							className="secondary-button"
							onClick={() => deepen.mutate({ sweep: true })}
							title="Kör ett varv till på alla trådar"
						>
							Gräv ett varv till
						</button>
					)}
				</div>
			</header>

			<main className="content">
				<div className="panel mode-toggle-bar">
					<ModeToggle mode="research" onModeChange={goToMode} />
				</div>
				{b.intro && (
					<div className="panel">
						<p className="research-board__intro">{b.intro}</p>
					</div>
				)}
				{busy && lastTick && <div className="research-ticker">{lastTick}</div>}
				{b.status === "failed" && (
					<div className="error-banner">
						Jobbet avbröts eller misslyckades — det som hann sparas finns kvar nedan.
					</div>
				)}
				{(deepen.isError || activate.isError || report.isError) && (
					<div className="error-banner">
						{((deepen.error || activate.error || report.error) as any)?.response?.data?.detail ??
							"Kunde inte starta jobbet."}
					</div>
				)}

				{/* Final report — rendered on top; board stays live below it. */}
				{b.report ? (
					<div className="panel research-report">
						<header className="panel-header">
							<h2>Rapport</h2>
							<div className="research-report__actions">
								<CopyMarkdownButton
									getMarkdown={() => researchAnswerToMarkdown(b.report!, reportSources)}
									label="Kopiera rapporten"
									className="copy-md-button--lg"
								/>
								<button
									type="button"
									className="secondary-button"
									onClick={() => report.mutate()}
									disabled={busy || report.isPending}
								>
									Uppdatera rapporten
								</button>
							</div>
						</header>
						{b.report_generated_at && (
							<p className="research-report__meta">
								Genererad {new Date(b.report_generated_at).toLocaleString("sv-SE")}
							</p>
						)}
						<ResearchMarkdown md={b.report} sources={reportSources} className="research-markdown research-report__body" />
					</div>
				) : (
					hasFindings && (
						<div className="panel research-report research-report--empty">
							<div>
								<h2>Rapport</h2>
								<p className="research-report__hint">
									Nöjd med grävningen? Skapa en samlad rapport av alla trådsvar.
								</p>
							</div>
							<button
								type="button"
								className="primary"
								onClick={() => report.mutate()}
								disabled={busy || report.isPending}
							>
								{report.isPending ? (
									<>
										<span className="button-spinner" aria-hidden="true" />
										<span>Skapar…</span>
									</>
								) : (
									"Skapa rapport"
								)}
							</button>
						</div>
					)
				)}

				{/* Proposed threads awaiting the user's selection. */}
				{proposals.length > 0 && (
					<ProposalSection
						proposals={proposals}
						heading={
							b.status === "awaiting"
								? "Föreslagna trådar — välj vilka som ska grävas"
								: "Nya förslag efter grävningen"
						}
						busy={busy}
						pending={activate.isPending}
						onActivate={(sel) => activate.mutate(sel)}
						onDismiss={(threadId) => dismiss.mutate(threadId)}
					/>
				)}

				<form
					className="panel research-seed"
					aria-busy={seed.isPending}
					onSubmit={(e) => {
						e.preventDefault();
						if (canSeed) seed.mutate(seedText.trim());
					}}
				>
					<label className="field field--query">
						<div className="search-bar">
							<input
								type="text"
								value={seedText}
								onChange={(e) => setSeedText(e.target.value)}
								placeholder="Lägg till en egen tråd, t.ex. 'Vad sa Socialdemokraterna om effektskatten?'"
							/>
							<button type="submit" className="primary search-button" disabled={!canSeed}>
								{seed.isPending ? (
									<>
										<span className="button-spinner" aria-hidden="true" />
										<span>Lägger till…</span>
									</>
								) : (
									"Lägg till tråd"
								)}
							</button>
						</div>
					</label>
				</form>

				{activeThreads.map((t) => (
					<ThreadCard
						key={t.id}
						thread={t}
						targetDepth={b.target_depth}
						busy={busy}
						flash={flashIds.has(t.id)}
						onDeepenThread={() => deepen.mutate({ thread_id: t.id })}
						onDeepenLead={(lead) => deepen.mutate({ thread_id: t.id, lead })}
					/>
				))}

				{activeThreads.length === 0 && proposals.length === 0 && (
					<div className="empty-state panel">
						<h2>{b.status === "scouting" ? "Kartlägger ämnet…" : "Inga trådar ännu"}</h2>
						{b.status === "scouting" && <p>Trådförslag dyker upp här inom kort — välj sedan vilka som ska grävas.</p>}
					</div>
				)}
			</main>
		</>
	);
}
