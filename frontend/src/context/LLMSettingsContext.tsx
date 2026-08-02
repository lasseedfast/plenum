import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { deleteMySettings, getMySettings, putMySettings } from "../api";
import { decryptJson, encryptJson } from "../crypto";
import type { ProviderOverride } from "../types";
import { LLMSettingsModal } from "../components/LLMSettingsModal";
import { useAuth } from "./AuthContext";

/**
 * One source of truth for the user's AI settings — provider, API key, models —
 * shared by chat, MP-chat and research.
 *
 * Two storage scopes:
 *  - guest: localStorage only, under the keys this used to live at inside
 *    LLMSettingsPanel, so an existing config survives the move.
 *  - logged in: encrypted under the account DEK and stored server-side, so the
 *    key follows the user between devices without the server being able to read
 *    it. localStorage is never written in this scope — an account key must not
 *    outlive a logout.
 */

const STORAGE_KEY = "riksdagen-llm-provider";
const EDITOR_FLAG_KEY = "riksdagen-use-editor";
const SAVE_DEBOUNCE_MS = 600;

export type ProviderSlot = {
	api_key: string;
	smart_model: string;
	fast_model: string;
	editor_model: string;
};

export type LLMSettings = {
	active_provider: string;
	providers: Record<string, ProviderSlot>;
	use_editor: boolean;
};

const EMPTY: LLMSettings = { active_provider: "", providers: {}, use_editor: false };

export function emptySlot(): ProviderSlot {
	return { api_key: "", smart_model: "", fast_model: "", editor_model: "" };
}

/** Fill missing fields so older stored payloads keep working. */
export function slotFor(settings: LLMSettings, providerId: string): ProviderSlot {
	const raw = settings.providers[providerId];
	return {
		api_key: raw?.api_key ?? "",
		smart_model: raw?.smart_model ?? "",
		fast_model: raw?.fast_model ?? "",
		editor_model: raw?.editor_model ?? "",
	};
}

function normalize(parsed: any, useEditor: boolean): LLMSettings {
	if (parsed?.providers && typeof parsed.providers === "object") {
		return {
			active_provider: parsed.active_provider ?? "",
			providers: parsed.providers,
			use_editor: typeof parsed.use_editor === "boolean" ? parsed.use_editor : useEditor,
		};
	}
	// Migrate the original flat format ({provider_id, api_key, …}).
	if (parsed?.provider_id && parsed.provider_id !== "vllm") {
		return {
			active_provider: parsed.provider_id,
			providers: {
				[parsed.provider_id]: {
					api_key: parsed.api_key ?? "",
					smart_model: parsed.smart_model ?? "",
					fast_model: parsed.fast_model ?? "",
					editor_model: parsed.editor_model ?? "",
				},
			},
			use_editor: useEditor,
		};
	}
	return { ...EMPTY, use_editor: useEditor };
}

function readLocal(): LLMSettings {
	try {
		const useEditor = localStorage.getItem(EDITOR_FLAG_KEY) === "1";
		const raw = localStorage.getItem(STORAGE_KEY);
		return normalize(raw ? JSON.parse(raw) : null, useEditor);
	} catch {
		return EMPTY;
	}
}

function writeLocal(settings: LLMSettings) {
	try {
		const { use_editor, ...rest } = settings;
		localStorage.setItem(STORAGE_KEY, JSON.stringify(rest));
		localStorage.setItem(EDITOR_FLAG_KEY, use_editor ? "1" : "0");
	} catch {
		/* private mode / quota — settings just won't persist */
	}
}

function clearLocal() {
	try {
		localStorage.removeItem(STORAGE_KEY);
		localStorage.removeItem(EDITOR_FLAG_KEY);
	} catch {}
}

/** Has the user configured enough for the backend to act on? */
function toOverride(settings: LLMSettings): ProviderOverride | undefined {
	if (!settings.active_provider) return undefined;
	const slot = slotFor(settings, settings.active_provider);
	if (!slot.api_key.trim() || !slot.smart_model) return undefined;
	return {
		provider_id: settings.active_provider,
		api_key: slot.api_key.trim(),
		smart_model: slot.smart_model,
		fast_model: slot.fast_model || slot.smart_model,
		// Empty string → backend falls back to smart_model.
		editor_model: slot.editor_model || "",
	};
}

type LLMSettingsContextValue = {
	settings: LLMSettings;
	providerOverride: ProviderOverride | undefined;
	useEditor: boolean;
	/** true while the account blob is still being fetched/decrypted */
	loading: boolean;
	/** where this browser is currently storing the settings */
	scope: "local" | "account";
	syncError: string | null;
	setActiveProvider: (id: string) => void;
	updateSlot: (id: string, patch: Partial<ProviderSlot>) => void;
	setUseEditor: (v: boolean) => void;
	clearSettings: () => Promise<void>;
	openSettings: () => void;
};

