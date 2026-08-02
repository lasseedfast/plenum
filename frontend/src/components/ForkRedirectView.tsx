import { useEffect } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { forkSnapshot, getSnapshot, upsertSession } from "../api";
import { encryptJson } from "../crypto";
import { useAuth } from "../context/AuthContext";
import type { ChatTurn, EncSessionPayload, EncTitlePayload } from "../types";

export function ForkRedirectView() {
	const { uuid } = useParams<{ uuid: string }>();
	const navigate = useNavigate();
	const { user, dek } = useAuth();

	useEffect(() => {
		if (!uuid) return;
		const storageKey = `fork_${uuid}`;
		const existing = localStorage.getItem(storageKey);
		if (existing) {
			// Old entries stored a bare session uuid; newer ones a full path.
			navigate(existing.startsWith("/") ? existing : `/chat/${existing}`, { replace: true });
			return;
		}

		const run = async () => {
			if (user && dek) {
				// Logged in: fork locally so the copy is encrypted from the start —
				// the server never stores a plaintext session for this account.
				const snap = await getSnapshot(uuid);
				if (!snap) throw new Error("snapshot not found");
				const sessionId = crypto.randomUUID();
				const payload: EncSessionPayload = {
					llm_messages: snap.llm_messages ?? [],
					turns: (snap.turns ?? []) as unknown as ChatTurn[],
					focus_ids: snap.focus_ids ?? [],
					intressent_id: snap.intressent_id ?? null,
					initial_talk_id: snap.initial_talk_id ?? null,
				};
				const titlePayload: EncTitlePayload = {
					title: (snap.turns?.[0]?.question ?? "Delad konversation").slice(0, 80),
					intressent_id: snap.intressent_id ?? null,
				};
				const [enc_payload, enc_title] = await Promise.all([
					encryptJson(dek, payload),
					encryptJson(dek, titlePayload),
				]);
				await upsertSession(sessionId, { session_type: snap.session_type, enc_payload, enc_title });
				const path =
					snap.session_type === "mp" && snap.intressent_id
						? `/mp/${snap.intressent_id}?session=${sessionId}`
						: `/chat/${sessionId}`;
				localStorage.setItem(storageKey, path);
				navigate(path, { replace: true });
			} else {
				const sessionId = await forkSnapshot(uuid);
				localStorage.setItem(storageKey, sessionId);
				navigate(`/chat/${sessionId}`, { replace: true });
			}
		};

		run().catch(() => navigate("/", { replace: true }));
	}, [uuid, navigate, user, dek]);

	return (
		<div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100vh" }}>
			<p style={{ color: "var(--color-text-muted, #888)" }}>Öppnar konversation…</p>
		</div>
	);
}
