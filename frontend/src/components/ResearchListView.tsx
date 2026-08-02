import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ModeToggle, useModeNavigation } from "./ModeToggle";
import { useLLMSettings } from "../context/LLMSettingsContext";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createResearch, deleteResearch, listResearch } from "../api";
import { generateBoardKey, wrapBoardKey } from "../crypto";
import { useAuth } from "../context/AuthContext";
import { decryptBoardSummary } from "../utils/researchCrypto";
import type { ResearchBoardSummary } from "../types";

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

export default function ResearchListView() {
	const navigate = useNavigate();
	const queryClient = useQueryClient();
	const [topic, setTopic] = useState("");
	const { user, dek, getBoardKey, rememberBoardKey } = useAuth();
	const { providerOverride } = useLLMSettings();
	const goToMode = useModeNavigation();

	const boards = useQuery({
		queryKey: ["research-list", user?.userId ?? "anon"],
		queryFn: async (): Promise<ResearchBoardSummary[]> => {
			const rows = await listResearch();
			if (!dek) return rows;
			// Encrypted boards: unwrap each board key and decrypt title/topic locally.
			return Promise.all(
				rows.map(async (row) => {
					if (!row.enc || !row.wrapped_board_key) return row;
					try {
						const bk = await getBoardKey(row.id, row.wrapped_board_key);
						return await decryptBoardSummary(row, bk.key);
					} catch {
						return { ...row, title: "🔒 Krypterad utforskning", topic: "" };
					}
				}),
			);
		},
		refetchInterval: (query) =>
			(query.state.data ?? []).some((b: ResearchBoardSummary) => BUSY_STATUSES.has(b.status)) ? 5000 : false,
	});

	const create = useMutation({
		mutationFn: async (t: string) => {
			if (user && dek) {
				// Logged in: mint a per-board key. The raw key rides along so the
				// server/job can encrypt writes; only the wrapped copy is stored.
				const bk = await generateBoardKey();
				const wrapped = await wrapBoardKey(dek, bk);
				const res = await createResearch({
					topic: t,
					board_key: bk.rawB64,
					wrapped_board_key: wrapped,
					llm: providerOverride,
				});
				rememberBoardKey(res.board_id, bk);
				return res;
			}
			return createResearch({ topic: t, llm: providerOverride });
		},
		onSuccess: ({ board_id }) => navigate(`/research/${board_id}`),
	});

	const remove = useMutation({
		mutationFn: (id: string) => deleteResearch(id),
		onSuccess: () => queryClient.invalidateQueries({ queryKey: ["research-list"] }),
	});

	const canSubmit = topic.trim().length >= 3 && !create.isPending;

	const submit = (e: React.FormEvent) => {
		e.preventDefault();
		if (canSubmit) create.mutate(topic.trim());
	};

	// Enter submits, shift+Enter adds a line — same as the chat composer.
	// Routed through the form so there is one submission path, guard included.
	const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
		if (e.key === "Enter" && !e.shiftKey && !e.metaKey && !e.ctrlKey) {
			e.preventDefault();
			e.currentTarget.form?.requestSubmit();
		}
	};

	return (
		<>
			<header className="page-header">
				<h1>Research</h1>
				<p className="tagline">
					Ange ett ämne så gräver systemet fram trådar, fynd och citat ur riksdagens debatter —
					i bakgrunden, även om du stänger fliken.
				</p>
			</header>

			<main className="content">
				<section className="panel research-create">
					<header className="panel-header">
						<ModeToggle mode="research" onModeChange={goToMode} />
					</header>
					<form onSubmit={submit} aria-busy={create.isPending}>
						<label className="field field--query">
							<div className="search-bar">
								<textarea
									value={topic}
									onChange={(e) => setTopic(e.target.value)}
									onKeyDown={handleKeyDown}
									placeholder="T.ex. Hur har kärnkraftsdebatten utvecklats 2010–2024? Vilka partier bytte fot, och varför?"
									rows={3}
								/>
								<button type="submit" className="primary search-button" disabled={!canSubmit}>
									{create.isPending ? (
										<>
											<span className="button-spinner" aria-hidden="true" />
											<span>Startar…</span>
										</>
									) : (
										"Starta research"
									)}
								</button>
							</div>
						</label>
					</form>
					{create.isError && (
						<div className="error-banner">
							{(create.error as any)?.response?.data?.detail ?? "Kunde inte starta research."}
						</div>
					)}
				</section>

				{boards.isError && (
					<div className="error-banner">Kunde inte hämta dina utforskningar.</div>
				)}

				{boards.data && boards.data.length > 0 && (
					<div className="panel research-list">
						<h2>Sparade utforskningar</h2>
						<table>
							<thead>
								<tr>
									<th>Ämne</th>
									<th>Status</th>
									<th>Trådar</th>
									<th>Uppdaterad</th>
									<th />
								</tr>
							</thead>
							<tbody>
								{boards.data.map((b) => (
									<tr key={b.id}>
										<td>
											<Link to={`/research/${b.id}`}>{b.title}</Link>
										</td>
										<td>
											<span className={`research-status research-status--${b.status}`}>
												{STATUS_LABELS[b.status] ?? b.status}
											</span>
										</td>
										<td>{b.thread_count}</td>
										<td>{new Date(b.updated_at).toLocaleString("sv-SE")}</td>
										<td>
											<button
												type="button"
												className="secondary-button research-delete"
												onClick={() => {
													if (window.confirm(`Ta bort "${b.title}"?`)) remove.mutate(b.id);
												}}
											>
												Ta bort
											</button>
										</td>
									</tr>
								))}
							</tbody>
						</table>
					</div>
				)}

				{boards.data && boards.data.length === 0 && (
					<div className="empty-state panel">
						<h2>Inga utforskningar ännu</h2>
						<p>Starta en ovan — resultatet sparas och går att fördjupa senare.</p>
					</div>
				)}
			</main>
		</>
	);
}
