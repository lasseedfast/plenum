import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { slotFor, useLLMSettings } from "../context/LLMSettingsContext";
import type { ProviderSlot } from "../context/LLMSettingsContext";

/**
 * The AI settings form. Pure UI over LLMSettingsContext — it owns no
 * persistence, so the same form serves guests (localStorage) and logged-in
 * users (encrypted blob on the account).
 *
 * Laid out as numbered steps — provider, key, models — because the choice is
 * a sequence: you cannot list models before there is a key to list them with.
 */

type ProviderOption = {
    id: string;
    name: string;
    user_api_key: boolean;
};

/** What /api/providers/{id}/models reports per model. Everything but id and
 *  name is optional: only OpenRouter publishes prices and context sizes. */
type ModelInfo = {
    id: string;
    name: string;
    description?: string;
    context_length?: number | null;
    max_output_tokens?: number | null;
    /** USD per 1M tokens, or null when the provider doesn't publish it. */
    prompt_price?: number | null;
    completion_price?: number | null;
    reasoning?: boolean;
    input_modalities?: string[];
};

// ── Icons ────────────────────────────────────────────────────────────────────
// Small inline strokes rather than an icon dependency; sized by the caller's
// font-size so they line up with the text they label.

const svgProps = {
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.8,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
};

const IconCheck = ({ size = 12 }: { size?: number }) => (
    <svg width={size} height={size} {...svgProps}><path d="m5 13 4 4L19 7" /></svg>
);
const IconShield = ({ size = 12 }: { size?: number }) => (
    <svg width={size} height={size} {...svgProps}>
        <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z" /><path d="m9 12 2 2 4-4" />
    </svg>
);
const IconCloud = ({ size = 12 }: { size?: number }) => (
    <svg width={size} height={size} {...svgProps}>
        <path d="M17.5 19a4.5 4.5 0 0 0 0-9 6 6 0 0 0-11.6 2A3.5 3.5 0 0 0 6.5 19Z" />
    </svg>
);
const IconExternal = ({ size = 11 }: { size?: number }) => (
    <svg width={size} height={size} {...svgProps}>
        <path d="M15 3h6v6" /><path d="M10 14 21 3" />
        <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h6" />
    </svg>
);
const IconCpu = ({ size = 15 }: { size?: number }) => (
    <svg width={size} height={size} {...svgProps}>
        <rect x="4" y="4" width="16" height="16" rx="2" /><rect x="9" y="9" width="6" height="6" />
        <path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2" />
    </svg>
);
const IconZap = ({ size = 15 }: { size?: number }) => (
    <svg width={size} height={size} {...svgProps}><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8Z" /></svg>
);
const IconPen = ({ size = 15 }: { size?: number }) => (
    <svg width={size} height={size} {...svgProps}>
        <path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" />
    </svg>
);

// ── Provider metadata ────────────────────────────────────────────────────────
// The backend only knows id, name and whether the user brings a key. What a
// provider *is* — where the data goes, what it costs — lives here, keyed by the
// ids in providers.yaml. Unknown ids fall back to a neutral entry so a new
// provider still renders.

type ProviderMeta = {
    blurb: string;
    cost: string;
    privacy: string;
    group: "lokal" | "moln" | "annat";
    local?: boolean;
    keyDocs?: string;
};

const PROVIDER_META: Record<string, ProviderMeta> = {
    vllm: {
        group: "lokal",
        local: true,
        blurb: "Sajtens egen vLLM-instans. Ingen nyckel behövs och frågorna lämnar aldrig servern.",
        cost: "Ingår",
        privacy: "Lokalt",
    },
    openrouter: {
        group: "moln",
        blurb: "Ett konto, alla de stora modellerna — GPT, Claude, Gemini, DeepSeek — via samma nyckel. Här visas också vad varje modell kostar.",
        cost: "Per token",
        privacy: "Moln",
        keyDocs: "https://openrouter.ai/keys",
    },
    berget: {
        group: "moln",
        blurb: "Svensk värd för öppna modeller. Data stannar i Sverige.",
        cost: "Per token",
        privacy: "Moln (SE)",
        keyDocs: "https://berget.ai/dashboard",
    },
    openai: {
        group: "moln",
        blurb: "GPT-modellerna direkt från OpenAI, utan mellanhand.",
        cost: "Per token",
        privacy: "Moln",
        keyDocs: "https://platform.openai.com/api-keys",
    },
    googlegemini: {
        group: "moln",
        blurb: "Google Gemini via deras OpenAI-kompatibla endpoint.",
        cost: "Per token",
        privacy: "Moln",
        keyDocs: "https://aistudio.google.com/apikey",
    },
};

