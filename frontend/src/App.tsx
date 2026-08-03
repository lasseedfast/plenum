import { useEffect, useMemo, useRef, useState } from "react";
import { BrowserRouter, Routes, Route, useSearchParams, useNavigate } from "react-router-dom";
import { useQuery, useMutation } from "@tanstack/react-query"; // Hooks for data fetching & mutations
import { fetchMeta, searchTalks, sendFeedback, setSessionId } from "./api";  // API functions
import type { SearchFilters, TalkHit } from "./types";
import { setPhotoUrlTemplate } from "./utils/markdown";
import { Link } from "react-router-dom";
import { SearchPanel } from "./components/SearchPanel";       // Search form & filters
import { StatsView } from "./components/StatsView";           // Stats visualization
import { ResultsTable } from "./components/ResultsTable";     // Table of results
import { ChatSessionView } from "./components/ChatSessionView";
import { ChatSnapshotView } from "./components/ChatSnapshotView";
import { TalkView } from "./components/TalkView";
import { MotionView } from "./components/MotionView";
import { MpChatView } from "./components/MpChatView";
import { ForkRedirectView } from "./components/ForkRedirectView";
import { GuideView } from "./components/GuideView";
import ResearchListView from "./components/ResearchListView";
import ResearchBoardView from "./components/ResearchBoardView";
import MyChatsView from "./components/MyChatsView";
import { AccountMenu } from "./components/AccountMenu";
import { useModeNavigation } from "./components/ModeToggle";
import { TalkDrawer } from "./components/TalkDrawer";
import { TalkDrawerProvider } from "./context/TalkDrawerContext";
import { AuthProvider } from "./context/AuthContext";
import { LLMSettingsProvider } from "./context/LLMSettingsContext";

const PAGE_SIZE = 25; // Number of results to show per page

/**
 * Remove all @mentions from a query string.
 * Example: "bidrag @Anders Borg ekonomi" -> "bidrag ekonomi"
 *
 * This keeps the UI showing the @mention for clarity, but strips it before sending
 * to the backend since we're using speaker_ids for filtering instead.
 */
function stripMentions(query: string): string {
	let result = query;
	result = result.replace(/@"[^"]+"/g, '');
	result = result.replace(/@\S+(?:\s+\S+)*/g, '');
	result = result.replace(/\s+/g, ' ').trim();
	return result;
}

/** Parse comma-separated URL param, returning empty array if missing/empty. */
function parseList(value: string | null): string[] {
	if (!value) return [];
	return value.split(",").map(s => s.trim()).filter(Boolean);
}

