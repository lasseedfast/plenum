import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import type { MetaResponse, SearchFilters } from "../types";
import MentionInput, { MentionSuggestion } from "../components/MentionInput";
import MentionTextarea from "../components/MentionTextarea";
import { ExplainerText } from "./ExplainerText";
import { fetchGuide } from "../api";
import { useLLMSettings } from "../context/LLMSettingsContext";
import { ModeToggle, type SearchMode } from "./ModeToggle";

type Props = {
	meta?: MetaResponse;
	filters: SearchFilters;
	query: string;
	onQueryChange: (value: string) => void;
	onFiltersChange: (filters: SearchFilters) => void;
	onSubmit: () => void;
	speakerSuggestions: string[];
	onSelectSpeaker: (speaker: string, speakerId?: string) => void; // Changed signature
	isSearching: boolean;
	mode: SearchMode;
	onModeChange: (mode: SearchMode) => void;
	chatInput: string;
	onChatInputChange: (value: string) => void;
	onChatSubmit: () => void;
	isChatSending: boolean;
	canResetChat: boolean;
	onResetChat: () => void;
	onChatMentionSelect?: (suggestion: MentionSuggestion) => void;
	chatInputRef?: React.Ref<{ getFinalText: () => string }>;
};

const resizeChatTextarea = (element: HTMLTextAreaElement) => {
	// Grow/shrink the textarea to match its content.
	element.style.height = "auto";
	element.style.height = `${element.scrollHeight}px`;
};