const FALLBACK_META: ProviderMeta = {
    group: "annat",
    blurb: "Egen OpenAI-kompatibel endpoint.",
    cost: "—",
    privacy: "Beror på",
};

const metaFor = (id: string): ProviderMeta => PROVIDER_META[id] ?? FALLBACK_META;

const GROUP_LABELS: { key: ProviderMeta["group"]; label: string }[] = [
    { key: "lokal", label: "Lokala" },
    { key: "moln", label: "Moln" },
    { key: "annat", label: "Annat" },
];

// ── Model roles ──────────────────────────────────────────────────────────────
// Data-driven so a fourth role is one entry, not another hand-written card.
// An empty value means "use the main model" — that is what the backend does
// with an empty fast_model/editor_model.

type ModelRole = {
    key: keyof Pick<ProviderSlot, "smart_model" | "fast_model" | "editor_model">;
    icon: ReactNode;
    title: string;
    blurb: string;
    emptyLabel: string;
};

const MODEL_ROLES: ModelRole[] = [
    {
        key: "smart_model",
        icon: <IconCpu />,
        title: "Huvudmodell",
        blurb: "Driver chatten, ledamotschatten och din research — det är den här som skriver svaren du läser. Kvaliteten här avgör hela svaret.",
        emptyLabel: "— välj modell —",
    },
    {
        key: "fast_model",
        icon: <IconZap />,
        title: "Sammanfattningsmodell",
        blurb: "Används för de snabba, mekaniska stegen: sammanfattningar och rubriker. Peka den mot något billigare — tom = huvudmodellen.",
        emptyLabel: "— använd huvudmodellen —",
    },
    {
        key: "editor_model",
        icon: <IconPen />,
        title: "Redaktörsmodell",
        blurb: "Används bara när redaktörsgranskningen nedan är på: en faktacheck av svaret mot källorna innan du ser det. Tom = huvudmodellen.",
        emptyLabel: "— använd huvudmodellen —",
    },
];

// ── Formatting helpers ───────────────────────────────────────────────────────

/** Tolerate the older endpoint shape (a plain list of model ids). */
function toModelInfo(raw: unknown): ModelInfo {
    if (typeof raw === "string") return { id: raw, name: raw };
    const m = raw as ModelInfo;
    return { ...m, name: m.name || m.id };
}

const hasPrice = (m: ModelInfo) => m.prompt_price != null && m.completion_price != null;
const isFree = (m: ModelInfo) => m.prompt_price === 0 && m.completion_price === 0;

/** USD, with just enough decimals to stay honest about cheap models. */
function money(v: number): string {
    if (v === 0) return "$0";
    if (v < 0.01) return `$${v.toFixed(4)}`;
    if (v < 1) return `$${v.toFixed(3).replace(/0+$/, "")}`;
    if (v < 100) return `$${v.toFixed(2).replace(/\.?0+$/, "")}`;
    return `$${Math.round(v)}`;
}

function tokens(n: number): string {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n % 1_000_000 === 0 ? 0 : 1)}M`;
    if (n >= 1000) return `${Math.round(n / 1000)}k`;
    return String(n);
}

/** What one average chat turn costs: ~10 000 tokens in, ~1 000 ut. */
const SAMPLE_IN = 10_000;
const SAMPLE_OUT = 1_000;

function sampleCost(m: ModelInfo): string | null {
    if (!hasPrice(m)) return null;
    const usd = (m.prompt_price! * SAMPLE_IN + m.completion_price! * SAMPLE_OUT) / 1_000_000;
    if (usd === 0) return "$0";
    return usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`;
}

/** The one-line summary shown inside the <select>. */
function modelLabel(m: ModelInfo): string {
    const bits: string[] = [];
    if (hasPrice(m)) {
        bits.push(isFree(m) ? "gratis" : `${money(m.prompt_price!)} / ${money(m.completion_price!)} per M`);
    }
    if (m.context_length) bits.push(`${tokens(m.context_length)} kontext`);
    return bits.length ? `${m.name} — ${bits.join(" · ")}` : m.name;
}

// ── Pieces ───────────────────────────────────────────────────────────────────

function Step({ num, title, help, children }: { num: number; title: string; help: ReactNode; children: ReactNode }) {
    return (
        <section className="llm-step">
            <header className="llm-step__head">
                <span className="llm-step__num">{num}</span>
                <h3 className="llm-step__title">{title}</h3>
            </header>
            <p className="llm-step__help">{help}</p>
            <div className="llm-step__body">{children}</div>
        </section>
    );
}