function SearchView() {
	const [searchParams, setSearchParams] = useSearchParams();
	const navigate = useNavigate();
	const goToMode = useModeNavigation();

	// Initialise state directly from URL params (no sessionStorage)
	const [query, setQuery] = useState(() => searchParams.get("q") ?? "");
	const [filters, setFilters] = useState<SearchFilters>(() => ({
		parties: parseList(searchParams.get("parties")),
		people: parseList(searchParams.get("people")),
		debates: parseList(searchParams.get("debates")),
		from_year: searchParams.get("from") ? Number(searchParams.get("from")) : undefined,
		to_year: searchParams.get("to") ? Number(searchParams.get("to")) : undefined,
	}));
	const [speaker, setSpeaker] = useState<string | null>(() => searchParams.get("speakerName"));
	const [speakerIds, setSpeakerIds] = useState<string[]>(() => parseList(searchParams.get("speakers")));
	const [results, setResults] = useState<TalkHit[]>([]);

	// --- Pagination and sorting ---
	const [sortMode, setSortMode] = useState<"relevance" | "date">(
		() => (searchParams.get("sort") === "date" ? "date" : "relevance"),
	);
	const [visibleCount, setVisibleCount] = useState<number>(PAGE_SIZE);

	// --- Search state ---
	const [hasSearched, setHasSearched] = useState(false);
	const [lastError, setLastError] = useState<string | null>(null);

	const [speakerSuggestions] = useState<string[]>([]);

	const didInitialSearch = useRef(false);

	// --- Misc ---
	const [, setStats] = useState({ per_party: {}, per_year: {}, total: 0 });

	const meta = useQuery({
		queryKey: ["meta"],
		queryFn: fetchMeta,
	});

	// Publish the configured palette as CSS custom properties. Doing it here rather
	// than in the stylesheet is what lets a non-Swedish deployment render its own
	// parties: CSS cannot read a dict, so hardcoded per-party rules could never adapt.
	useEffect(() => {
		if (!meta.data) return;
		const root = document.documentElement;
		for (const party of meta.data.parties) {
			root.style.setProperty(`--party-${party.code}`, party.color);
		}
		root.style.setProperty("--party-na", meta.data.party_defaults.unknown_color);
		// Brand tokens, so a deployment can match its own parliament's visual language
		// without editing the stylesheet. Omitted keys keep the shipped defaults.
		for (const [token, value] of Object.entries(meta.data.theme ?? {})) {
			root.style.setProperty(`--${token.replace(/_/g, "-")}`, value);
		}
		setPhotoUrlTemplate(meta.data.urls?.person_photo ?? "");
		if (meta.data.site?.title) document.title = meta.data.site.title;
	}, [meta.data]);

	const feedback = useMutation({
		mutationFn: sendFeedback,
	});

	const searchMutation = useMutation({
		mutationFn: searchTalks,
		onSuccess: (data) => {
			setResults(data.results);
			setStats(data.stats);
			setHasSearched(true);
			setLastError(null);
		},
		onError: (error) => {
			setLastError(
				typeof error === "string" ? error : (error as Error).message ?? "Okänt fel vid hämtning av träffar.",
			);
			setHasSearched(true);
		},
	});

	// Run search automatically when the page loads with a query in the URL
	useEffect(() => {
		if (didInitialSearch.current) return;
		didInitialSearch.current = true;
		const q = searchParams.get("q");
		if (!q) return;
		const cleanQuery = stripMentions(q);
		const ids = parseList(searchParams.get("speakers"));
		searchMutation.mutate({
			q: cleanQuery,
			parties: parseList(searchParams.get("parties")),
			people: parseList(searchParams.get("people")),
			debates: parseList(searchParams.get("debates")),
			from_year: searchParams.get("from") ? Number(searchParams.get("from")) : undefined,
			to_year: searchParams.get("to") ? Number(searchParams.get("to")) : undefined,
			speaker_ids: ids.length > 0 ? ids : undefined,
			include_snippets: true,
		});
	}, []); // eslint-disable-line react-hooks/exhaustive-deps

	// Keep URL params in sync with state (replace history entry to avoid bloating back stack)
	useEffect(() => {
		const params: Record<string, string> = {};
		if (query) params.q = query;
		if (filters.parties.length) params.parties = filters.parties.join(",");
		if (filters.people.length) params.people = filters.people.join(",");
		if (filters.debates.length) params.debates = filters.debates.join(",");
		if (filters.from_year) params.from = String(filters.from_year);
		if (filters.to_year) params.to = String(filters.to_year);
		if (sortMode === "date") params.sort = "date";
		if (speakerIds.length) params.speakers = speakerIds.join(",");
		if (speaker) params.speakerName = speaker;
		setSearchParams(params, { replace: true });
	}, [query, filters, sortMode, speakerIds, speaker]); // eslint-disable-line react-hooks/exhaustive-deps

	useEffect(() => {
		// Guarantee each browser keeps a consistent session id for backend correlation.
		if (typeof window === "undefined") return;
		const storageKey = "riksdagen-session-id";
		const storedId = window.localStorage.getItem(storageKey);
		const session = storedId ?? (crypto.randomUUID ? crypto.randomUUID() : `session-${Date.now()}`);
		if (!storedId) window.localStorage.setItem(storageKey, session);
		setSessionId(session);
	}, []);

	// Reset pagination when filters change
	useEffect(() => {
		setVisibleCount(PAGE_SIZE);
	}, [filters, speaker, speakerIds]);

	const selectedDebateLabels = useMemo(() => {
		const mapping = meta.data?.activity_types ?? {};
		return (filters.debates ?? []).map((code) => {
			const debateType = mapping[code];
			return typeof debateType === 'string' ? debateType : debateType?.title ?? code;
		});
	}, [filters.debates, meta.data]);

	const filteredResults = useMemo<TalkHit[]>(() => {
		const partyFilter = new Set((filters.parties ?? []).filter(Boolean));
		const peopleFilter = new Set(
			(filters.people ?? []).map((name) => name.trim().toLowerCase()).filter(Boolean),
		);
		const debateFilter = new Set(
			selectedDebateLabels.map((label) => label.trim().toLowerCase()).filter(Boolean),
		);
		const normalizedSpeaker = speaker?.trim().toLowerCase() ?? null;
		const selectedSpeakerIds = speakerIds.map(id => id.trim()).filter(Boolean);
		const fromYearFilter = filters.from_year ?? null;
		const toYearFilter = filters.to_year ?? null;

		return results.filter((hit) => {
			const hitParty = (hit.party ?? "").trim();
			if (partyFilter.size && !partyFilter.has(hitParty)) return false;

			const hitSpeaker = (hit.speaker ?? "").trim().toLowerCase();
			const hitSpeakerId = hit.person_id?.trim() ?? null;

			if (selectedSpeakerIds.length > 0 && (!hitSpeakerId || !selectedSpeakerIds.includes(hitSpeakerId))) return false;
			if (selectedSpeakerIds.length === 0 && normalizedSpeaker && hitSpeaker !== normalizedSpeaker) return false;

			const hitDebate = (hit.debate_type ?? "").trim().toLowerCase();
			if (debateFilter.size && (!hitDebate || !debateFilter.has(hitDebate))) return false;

			const hitYear =
				typeof hit.year === "number"
					? hit.year
					: hit.date
						? Number.parseInt(hit.date.slice(0, 4), 10)
						: undefined;

			if (fromYearFilter !== null && typeof hitYear === "number" && hitYear < fromYearFilter) return false;
			if (toYearFilter !== null && typeof hitYear === "number" && hitYear > toYearFilter) return false;

			return true;
		});
	}, [results, filters, speaker, speakerIds, selectedDebateLabels]);

	const displayStats = useMemo(() => {
		const perParty: Record<string, number> = {};
		const perYear: Record<number, number> = {};
		for (const hit of filteredResults) {
			if (hit.party) perParty[hit.party] = (perParty[hit.party] ?? 0) + 1;
			const hitYear =
				typeof hit.year === "number"
					? hit.year
					: hit.date
						? Number.parseInt(hit.date.slice(0, 4), 10)
						: undefined;
			if (typeof hitYear === "number") perYear[hitYear] = (perYear[hitYear] ?? 0) + 1;
		}
		return { per_party: perParty, per_year: perYear, total: filteredResults.length };
	}, [filteredResults]);

	const sortedResults = useMemo<TalkHit[]>(() => {
		if (sortMode === "date") {
			return [...filteredResults].sort((a, b) => {
				const timeA = a.date ? new Date(a.date).getTime() : 0;
				const timeB = b.date ? new Date(b.date).getTime() : 0;
				return timeB - timeA;
			});
		}
		return filteredResults;
	}, [filteredResults, sortMode]);

	const visibleResults = useMemo<TalkHit[]>(
		() => sortedResults.slice(0, visibleCount),
		[sortedResults, visibleCount],
	);

	const hasMoreResults = visibleResults.length < sortedResults.length;
	const remainingResults = sortedResults.length - visibleResults.length;
	const nextBatchSize = remainingResults > 0 ? Math.min(PAGE_SIZE, remainingResults) : 0;
	const handleLoadMore = () => {
		setVisibleCount((count) => Math.min(count + PAGE_SIZE, sortedResults.length));
	};

	const handleStartChat = () => goToMode("chat");

	return (
		<>
			<header className="page-header">
				<h1>Vad säger de i Riksdagen?</h1>
			</header>

			<main className="content">
				<SearchPanel
					meta={meta.data}
					query={query}
					filters={filters}
					onQueryChange={setQuery}
					onFiltersChange={setFilters}
					onSubmit={() => {
						const cleanQuery = stripMentions(query);
						const hasMention = query.includes("@");
						if (!hasMention) {
							setSpeaker(null);
							setSpeakerIds([]);
						}
						const effectiveSpeakerIds = hasMention ? speakerIds : [];
						searchMutation.mutate({
							q: cleanQuery,
							...filters,
							speaker_ids: effectiveSpeakerIds.length > 0 ? effectiveSpeakerIds : undefined,
							include_snippets: true,
						});
					}}
					speakerSuggestions={speakerSuggestions}
					onSelectSpeaker={(name, selectedSpeakerId) => {
						setSpeaker(name);
						setSpeakerIds(selectedSpeakerId ? [selectedSpeakerId] : []);
					}}
					isSearching={searchMutation.isPending}
					mode="search"
					onModeChange={goToMode}
					chatInput=""
					onChatInputChange={() => {}}
					onChatSubmit={() => {}}
					isChatSending={false}
					canResetChat={false}
					onResetChat={() => {}}
				/>

				{/* "Chatta med" shortcut when a single MP is filtered */}
				{speakerIds.length === 1 && speaker && (
					<div className="mp-chat-shortcut">
						<Link to={`/mp/${speakerIds[0]}`} className="secondary-button">
							Chatta med {speaker}
						</Link>
					</div>
				)}

				{/* Show stats and table when we have hits */}
				{sortedResults.length > 0 && (
					<>
						<StatsView stats={displayStats} meta={meta.data} />
						<div className="results-controls" role="region" aria-label="Resultathantering">
							<div className="results-controls__group" role="radiogroup" aria-label="Sortera träffar">
								<span className="results-controls__label">Sortera efter:</span>
								<div className="results-controls__toggle">
									<button
										type="button"
										data-active={sortMode === "relevance"}
										onClick={() => setSortMode("relevance")}
									>
										Relevans
									</button>
									<button
										type="button"
										data-active={sortMode === "date"}
										onClick={() => setSortMode("date")}
									>
										Datum
									</button>
								</div>
							</div>
							<div className="results-controls__group">
								<span className="results-controls__label">
									Visar {visibleResults.length} av {sortedResults.length} träffar
								</span>
							</div>
						</div>
						<ResultsTable
							results={visibleResults}
							exportResults={sortedResults}
							onLoadMore={hasMoreResults ? handleLoadMore : undefined}
							nextBatchSize={nextBatchSize}
						/>
					</>
				)}

				{lastError && <div className="error-banner">{lastError}</div>}

				{hasSearched && sortedResults.length === 0 && !lastError && (
					<div className="empty-state panel">
						<h2>Inga träffar</h2>
					</div>
				)}
			</main>
		</>
	);
}

function App() {
	return (
		<BrowserRouter>
			<AuthProvider>
				{/* Inside AuthProvider (needs the DEK to decrypt account settings)
				    and outside AccountMenu, which opens the settings modal. */}
				<LLMSettingsProvider>
				<TalkDrawerProvider>
					<AccountMenu />
					<div className="app">
						<Routes>
							<Route path="/" element={<SearchView />} />
							<Route path="/chat/:uuid" element={<ChatSessionView />} />
							<Route path="/chats" element={<MyChatsView />} />
							<Route path="/share/:uuid" element={<ChatSnapshotView />} />
							<Route path="/fork/:uuid" element={<ForkRedirectView />} />
							<Route path="/talk/:id" element={<TalkView />} />
							<Route path="/motion/:id" element={<MotionView />} />
							<Route path="/mp/:id" element={<MpChatView />} />
							<Route path="/research" element={<ResearchListView />} />
							<Route path="/research/:id" element={<ResearchBoardView />} />
							<Route path="/guide" element={<GuideView />} />
						</Routes>
					</div>
					<TalkDrawer />
				</TalkDrawerProvider>
				</LLMSettingsProvider>
			</AuthProvider>
		</BrowserRouter>
	);
}

export default App;
