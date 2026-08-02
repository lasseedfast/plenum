import { useEffect, useRef } from "react";
import { useLLMSettings } from "../context/LLMSettingsContext";
import { LLMSettingsPanel } from "./LLMSettingsPanel";

/**
 * The single settings surface, opened from the account menu. Reuses the auth
 * modal's shell so login and settings look like the same thing.
 */
export function LLMSettingsModal({ onClose }: { onClose: () => void }) {
	const { loading, scope, providerOverride, clearSettings } = useLLMSettings();
	const dialogRef = useRef<HTMLDivElement | null>(null);

	useEffect(() => {
		const onKey = (e: KeyboardEvent) => {
			if (e.key === "Escape") onClose();
		};
		document.addEventListener("keydown", onKey);
		dialogRef.current?.focus();
		return () => document.removeEventListener("keydown", onKey);
	}, [onClose]);

	return (
		<div className="modal-backdrop" onClick={onClose}>
			<div
				ref={dialogRef}
				className="modal llm-modal panel"
				role="dialog"
				aria-modal="true"
				aria-labelledby="llm-modal-title"
				tabIndex={-1}
				onClick={(e) => e.stopPropagation()}
			>
				<h2 id="llm-modal-title" className="llm-modal__title">AI-inställningar</h2>

				{loading ? (
					<p className="llm-settings-panel__hint">Hämtar dina sparade inställningar…</p>
				) : (
					<LLMSettingsPanel />
				)}

				<div className="modal__actions">
					{providerOverride && (
						<button
							type="button"
							className="secondary-button"
							onClick={() => {
								if (
									window.confirm(
										scope === "account"
											? "Ta bort din API-nyckel och modellval från kontot?"
											: "Ta bort din API-nyckel och modellval från den här webbläsaren?",
									)
								) {
									clearSettings();
								}
							}}
						>
							Glöm min nyckel
						</button>
					)}
					<button type="button" className="primary" onClick={onClose}>
						Klar
					</button>
				</div>
			</div>
		</div>
	);
}