/** Facts about the model the user actually picked, under its select. */
function ModelFacts({ model }: { model: ModelInfo | undefined }) {
    if (!model) return null;
    const cost = sampleCost(model);
    return (
        <div className="llm-model-facts">
            <code className="llm-model-facts__id">{model.id}</code>
            <ul className="llm-model-facts__list">
                {hasPrice(model) ? (
                    <li>
                        {isFree(model)
                            ? "Gratis hos providern"
                            : `${money(model.prompt_price!)} per miljon tokens in · ${money(model.completion_price!)} per miljon tokens ut`}
                    </li>
                ) : (
                    <li className="llm-model-facts__unknown">Providern uppger inget pris.</li>
                )}
                {model.context_length ? (
                    <li>
                        Kontext {tokens(model.context_length)} tokens
                        {model.max_output_tokens ? ` · max svar ${tokens(model.max_output_tokens)}` : ""}
                    </li>
                ) : null}
                {model.reasoning ? <li>Kan resonera (thinking)</li> : null}
            </ul>
            {cost && (
                <p className="llm-model-facts__estimate">
                    En typisk fråga ({tokens(SAMPLE_IN)} tokens in, {tokens(SAMPLE_OUT)} ut) kostar ungefär {cost}.
                </p>
            )}
        </div>
    );
}

function ModelRoleCard({
    role,
    value,
    models,
    onChange,
}: {
    role: ModelRole;
    value: string;
    models: ModelInfo[];
    onChange: (modelId: string) => void;
}) {
    // The selected model must stay in the list even when the filter hides it,
    // or the <select> would silently show no selection at all.
    const options = useMemo(() => {
        if (!value || models.some(m => m.id === value)) return models;
        return [{ id: value, name: value } as ModelInfo, ...models];
    }, [models, value]);
    const selected = options.find(m => m.id === value);
    const selectId = `llm-role-${role.key}`;

    return (
        <div className="llm-role">
            <div className="llm-role__head">
                <span className="llm-role__icon">{role.icon}</span>
                <div>
                    <label className="llm-role__title" htmlFor={selectId}>{role.title}</label>
                    <p className="llm-role__blurb">{role.blurb}</p>
                </div>
            </div>
            <div className="llm-role__input">
                <select
                    id={selectId}
                    className="llm-role__select"
                    value={value}
                    onChange={e => onChange(e.target.value)}
                >
                    <option value="">{role.emptyLabel}</option>
                    {options.map(m => (
                        <option key={m.id} value={m.id} title={m.description || m.id}>
                            {modelLabel(m)}
                        </option>
                    ))}
                </select>
                <ModelFacts model={selected} />
            </div>
        </div>
    );
}

// ── The panel ────────────────────────────────────────────────────────────────

