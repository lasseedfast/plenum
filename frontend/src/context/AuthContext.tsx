import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
	AUTH_TOKEN_STORAGE_KEY,
	loginAccount,
	logoutAccount,
	preLogin,
	setAuthToken,
	signupAccount,
} from "../api";
import {
	deriveKeys,
	generateDekRaw,
	idbDeleteKey,
	idbGetKey,
	idbPutKey,
	importAesKey,
	KDF_ITERATIONS,
	randomBytes,
	toB64,
	unwrapBoardKey,
	unwrapDek,
	wrapDek,
	type BoardKey,
} from "../crypto";

const USER_STORAGE_KEY = "riksdagen-auth-user";
const DEK_IDB_KEY = "dek";

export type AuthUser = { userId: string; username: string };

type AuthContextValue = {
	/** false until the stored login (token + key) has been restored or rejected */
	ready: boolean;
	user: AuthUser | null;
	/** The data-encryption key — non-extractable, never leaves the browser. */
	dek: CryptoKey | null;
	signup: (username: string, password: string) => Promise<void>;
	login: (username: string, password: string) => Promise<void>;
	logout: () => Promise<void>;
	/** Unwrap (and cache) the per-board research key for an encrypted board. */
	getBoardKey: (boardId: string, wrappedBoardKey: string) => Promise<BoardKey>;
	/** Remember a freshly generated board key so the first poll needn't unwrap. */
	rememberBoardKey: (boardId: string, key: BoardKey) => void;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
	const [ready, setReady] = useState(false);
	const [user, setUser] = useState<AuthUser | null>(null);
	const [dek, setDek] = useState<CryptoKey | null>(null);
	const boardKeys = useRef<Map<string, BoardKey>>(new Map());

	// Restore a previous login: token + user from localStorage, DEK from
	// IndexedDB. If any piece is missing the login is unusable (we could not
	// decrypt anything), so drop it entirely.
	useEffect(() => {
		let cancelled = false;
		(async () => {
			try {
				const token = window.localStorage.getItem(AUTH_TOKEN_STORAGE_KEY);
				const storedUser = window.localStorage.getItem(USER_STORAGE_KEY);
				if (token && storedUser) {
					const parsed = JSON.parse(storedUser) as AuthUser;
					const storedDek = await idbGetKey(DEK_IDB_KEY);
					if (storedDek && !cancelled) {
						setAuthToken(token);
						setUser(parsed);
						setDek(storedDek);
					} else if (!cancelled) {
						setAuthToken(null);
						window.localStorage.removeItem(AUTH_TOKEN_STORAGE_KEY);
						window.localStorage.removeItem(USER_STORAGE_KEY);
					}
				}
			} catch {
				/* treat as logged out */
			} finally {
				if (!cancelled) setReady(true);
			}
		})();
		return () => {
			cancelled = true;
		};
	}, []);

	const persistLogin = useCallback(async (token: string, nextUser: AuthUser, nextDek: CryptoKey) => {
		setAuthToken(token);
		window.localStorage.setItem(AUTH_TOKEN_STORAGE_KEY, token);
		window.localStorage.setItem(USER_STORAGE_KEY, JSON.stringify(nextUser));
		await idbPutKey(DEK_IDB_KEY, nextDek);
		setUser(nextUser);
		setDek(nextDek);
	}, []);

	const signup = useCallback(async (username: string, password: string) => {
		const kdfSalt = toB64(randomBytes(16));
		const { authKey, kek } = await deriveKeys(password, kdfSalt, KDF_ITERATIONS);
		const dekRaw = generateDekRaw();
		const wrappedDek = await wrapDek(kek, dekRaw);
		const resp = await signupAccount({
			username: username.trim().toLowerCase(),
			auth_key: authKey,
			kdf_salt: kdfSalt,
			kdf_iterations: KDF_ITERATIONS,
			wrapped_dek: wrappedDek,
		});
		const nextDek = await importAesKey(dekRaw);
		await persistLogin(resp.token, { userId: resp.user_id, username: resp.username }, nextDek);
	}, [persistLogin]);

	const login = useCallback(async (username: string, password: string) => {
		const uname = username.trim().toLowerCase();
		const pre = await preLogin(uname);
		const { authKey, kek } = await deriveKeys(password, pre.kdf_salt, pre.kdf_iterations);
		const resp = await loginAccount({ username: uname, auth_key: authKey });
		const nextDek = await unwrapDek(kek, resp.wrapped_dek);
		await persistLogin(resp.token, { userId: resp.user_id, username: resp.username }, nextDek);
	}, [persistLogin]);

	const logout = useCallback(async () => {
		await logoutAccount();
		setAuthToken(null);
		window.localStorage.removeItem(AUTH_TOKEN_STORAGE_KEY);
		window.localStorage.removeItem(USER_STORAGE_KEY);
		await idbDeleteKey(DEK_IDB_KEY);
		boardKeys.current.clear();
		setUser(null);
		setDek(null);
	}, []);

	const getBoardKey = useCallback(async (boardId: string, wrappedBoardKey: string): Promise<BoardKey> => {
		const cached = boardKeys.current.get(boardId);
		if (cached) return cached;
		if (!dek) throw new Error("not logged in");
		const key = await unwrapBoardKey(dek, wrappedBoardKey);
		boardKeys.current.set(boardId, key);
		return key;
	}, [dek]);

	const rememberBoardKey = useCallback((boardId: string, key: BoardKey) => {
		boardKeys.current.set(boardId, key);
	}, []);

	return (
		<AuthContext.Provider value={{ ready, user, dek, signup, login, logout, getBoardKey, rememberBoardKey }}>
			{/* Hold rendering the few ms it takes to restore token+DEK, so no view
			    fires an API call that should have carried Authorization. */}
			{ready ? children : null}
		</AuthContext.Provider>
	);
}

export function useAuth(): AuthContextValue {
	const ctx = useContext(AuthContext);
	if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
	return ctx;
}
