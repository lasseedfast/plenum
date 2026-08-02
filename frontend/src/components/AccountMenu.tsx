import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { useLLMSettings } from "../context/LLMSettingsContext";

/**
 * Fixed top-right controls: a gear that opens the AI settings (available to
 * everyone — the settings are browser-local until you have an account), plus
 * a login/signup modal when logged out or an account menu when logged in.
 * Rendered once in the App shell.
 */
export function AccountMenu() {
	const { ready, user, logout } = useAuth();
	const { openSettings } = useLLMSettings();
	const [modalOpen, setModalOpen] = useState(false);
	const [menuOpen, setMenuOpen] = useState(false);
	const menuRef = useRef<HTMLDivElement | null>(null);

	useEffect(() => {
		if (!menuOpen) return;
		const onClick = (e: MouseEvent) => {
			if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false);
		};
		document.addEventListener("mousedown", onClick);
		return () => document.removeEventListener("mousedown", onClick);
	}, [menuOpen]);

	if (!ready) return null;

	return (
		<div className="account-menu" ref={menuRef}>
			<button
				type="button"
				className="account-menu__chip account-menu__chip--icon"
				onClick={openSettings}
				aria-label="AI-inställningar"
				title="AI-inställningar"
			>
				<span aria-hidden="true">⚙</span>
			</button>
			{user ? (
				<>
					<button
						type="button"
						className="account-menu__chip"
						onClick={() => setMenuOpen((o) => !o)}
						aria-expanded={menuOpen}
					>
						<span className="account-menu__avatar">{user.username.slice(0, 1).toUpperCase()}</span>
						{user.username}
					</button>
					{menuOpen && (
						<div className="account-menu__dropdown panel">
							<Link to="/chats" onClick={() => setMenuOpen(false)}>Mina chattar</Link>
							<Link to="/research" onClick={() => setMenuOpen(false)}>Min research</Link>
							<button
								type="button"
								onClick={() => {
									setMenuOpen(false);
									openSettings();
								}}
							>
								AI-inställningar
							</button>
							<button
								type="button"
								onClick={() => {
									setMenuOpen(false);
									logout().catch(() => {});
								}}
							>
								Logga ut
							</button>
						</div>
					)}
				</>
			) : (
				<button type="button" className="account-menu__chip" onClick={() => setModalOpen(true)}>
					Logga in
				</button>
			)}
			{modalOpen && <LoginModal onClose={() => setModalOpen(false)} />}
		</div>
	);
}

function LoginModal({ onClose }: { onClose: () => void }) {
	const { login, signup } = useAuth();
	const [mode, setMode] = useState<"login" | "signup">("login");
	useEffect(() => {
		const onKey = (e: KeyboardEvent) => {
			if (e.key === "Escape") onClose();
		};
		document.addEventListener("keydown", onKey);
		return () => document.removeEventListener("keydown", onKey);
	}, [onClose]);
	const [username, setUsername] = useState("");
	const [password, setPassword] = useState("");
	const [password2, setPassword2] = useState("");
	const [busy, setBusy] = useState(false);
	const [error, setError] = useState<string | null>(null);

	const canSubmit =
		username.trim().length >= 3 &&
		password.length >= 8 &&
		(mode === "login" || password === password2);

	const submit = async () => {
		if (!canSubmit || busy) return;
		setBusy(true);
		setError(null);
		try {
			if (mode === "signup") await signup(username, password);
			else await login(username, password);
			onClose();
		} catch (err: any) {
			const detail = err?.response?.data?.detail;
			setError(
				typeof detail === "string"
					? detail
					: mode === "login"
						? "Inloggningen misslyckades."
						: "Kunde inte skapa kontot.",
			);
		} finally {
			setBusy(false);
		}
	};

	return (
		<div className="modal-backdrop" onClick={onClose}>
			<div
				className="modal auth-modal panel"
				role="dialog"
				aria-modal="true"
				aria-label={mode === "signup" ? "Skapa konto" : "Logga in"}
				onClick={(e) => e.stopPropagation()}
			>
				<div className="auth-modal__tabs" role="tablist">
					<button type="button" role="tab" data-active={mode === "login"} onClick={() => { setMode("login"); setError(null); }}>
						Logga in
					</button>
					<button type="button" role="tab" data-active={mode === "signup"} onClick={() => { setMode("signup"); setError(null); }}>
						Skapa konto
					</button>
				</div>

				<form
					onSubmit={(e) => {
						e.preventDefault();
						submit();
					}}
				>
					<label>
						Användarnamn
						<input
							type="text"
							value={username}
							autoComplete="username"
							autoFocus
							onChange={(e) => setUsername(e.target.value)}
						/>
					</label>
					<label>
						Lösenord {mode === "signup" && <span className="auth-modal__hint">(minst 8 tecken)</span>}
						<input
							type="password"
							value={password}
							autoComplete={mode === "signup" ? "new-password" : "current-password"}
							onChange={(e) => setPassword(e.target.value)}
						/>
					</label>
					{mode === "signup" && (
						<>
							<label>
								Upprepa lösenord
								<input
									type="password"
									value={password2}
									autoComplete="new-password"
									onChange={(e) => setPassword2(e.target.value)}
								/>
							</label>
							<p className="auth-modal__warning">
								Dina chattar och din research krypteras med en nyckel härledd ur lösenordet —
								inte ens den som driver sajten kan läsa dem. Baksidan: <strong>glömmer du
								lösenordet går historiken inte att återställa.</strong> Inget lösenord skickas
								eller sparas på servern.
							</p>
						</>
					)}
					{error && <div className="error-banner">{error}</div>}
					<div className="modal__actions">
						<button type="button" className="secondary-button" onClick={onClose} disabled={busy}>
							Avbryt
						</button>
						<button type="submit" className="primary" disabled={!canSubmit || busy}>
							{busy ? "Vänta…" : mode === "signup" ? "Skapa konto" : "Logga in"}
						</button>
					</div>
				</form>
			</div>
		</div>
	);
}
