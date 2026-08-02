import { useEffect, useState } from "react";
import { slotFor, useLLMSettings } from "../context/LLMSettingsContext";

/**
 * The AI settings form. Pure UI over LLMSettingsContext — it owns no
 * persistence, so the same form serves guests (localStorage) and logged-in
 * users (encrypted blob on the account).
 */

type ProviderOption = {
    id: string;
    name: string;
    user_api_key: boolean;
};

export function LLMSettingsPanel() {
    const { settings, scope, syncError, providerOverride, setActiveProvider, updateSlot, setUseEditor } =
        useLLMSettings();

    const providerId = settings.active_provider;
    const slot = slotFor(settings, providerId);
    const { api_key: apiKey, smart_model: smartModel, fast_model: fastModel, editor_model: editorModel } = slot;
    const useEditor = settings.use_editor;

    const [separateFast, setSeparateFast] = useState(!!fastModel && fastModel !== smartModel);
    const [separateEditor, setSeparateEditor] = useState(!!editorModel && editorModel !== smartModel);

    const [providers, setProviders] = useState<ProviderOption[]>([]);
    const [providersLoaded, setProvidersLoaded] = useState(false);
    const [models, setModels] = useState<string[]>([]);
    const [fetchState, setFetchState] = useState<"idle" | "loading" | "done" | "error">("idle");
    const [fetchError, setFetchError] = useState("");

    // Fetch provider list from backend on mount.
    useEffect(() => {
        let cancelled = false;
        fetch("/api/providers")
            .then(r => r.json())
            .then(data => {
                if (cancelled) return;
                const list: ProviderOption[] = data.providers ?? [];
                setProviders(list);
                setProvidersLoaded(true);
            })
            .catch(() => {
                if (!cancelled) setProvidersLoaded(true);
            });
        return () => {
            cancelled = true;
        };
    }, []);

    // Pre-select the first provider once the list arrives and nothing is stored.
    // Kept out of the fetch callback so it reads the current providerId rather
    // than the one captured when the effect first ran.
    useEffect(() => {
        if (providersLoaded && !providerId && providers.length > 0) {
            setActiveProvider(providers[0].id);
        }
    }, [providersLoaded, providers, providerId, setActiveProvider]);

    const handleProviderChange = (id: string) => {
        setActiveProvider(id);
        setFetchState("idle");
        setModels([]);
        const next = slotFor(settings, id);
        setSeparateFast(!!next.fast_model && next.fast_model !== next.smart_model);
        setSeparateEditor(!!next.editor_model && next.editor_model !== next.smart_model);
    };

    const handleKeyChange = (e: React.ChangeEvent<HTMLInputElement>) => {
        setModels([]);
        setFetchState("idle");
        // A new key invalidates the model list it was fetched with.
        updateSlot(providerId, { api_key: e.target.value, smart_model: "", fast_model: "", editor_model: "" });
    };

    const handleKeyClear = () => {
        setModels([]);
        setFetchState("idle");
        updateSlot(providerId, { api_key: "", smart_model: "", fast_model: "", editor_model: "" });
    };

    const fetchModels = async () => {
        if (!apiKey.trim()) return;
        setFetchState("loading");
        setFetchError("");
        try {
            const resp = await fetch(`/api/providers/${providerId}/models`, {
                headers: { "X-Provider-Key": apiKey.trim() },
            });
            if (!resp.ok) {
                const body = await resp.json().catch(() => ({}));
                throw new Error(body.detail ?? `HTTP ${resp.status}`);
            }
            const data = await resp.json();
            const list: string[] = data.models ?? [];
            setModels(list);
            setFetchState("done");
            if (list.length === 1) {
                updateSlot(providerId, { smart_model: list[0], fast_model: list[0] });
            }
        } catch (e: unknown) {
            setFetchState("error");
            setFetchError(e instanceof Error ? e.message : String(e));
        }
    };

    const handleSmartModelChange = (m: string) => {
        updateSlot(providerId, separateFast ? { smart_model: m } : { smart_model: m, fast_model: m });
    };

    const handleSeparateFastToggle = (checked: boolean) => {
        setSeparateFast(checked);
        if (!checked) updateSlot(providerId, { fast_model: smartModel });
    };

    const handleSeparateEditorToggle = (checked: boolean) => {
        setSeparateEditor(checked);
        // Empty string means "fall back to smart model" on the backend.
        if (!checked) updateSlot(providerId, { editor_model: "" });
    };

    const isConfigured = providerOverride !== undefined;
    const selectedProvider = providers.find(p => p.id === providerId);
    const needsKey = !!selectedProvider?.user_api_key;

    return (
        <div className="llm-settings-panel">
            <p className="llm-settings-panel__intro">
                Välj vilket AI-API som ska driva chatten, ledamotschatten och din research.{" "}
                {scope === "account"
                    ? "Nyckeln sparas krypterad med ditt lösenord, så den följer med mellan dina enheter. Servern kan inte läsa den."
                    : "Din nyckel lagras bara lokalt i den här webbläsaren — den sparas aldrig på servern."}
            </p>
            <p className="llm-settings-panel__hint">
                När du startar en research skickas nyckeln med till bakgrundsjobbet och finns kvar
                i jobbets minne tills det är klart (som mest en timme). Den skrivs aldrig till databasen.
            </p>
            {syncError && <p className="llm-settings-panel__warning">{syncError}</p>}

            <fieldset className="llm-settings-panel__providers">
                <legend className="llm-settings-panel__legend">Provider</legend>
                {!providersLoaded && <p className="llm-settings-panel__hint">Laddar providers…</p>}
                {providers.map(p => (
                    <label key={p.id} className="llm-settings-panel__provider-option">
                        <input
                            type="radio"
                            name="llm-provider"
                            value={p.id}
                            checked={providerId === p.id}
                            onChange={() => handleProviderChange(p.id)}
                        />
                        <span>{p.name}</span>
                    </label>
                ))}
            </fieldset>

            {needsKey && (
                <>
                    <div className="llm-settings-panel__key-row">
                        <label className="llm-settings-panel__key-label" htmlFor="llm-api-key">
                            API-nyckel
                        </label>
                        <div className="llm-settings-panel__key-input-wrap">
                            <input
                                id="llm-api-key"
                                type="password"
                                autoComplete="off"
                                className="llm-settings-panel__key-input"
                                value={apiKey}
                                onChange={handleKeyChange}
                                placeholder="sk-…"
                            />
                            {apiKey && (
                                <button
                                    type="button"
                                    className="llm-settings-panel__key-clear"
                                    onClick={handleKeyClear}
                                    aria-label="Rensa nyckel"
                                >
                                    ✕
                                </button>
                            )}
                        </div>
                        <p className="llm-settings-panel__hint">Nyckeln skickas direkt till providern.</p>
                    </div>

                    <div className="llm-settings-panel__models-row">
                        <button
                            type="button"
                            className="secondary-button llm-settings-panel__fetch-btn"
                            onClick={fetchModels}
                            disabled={!apiKey.trim() || fetchState === "loading"}
                        >
                            {fetchState === "loading" ? "Hämtar…" : "Hämta modeller"}
                        </button>

                        {fetchState === "error" && (
                            <p className="llm-settings-panel__warning">
                                Kunde inte hämta modeller: {fetchError}
                            </p>
                        )}

                        {fetchState === "done" && models.length === 0 && (
                            <p className="llm-settings-panel__warning">Inga modeller hittades.</p>
                        )}

                        {models.length > 0 && (
                            <div className="llm-settings-panel__model-selectors">
                                <label className="llm-settings-panel__model-label" htmlFor="llm-smart-model">
                                    Modell {models.length > 1 ? "(smart / kommunikatör)" : ""}
                                </label>
                                <select
                                    id="llm-smart-model"
                                    className="llm-settings-panel__model-select"
                                    value={smartModel}
                                    onChange={e => handleSmartModelChange(e.target.value)}
                                    size={Math.min(models.length + 1, 6)}
                                >
                                    <option value="">— välj modell —</option>
                                    {models.map(m => (
                                        <option key={m} value={m}>{m}</option>
                                    ))}
                                </select>

                                {models.length > 1 && (
                                    <label className="llm-settings-panel__provider-option" style={{ marginTop: "0.4em" }}>
                                        <input
                                            type="checkbox"
                                            checked={separateFast}
                                            onChange={e => handleSeparateFastToggle(e.target.checked)}
                                        />
                                        <span>Separat modell för sammanfattning (snabbare/billigare)</span>
                                    </label>
                                )}

                                {separateFast && models.length > 1 && (
                                    <>
                                        <label className="llm-settings-panel__model-label" htmlFor="llm-fast-model">
                                            Sammanfattningsmodell
                                        </label>
                                        <select
                                            id="llm-fast-model"
                                            className="llm-settings-panel__model-select"
                                            value={fastModel}
                                            onChange={e => updateSlot(providerId, { fast_model: e.target.value })}
                                            size={Math.min(models.length + 1, 6)}
                                        >
                                            <option value="">— välj modell —</option>
                                            {models.map(m => (
                                                <option key={m} value={m}>{m}</option>
                                            ))}
                                        </select>
                                    </>
                                )}

                                {models.length > 1 && (
                                    <label className="llm-settings-panel__provider-option" style={{ marginTop: "0.4em" }}>
                                        <input
                                            type="checkbox"
                                            checked={separateEditor}
                                            onChange={e => handleSeparateEditorToggle(e.target.checked)}
                                        />
                                        <span>Separat modell för redaktörsgranskning (faktacheck + språk)</span>
                                    </label>
                                )}

                                {separateEditor && models.length > 1 && (
                                    <>
                                        <label className="llm-settings-panel__model-label" htmlFor="llm-editor-model">
                                            Redaktörsmodell
                                        </label>
                                        <select
                                            id="llm-editor-model"
                                            className="llm-settings-panel__model-select"
                                            value={editorModel}
                                            onChange={e => updateSlot(providerId, { editor_model: e.target.value })}
                                            size={Math.min(models.length + 1, 6)}
                                        >
                                            <option value="">— använd smart-modellen —</option>
                                            {models.map(m => (
                                                <option key={m} value={m}>{m}</option>
                                            ))}
                                        </select>
                                    </>
                                )}
                            </div>
                        )}
                    </div>

                    {!apiKey && (
                        <p className="llm-settings-panel__warning">
                            Ingen nyckel angiven — chatten och research faller tillbaka på standardmodellen.
                        </p>
                    )}
                    {apiKey && !smartModel && (
                        <p className="llm-settings-panel__warning">
                            Ingen modell vald — hämta och välj en modell ovan.
                        </p>
                    )}
                </>
            )}

            <fieldset className="llm-settings-panel__providers">
                <legend className="llm-settings-panel__legend">Redaktörsgranskning</legend>
                <label className="llm-settings-panel__provider-option">
                    <input
                        type="checkbox"
                        checked={useEditor}
                        onChange={e => setUseEditor(e.target.checked)}
                    />
                    <span>Kör en faktacheck + språkgranskning av svaret innan det visas</span>
                </label>
                <p className="llm-settings-panel__hint">
                    Gäller chatten. Använder redaktörsmodellen ovan (eller smart-modellen om ingen är vald).
                    Det tar längre tid att få ett svar, men det har då gått igenom en extra koll.
                </p>
            </fieldset>

            {isConfigured && (
                <p className="llm-settings-panel__saved">
                    Aktiv: {smartModel}
                    {separateFast && fastModel ? ` / ${fastModel}` : ""}
                    {separateEditor && editorModel ? ` / redaktör: ${editorModel}` : ""}
                    {useEditor ? " (redaktör på)" : ""}
                    {" "}✓
                </p>
            )}
        </div>
    );
}