const LLMSettingsContext = createContext<LLMSettingsContextValue | null>(null);

export function LLMSettingsProvider({ children }: { children: ReactNode }) {
	const { user, dek } = useAuth();
	const scope: "local" | "account" = user && dek ? "account" : "local";

	const [settings, setSettings] = useState<LLMSettings>(() => readLocal());
	const [loading, setLoading] = useState(scope === "account");
	const [syncError, setSyncError] = useState<string | null>(null);
	const [open, setOpen] = useState(false);

	// Nothing may be written back before the stored copy has been read, or the
	// first render would push the empty default over the user's saved config.
	const loaded = useRef(scope === "local");
	const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

	// Load: account blob when logged in, localStorage otherwise. Re-runs on
	// login and logout, which is exactly when the scope changes.
	useEffect(() => {
		let cancelled = false;
		if (!(user && dek)) {
			loaded.current = true;
			setLoading(false);
			setSyncError(null);
			setSettings(readLocal());
			return;
		}
		loaded.current = false;
		setLoading(true);
		(async () => {
			try {
				const { enc_settings } = await getMySettings();
				if (cancelled) return;
				if (enc_settings) {
					setSettings(normalize(await decryptJson<any>(dek, enc_settings), false));
				} else {
					// First login on a device that already had guest settings:
					// adopt them once, then let the account copy take over.
					const local = readLocal();
					setSettings(local);
					if (local.active_provider) {
						await putMySettings(await encryptJson(dek, local));
						clearLocal();
					}
				}
			} catch (err: any) {
				if (!cancelled) {
					setSyncError(
						err?.response?.status === 401
							? "Kunde inte hämta dina AI-inställningar — logga in igen."
							: "Kunde inte hämta dina sparade AI-inställningar.",
					);
				}
			} finally {
				if (!cancelled) {
					loaded.current = true;
					setLoading(false);
				}
			}
		})();
		return () => {
			cancelled = true;
		};
	}, [user?.userId, dek]);

	// Save: debounced, so typing an API key doesn't PUT on every keystroke.
	useEffect(() => {
		if (!loaded.current) return;
		if (!(user && dek)) {
			writeLocal(settings);
			return;
		}
		if (saveTimer.current) clearTimeout(saveTimer.current);
		saveTimer.current = setTimeout(() => {
			(async () => {
				try {
					await putMySettings(await encryptJson(dek, settings));
					setSyncError(null);
				} catch {
					setSyncError("Kunde inte spara AI-inställningarna till kontot.");
				}
			})();
		}, SAVE_DEBOUNCE_MS);
		return () => {
			if (saveTimer.current) clearTimeout(saveTimer.current);
		};
	}, [settings, user?.userId, dek]);

	const setActiveProvider = useCallback((id: string) => {
		setSettings((s) => ({ ...s, active_provider: id }));
	}, []);

	const updateSlot = useCallback((id: string, patch: Partial<ProviderSlot>) => {
		setSettings((s) => ({
			...s,
			active_provider: id,
			providers: { ...s.providers, [id]: { ...slotFor(s, id), ...patch } },
		}));
	}, []);

	const setUseEditor = useCallback((v: boolean) => {
		setSettings((s) => ({ ...s, use_editor: v }));
	}, []);

	const clearSettings = useCallback(async () => {
		setSettings(EMPTY);
		clearLocal();
		if (user && dek) {
			try {
				await deleteMySettings();
			} catch {
				setSyncError("Kunde inte rensa inställningarna på servern.");
			}
		}
	}, [user?.userId, dek]);

	const openSettings = useCallback(() => setOpen(true), []);

	const value = useMemo<LLMSettingsContextValue>(
		() => ({
			settings,
			providerOverride: toOverride(settings),
			useEditor: settings.use_editor,
			loading,
			scope,
			syncError,
			setActiveProvider,
			updateSlot,
			setUseEditor,
			clearSettings,
			openSettings,
		}),
		[settings, loading, scope, syncError, setActiveProvider, updateSlot, setUseEditor,
			clearSettings, openSettings],
	);

	return (
		<LLMSettingsContext.Provider value={value}>
			{children}
			{open && <LLMSettingsModal onClose={() => setOpen(false)} />}
		</LLMSettingsContext.Provider>
	);
}

export function useLLMSettings(): LLMSettingsContextValue {
	const ctx = useContext(LLMSettingsContext);
	if (!ctx) throw new Error("useLLMSettings must be used inside LLMSettingsProvider");
	return ctx;
}