export function SearchPanel({
	meta,
	filters,
	query,
	onQueryChange,
	onFiltersChange,
	onSubmit,
	speakerSuggestions,
	onSelectSpeaker,
	isSearching,
	mode,
	onModeChange,
	chatInput,
	onChatInputChange,
	onChatSubmit,
	isChatSending,
	canResetChat,
	onResetChat,
	onChatMentionSelect,
	chatInputRef,
}: Props) {
	const partyOptions = useMemo(() => Object.keys(meta?.parties ?? {}).filter(Boolean), [meta]);
	const debateOptions = useMemo(
		() => Object.entries(meta?.debate_types ?? {}),
		[meta],
	);

	const yearBounds = useMemo<{ min: number; max: number }>(() => {
		const metaWithYears = meta as (MetaResponse & { year_range?: { min: number; max: number }; years?: number[] }) | undefined;
		if (metaWithYears?.year_range) return metaWithYears.year_range;
		if (metaWithYears?.years?.length) {
			const years = [...metaWithYears.years].sort((a, b) => a - b);
			return { min: years[0], max: years[years.length - 1] };
		}
		return { min: 1993, max: new Date().getFullYear() };
	}, [meta]);

	const fromYear = filters.from_year ?? yearBounds.min;
	const toYear = filters.to_year ?? yearBounds.max;
	const sliderSpan = Math.max(yearBounds.max - yearBounds.min, 1);

	const handleFromYearChange = (value: number) => {
		const clamped = Math.max(yearBounds.min, Math.min(value, toYear));
		onFiltersChange({ ...filters, from_year: clamped, to_year: toYear });
	};

	const handleToYearChange = (value: number) => {
		const clamped = Math.min(yearBounds.max, Math.max(value, fromYear));
		onFiltersChange({ ...filters, from_year: fromYear, to_year: clamped });
	};

	const chatDisabled = chatInput.trim().length === 0;
	const chatInputElement = useRef<HTMLTextAreaElement | null>(null);

	useEffect(() => {
		if (chatInputElement.current) {
			resizeChatTextarea(chatInputElement.current);
		}
	}, [chatInput]);

	const handleChatKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
		if (event.key === "Enter" && !event.metaKey && !event.ctrlKey && !event.shiftKey) {
			event.preventDefault();
			onChatSubmit?.();
		}
	};

	// State for expanding/collapsing the info panel for ord-sök
	const [infoExpanded, setInfoExpanded] = useState(false);
	// State for expanding/collapsing the info panel for chat
	const [chatInfoExpanded, setChatInfoExpanded] = useState(false);
	// State for expanding/collapsing the LLM provider settings panel
	const { openSettings } = useLLMSettings();
	// Copy-guide state
	const [guideCopied, setGuideCopied] = useState(false);

	const handleCopyGuide = async () => {
		try {
			const text = await fetchGuide();
			if (navigator.clipboard) {
				await navigator.clipboard.writeText(text);
			} else {
				// Fallback for HTTP contexts where clipboard API is unavailable
				const el = document.createElement("textarea");
				el.value = text;
				el.style.position = "fixed";
				el.style.opacity = "0";
				document.body.appendChild(el);
				el.select();
				document.execCommand("copy");
				document.body.removeChild(el);
			}
			setGuideCopied(true);
			setTimeout(() => setGuideCopied(false), 2000);
		} catch (e) {
			console.error("Copy failed:", e);
		}
	};

	return (
		<section className="search-panel panel">
			<header className="panel-header">
				<ModeToggle mode={mode} onModeChange={onModeChange} />
				{mode === "chat" && (
					<button
						type="button"
						className="secondary-button panel-header__reset"
						onClick={onResetChat}
						disabled={!canResetChat || isChatSending}
					>
						Ny chatt
					</button>
				)}
			</header>
			{mode === "search" && (
				<form
					onSubmit={(event) => {
						event.preventDefault();
						onSubmit();
					}}
					aria-busy={isSearching}
				>
					<label className="field field--query">
						<div className="search-bar">
							<MentionInput
								value={query}
								onChange={(v) => onQueryChange(v)}
								onPick={(suggestion) => {
									// Pass both name and _key to parent
									onSelectSpeaker(suggestion.name, suggestion._key);
								}}
								placeholder='Sök efter riksdagsledamöternas anföranden i Riksdagen'
								className="search-panel__input"
								fetchUrl="/api/suggest"
								minChars={3}
								maxSuggestions={8}
							/>
							<button type="submit" className="primary search-button" disabled={isSearching}>
								{isSearching ? (
									<>
										<span className="button-spinner" aria-hidden="true" />
										<span>Söker...</span>
									</>
								) : (
									"Sök"
								)}
							</button>
						</div>
					</label>
					{/* --- Info panel about ord-sök --- */}
					<div className="search-info-panel" style={{ margin: "0.5em 0" }}>
						<button
							type="button"
							className="secondary-button"
							aria-expanded={infoExpanded}
							onClick={() => setInfoExpanded((v) => !v)}
							style={{ fontSize: "0.95em", padding: "0.3em 0.8em", marginBottom: "0.2em" }}
						>
							{infoExpanded ? "Stäng sökguide" : "Sökguide"}
						</button>
						{infoExpanded && (
							<div className="search-info-content" style={{ background: "#f8f8f8", borderRadius: "6px", padding: "0.8em", fontSize: "0.97em" }}>
								<strong>Så här söker du:</strong>
								<ul style={{ marginTop: "0.5em", paddingLeft: "1.2em" }}>
									<li>
										<b>Namn-sökning:</b> Skriv <code>@</code> före ett namn för att söka vad en viss person har sagt.<br />
										<code>@Anders Borg bidrag</code> visar tal av Anders Borg om bidrag.<br />
										<small>Du kan välja talare från förslagen under sökrutan.</small>
									</li>
									<li>
										<b>"Google-stil":</b> Skriv ord eller fraser för att hitta tal där de förekommer.<br />
										<code>klimatpolitik energikris "fossilfri"</code>
									</li>
									<li>
										<b>OR-sökning:</b> Skriv <code>OR</code> mellan ord för att hitta tal som innehåller något av dem.<br />
										<code>vindkraft OR kärnkraft</code>
									</li>
									<li>
										<b>Exkludera ord:</b> Sätt <code>-</code> framför ord för att utesluta dem.<br />
										<code>klimat -skatt</code>
									</li>
								</ul>
								<p style={{ marginTop: "0.7em" }}>
									Du kan kombinera flera filter och sökmetoder i samma sökning.
								</p>
								<p style={{ marginTop: "1em", fontSize: "0.95em" }}>
									<strong>Om appen och dess begränsningar:</strong>{" "}
									<Link to="/guide">Läs guiden</Link>
									{" · "}
									<a href="/api/guide" target="_blank" rel="noopener noreferrer" onClick={handleCopyGuide}>
										{guideCopied ? "Öppnad och kopierad!" : "Öppna som råtext"}
									</a> (för att klistra in i en AI-chatt)
								</p>
							</div>
						)}
					</div>
					{/* --- End info panel --- */}
					{speakerSuggestions.length > 0 && (
						<div className="speaker-suggestions">
							<span>Träffade talare:</span>
							{speakerSuggestions.map((suggestion) => (
								<button type="button" key={suggestion} onClick={() => onSelectSpeaker(suggestion)}>
									{suggestion}
								</button>
							))}
						</div>
					)}
					<div className="filters">
						<label className="field">
							<span>Partier</span>
							<select
								multiple
								value={filters.parties}
								onChange={(event) =>
									onFiltersChange({
										...filters,
										parties: Array.from(event.target.selectedOptions).map(
											(option) => option.value,
										),
									})
								}
							>
								{partyOptions.map((party) => (
									<option key={party} value={party}>
										{party}
									</option>
								))}
							</select>
						</label>
						<label className="field">
							<span>Debatttyper</span>
							<select
								multiple
								value={filters.debates}
								onChange={(event) =>
									onFiltersChange({
										...filters,
										debates: Array.from(event.target.selectedOptions).map(
											(option) => option.value,
										),
									})
								}
							>
								{debateOptions.map(([code, debateType]) => {
									const title = typeof debateType === 'string' ? debateType : debateType?.title;
									const description = typeof debateType === 'object' && debateType?.description ? debateType.description : null;
									return (
										<option key={code} value={code} title={description || undefined}>
											{title}
										</option>
									);
								})}
							</select>
						</label>
						<div className="field year-filter">
							<span>År</span>
							<div className="range-slider" role="group" aria-label="Filtrera på årtal">
								<div className="range-slider__inputs">
									<input
										type="range"
										min={yearBounds.min}
										max={yearBounds.max}
										step={1}
										value={fromYear}
										onChange={(event) => handleFromYearChange(Number(event.target.value))}
									/>
									<input
										type="range"
										min={yearBounds.min}
										max={yearBounds.max}
										step={1}
										value={toYear}
										onChange={(event) => handleToYearChange(Number(event.target.value))}
									/>
									<div
										className="range-slider__progress"
										style={{
											left: `${((fromYear - yearBounds.min) / sliderSpan) * 100}%`,
											right: `${100 - ((toYear - yearBounds.min) / sliderSpan) * 100}%`,
										}}
									/>
								</div>
								<div className="range-slider__labels" aria-hidden="true">
									<span>{fromYear}</span>
									<span>{toYear}</span>
								</div>
							</div>
						</div>
					</div>
				</form>
			)}
			{mode === "chat" && (
				<form
					className="chat-form"
					onSubmit={(event) => {
						event.preventDefault();
						onChatSubmit();
					}}
					aria-busy={isChatSending}
				>
					<div className="chat-form__content">
						<label className="field field--query field--chat">
							<div className="search-bar">
								<MentionTextarea
									ref={chatInputRef as any}
									rows={1}
									className="chat-panel__input"
									value={chatInput}
									onChange={(v) => {
										onChatInputChange(v);
										requestAnimationFrame(() => {
											const el = chatInputRef?.current;
											if (el && "resizeChatTextarea" in el) el.resizeChatTextarea();
										});
									}}
									onPick={(suggestion) => onChatMentionSelect?.(suggestion)}
									onSubmit={() => onChatSubmit()}
									placeholder="Ställ din fråga…"
								/>
								<div className="chat-actions">
									<button
										type="submit"
										className="primary search-button"
										disabled={isChatSending || chatDisabled}
									>
										{isChatSending ? (
											<>
												<span className="button-spinner" aria-hidden="true" />
												<span>Skickar...</span>
											</>
										) : (
											"Skicka"
										)}
									</button>
								</div>
							</div>
						</label>
						{/* --- Chat info panel --- */}
						<div className="chat-info-panel" style={{ margin: "0.5em 0" }}>
							<div style={{ display: "flex", gap: "0.5em", flexWrap: "wrap", marginBottom: "0.2em" }}>
								<button
									type="button"
									className="secondary-button"
									aria-expanded={chatInfoExpanded}
									onClick={() => setChatInfoExpanded((v) => !v)}
									style={{ fontSize: "0.95em", padding: "0.3em 0.8em" }}
								>
									{chatInfoExpanded ? "Stäng" : "Om chatten"}
								</button>
								<button
									type="button"
									className="secondary-button"
									onClick={openSettings}
									style={{ fontSize: "0.95em", padding: "0.3em 0.8em" }}
								>
									AI-inställningar
								</button>
							</div>
							{chatInfoExpanded && (
								<div className="chat-info-content" style={{ background: "#f8f8f8", borderRadius: "6px", padding: "0.8em", fontSize: "0.97em" }}>
									<p>
										Chatten är en funktion under utveckling. Den försöker svara på frågor om vad som sagts i riksdagen, men kan ibland ge felaktiga eller konstiga svar. Testa gärna och ge feedback!
									</p>
									<p style={{ marginTop: "0.7em" }}>
										<strong>Spara din chatt</strong><br />
										Varje chatt har en unik adress (URL) i webbläsaren. Kopiera den för att återvända till konversationen senare. Chattar sparas i 7 dagar från senaste aktivitet.
									</p>
									<p style={{ marginTop: "0.7em" }}>
										<strong>Dela konversationen</strong><br />
										Klicka på <em>Dela konversation</em> för att skapa en delningslänk. Den som öppnar länken får en egen kopia av konversationen och kan fortsätta den från där du slutade – utan att din ursprungliga chatt påverkas. Delningslänken upphör inte att gälla, men den kopierade chatten försvinner efter 7 dagars inaktivitet.
									</p>
									<p style={{ marginTop: "0.7em" }}>
										<strong>Källor och träffsäkerhet</strong><br />
										Svaren baseras på riksdagsdebatter och -dokument. Källorna visas under varje svar. Kontrollera alltid viktig information mot originalkällan.
									</p>
									<p style={{ marginTop: "0.7em" }}>
										Om något går fel, prova att starta en ny chatt eller formulera om din fråga.
									</p>
									<p style={{ marginTop: "1em", fontSize: "0.95em" }}>
										<strong>Om appen och dess begränsningar:</strong>{" "}
										<Link to="/guide">Läs guiden</Link>
										{" · "}
										<a href="/api/guide" target="_blank" rel="noopener noreferrer" onClick={handleCopyGuide}>
											{guideCopied ? "Öppnad och kopierad!" : "Öppna som råtext"}
										</a> (för att klistra in i en AI-chatt)
									</p>
								</div>
							)}
						</div>
						{/* --- End chat info panel --- */}
					</div>
				</form>
			)}
		</section>
	);
}