export function LLMSettingsPanel() {
    const { settings, scope, syncError, providerOverride, setActiveProvider, updateSlot, setUseEditor } =
        useLLMSettings();

    const providerId = settings.active_provider;
    const slot = slotFor(settings, providerId);
    const { api_key: apiKey, smart_model: smartModel, fast_model: fastModel, editor_model: editorModel } = slot;
    const useEditor = settings.use_editor;

    const [providers, setProviders] = useState<ProviderOption[]>([]);
    const [providersLoaded, setProvidersLoaded] = useState(false);
    const [models, setModels] = useState<ModelInfo[]>([]);
    const [fetchState, setFetchState] = useState<"idle" | "loading" | "done" | "error">("idle");
    const [fetchError, setFetchError] = useState("");
    const [filter, setFilter] = useState("");
    const [sortBy, setSortBy] = useState<"name" | "price">("name");

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

    const resetModels = () => {
        setModels([]);
        setFetchState("idle");
        setFilter("");
    };

    const handleProviderChange = (id: string) => {
        setActiveProvider(id);
        resetModels();
    };

    const handleKeyChange = (e: React.ChangeEvent<HTMLInputElement>) => {
        resetModels();
        // A new key invalidates the model list it was fetched with.
        updateSlot(providerId, { api_key: e.target.value, smart_model: "", fast_model: "", editor_model: "" });
    };

    const handleKeyClear = () => {
        resetModels();
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
            const list: ModelInfo[] = (data.models ?? []).map(toModelInfo);
            setModels(list);
            setFetchState("done");
            if (list.length === 1) updateSlot(providerId, { smart_model: list[0].id });
        } catch (e: unknown) {
            setFetchState("error");
            setFetchError(e instanceof Error ? e.message : String(e));
        }
    };

    // What the role cards list: the fetched models, filtered and sorted.
    const visibleModels = useMemo(() => {
        const q = filter.trim().toLowerCase();
        const list = q
            ? models.filter(m => m.id.toLowerCase().includes(q) || m.name.toLowerCase().includes(q))
            : models.slice();
        if (sortBy === "price") {
            // Models without a published price sort last — there is nothing to
            // compare them on, and guessing a number would be worse than none.
            list.sort((a, b) => {
                const pa = hasPrice(a) ? a.prompt_price! : Number.POSITIVE_INFINITY;
                const pb = hasPrice(b) ? b.prompt_price! : Number.POSITIVE_INFINITY;
                return pa - pb || a.id.localeCompare(b.id);
            });
        } else {
            list.sort((a, b) => a.name.localeCompare(b.name));
        }
        return list;
    }, [models, filter, sortBy]);

    const anyPrices = models.some(hasPrice);
    const isConfigured = providerOverride !== undefined;
    const selectedProvider = providers.find(p => p.id === providerId);
    const meta = metaFor(providerId);
    const needsKey = !!selectedProvider?.user_api_key;

    // Step numbers shift when the provider needs no key of its own.
    const stepKey = needsKey ? 2 : 0;
    const stepModels = needsKey ? 3 : 2;
    const stepEditor = stepModels + 1;

    const grouped = GROUP_LABELS.map(g => ({
        ...g,
        items: providers.filter(p => metaFor(p.id).group === g.key),
    })).filter(g => g.items.length > 0);

    return (
        <div className="llm-settings-panel">
            {syncError && <p className="llm-settings-panel__warning">{syncError}</p>}

            <Step
                num={1}
                title="Välj AI-leverantör"
                help={
                    <>
                        Var ska AI:n köra? Frågorna och nyckeln går direkt till leverantören —{" "}
                        {scope === "account"
                            ? "nyckeln sparas krypterad med ditt lösenord och följer med mellan dina enheter. Servern kan inte läsa den."
                            : "nyckeln lagras bara i den här webbläsaren och sparas aldrig på servern."}
                    </>
                }
            >
                {!providersLoaded && <p className="llm-settings-panel__hint">Laddar providers…</p>}
                <div className="llm-provider-tabs" role="radiogroup" aria-label="AI-leverantör">
                    {grouped.map(g => (
                        <div className="llm-provider-tabs__group" key={g.key}>
                            <span className="llm-provider-tabs__group-label">{g.label}</span>
                            {g.items.map(p => (
                                <button
                                    key={p.id}
                                    type="button"
                                    role="radio"
                                    aria-checked={providerId === p.id}
                                    className={`llm-ptab${providerId === p.id ? " llm-ptab--active" : ""}`}
                                    onClick={() => handleProviderChange(p.id)}
                                >
                                    <span>{p.name}</span>
                                    {providerId === p.id && (
                                        <span className="llm-ptab__check"><IconCheck /></span>
                                    )}
                                </button>
                            ))}
                        </div>
                    ))}
                </div>

                {selectedProvider && (
                    <div className="llm-provider-info">
                        <p className="llm-provider-info__blurb">{meta.blurb}</p>
                        <div className="llm-provider-info__pills">
                            <span className="llm-pill">{meta.cost}</span>
                            <span className={`llm-pill llm-pill--priv${meta.local ? " llm-pill--ok" : ""}`}>
                                {meta.local ? <IconShield /> : <IconCloud />}
                                {meta.privacy}
                            </span>
                            {meta.keyDocs && (
                                <a
                                    className="llm-pill-link"
                                    href={meta.keyDocs}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                >
                                    Skaffa nyckel <IconExternal />
                                </a>
                            )}
                        </div>
                    </div>
                )}
            </Step>

            {needsKey && (
                <Step
                    num={stepKey}
                    title={`API-nyckel för ${selectedProvider?.name}`}
                    help={
                        <>
                            Nyckeln skickas direkt till providern vid varje förfrågan. Startar du en research
                            följer den med till bakgrundsjobbet och ligger kvar i jobbets minne tills det är
                            klart (som mest en timme) — den skrivs aldrig till databasen i klartext.
                        </>
                    }
                >
                    <div className="llm-settings-panel__key-input-wrap">
                        <input
                            id="llm-api-key"
                            type="password"
                            autoComplete="off"
                            className="llm-settings-panel__key-input"
                            value={apiKey}
                            onChange={handleKeyChange}
                            placeholder="sk-…"
                            aria-label="API-nyckel"
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
                    {!apiKey && (
                        <p className="llm-settings-panel__warning">
                            Ingen nyckel angiven — chatten och research faller tillbaka på standardmodellen.
                        </p>
                    )}
                </Step>
            )}

            {needsKey && (
                <Step
                    num={stepModels}
                    title="Välj modeller"
                    help="Sajten använder olika modeller för olika steg. Bara huvudmodellen måste väljas — de andra faller tillbaka på den."
                >
                    <div className="llm-fetch-row">
                        <button
                            type="button"
                            className="secondary-button llm-settings-panel__fetch-btn"
                            onClick={fetchModels}
                            disabled={!apiKey.trim() || fetchState === "loading"}
                        >
                            {fetchState === "loading" ? "Hämtar…" : "Hämta modeller"}
                        </button>
                        {fetchState === "done" && models.length > 0 && (
                            <span className="llm-settings-panel__hint">{models.length} modeller hittades</span>
                        )}
                        {fetchState === "error" && (
                            <span className="llm-settings-panel__warning">
                                Kunde inte hämta modeller: {fetchError}
                            </span>
                        )}
                        {fetchState === "done" && models.length === 0 && (
                            <span className="llm-settings-panel__warning">Inga modeller hittades.</span>
                        )}
                    </div>

                    {models.length > 1 && (
                        <div className="llm-settings-panel__model-tools">
                            <input
                                type="search"
                                className="llm-settings-panel__model-filter"
                                value={filter}
                                onChange={e => setFilter(e.target.value)}
                                placeholder={`Sök bland ${models.length} modeller…`}
                                aria-label="Filtrera modeller"
                            />
                            {anyPrices && (
                                <select
                                    className="llm-settings-panel__model-sort"
                                    value={sortBy}
                                    onChange={e => setSortBy(e.target.value as "name" | "price")}
                                    aria-label="Sortera modeller"
                                >
                                    <option value="name">Sortera: namn</option>
                                    <option value="price">Sortera: billigast först</option>
                                </select>
                            )}
                        </div>
                    )}

                    {anyPrices && (
                        <p className="llm-settings-panel__hint">
                            Priserna kommer från providern och visas i USD per miljon tokens (in / ut). Du
                            betalar providern direkt; sajten tar inget påslag.
                        </p>
                    )}

                    {filter.trim() && visibleModels.length === 0 && (
                        <p className="llm-settings-panel__warning">Ingen modell matchar ”{filter.trim()}”.</p>
                    )}

                    {models.length > 0 && (
                        <div className="llm-roles">
                            {MODEL_ROLES.map(role => (
                                <ModelRoleCard
                                    key={role.key}
                                    role={role}
                                    value={slot[role.key]}
                                    models={visibleModels}
                                    onChange={m => updateSlot(providerId, { [role.key]: m })}
                                />
                            ))}
                        </div>
                    )}

                    {apiKey && models.length === 0 && fetchState !== "loading" && (
                        <p className="llm-settings-panel__hint">
                            Klicka «Hämta modeller» för att se providerns modeller med pris och kontextstorlek.
                        </p>
                    )}
                    {apiKey && !smartModel && models.length > 0 && (
                        <p className="llm-settings-panel__warning">
                            Ingen huvudmodell vald — chatten faller tillbaka på standardmodellen.
                        </p>
                    )}
                </Step>
            )}

            <Step
                num={needsKey ? stepEditor : 2}
                title="Redaktörsgranskning"
                help="En extra körning som faktakollar svaret mot källorna och putsar språket innan du ser det. Det tar längre tid, men svaret har då gått igenom en koll till."
            >
                <label className="llm-settings-panel__provider-option">
                    <input type="checkbox" checked={useEditor} onChange={e => setUseEditor(e.target.checked)} />
                    <span>Kör en faktacheck + språkgranskning av svaret innan det visas</span>
                </label>
                <p className="llm-settings-panel__hint">
                    Gäller chatten. Använder redaktörsmodellen ovan, eller huvudmodellen om ingen är vald.
                </p>
            </Step>

            {isConfigured && (
                <p className="llm-settings-panel__saved">
                    Aktiv: {smartModel}
                    {fastModel && fastModel !== smartModel ? ` / snabb: ${fastModel}` : ""}
                    {editorModel && editorModel !== smartModel ? ` / redaktör: ${editorModel}` : ""}
                    {useEditor ? " (redaktör på)" : ""}
                    {" "}✓
                </p>
            )}
        </div>
    );
}
