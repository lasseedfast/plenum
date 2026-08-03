import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { deleteMyChat, fetchMyChats } from "../api";
import { decryptJson } from "../crypto";
import { useAuth } from "../context/AuthContext";
import type { EncTitlePayload, MyChatRow } from "../types";

type DecryptedRow = MyChatRow & { title: string; person_id?: string | null };

/** /chats — the logged-in user's saved conversations; titles decrypt locally. */
export default function MyChatsView() {
	const { user, dek } = useAuth();
	const navigate = useNavigate();
	const queryClient = useQueryClient();

	const chats = useQuery({
		queryKey: ["my-chats", user?.userId],
		enabled: Boolean(user && dek),
		queryFn: async (): Promise<DecryptedRow[]> => {
			const rows = await fetchMyChats();
			return Promise.all(
				rows.map(async (row) => {
					let title = "Konversation";
					let person_id: string | null | undefined;
					if (row.enc_title && dek) {
						try {
							const decoded = await decryptJson<EncTitlePayload>(dek, row.enc_title);
							title = decoded.title || title;
							person_id = decoded.person_id;
						} catch {
							title = "Kunde inte avkryptera";
						}
					}
					return { ...row, title, person_id };
				}),
			);
		},
	});

	const remove = useMutation({
		mutationFn: (id: string) => deleteMyChat(id),
		onSuccess: () => queryClient.invalidateQueries({ queryKey: ["my-chats"] }),
	});

	const chatUrl = (row: DecryptedRow) =>
		row.session_type === "mp" && row.person_id
			? `/mp/${row.person_id}?session=${row.id}`
			: `/chat/${row.id}`;

	if (!user) {
		return (
			<>
				<header className="page-header"><h1>Mina chattar</h1></header>
				<main className="content">
					<div className="empty-state panel">
						<h2>Inte inloggad</h2>
						<p>Logga in uppe till höger för att se dina sparade chattar. <Link to="/">Till sök</Link></p>
					</div>
				</main>
			</>
		);
	}

	return (
		<>
			<header className="page-header">
				<h1>Mina chattar</h1>
				<p className="tagline">
					Sparade konversationer — krypterade så att bara du kan läsa dem. <Link to="/">Tillbaka till sök</Link>
				</p>
			</header>

			<main className="content">
				{chats.isLoading && <div className="panel">Laddar…</div>}
				{chats.isError && <div className="panel error-banner">Kunde inte hämta chattarna.</div>}

				{chats.data && chats.data.length > 0 && (
					<div className="panel research-list">
						<table>
							<thead>
								<tr>
									<th>Konversation</th>
									<th>Typ</th>
									<th>Senast active</th>
									<th />
								</tr>
							</thead>
							<tbody>
								{chats.data.map((row) => (
									<tr key={row.id}>
										<td>
											<Link to={chatUrl(row)}>{row.title}</Link>
										</td>
										<td>{row.session_type === "mp" ? "Ledamot" : "Allmän"}</td>
										<td>{new Date(row.last_activity).toLocaleString("sv-SE")}</td>
										<td>
											<button
												type="button"
												className="secondary-button research-delete"
												onClick={() => {
													if (window.confirm(`Ta bort "${row.title}"?`)) remove.mutate(row.id);
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

				{chats.data && chats.data.length === 0 && (
					<div className="empty-state panel">
						<h2>Inga sparade chattar ännu</h2>
						<p>
							Chattar du startar medan du är inloggad sparas här automatiskt.{" "}
							<button type="button" className="secondary-button" onClick={() => navigate(`/chat/${crypto.randomUUID()}`)}>
								Starta en chatt
							</button>
						</p>
					</div>
				)}
			</main>
		</>
	);
}
