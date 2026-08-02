import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { fetchGuide } from "../api";

export function GuideView() {
	const navigate = useNavigate();
	const [copied, setCopied] = useState(false);

	const { data: markdown, isLoading, error } = useQuery({
		queryKey: ["guide"],
		queryFn: fetchGuide,
	});

	const handleCopy = async () => {
		if (!markdown) return;
		try {
			if (navigator.clipboard) {
				await navigator.clipboard.writeText(markdown);
			} else {
				const el = document.createElement("textarea");
				el.value = markdown;
				el.style.position = "fixed";
				el.style.opacity = "0";
				document.body.appendChild(el);
				el.select();
				document.execCommand("copy");
				document.body.removeChild(el);
			}
			setCopied(true);
			setTimeout(() => setCopied(false), 2000);
		} catch (e) {
			console.error("Copy failed:", e);
		}
	};

	if (isLoading) {
		return (
			<div className="guide-view">
				<div className="panel"><p>Laddar guide...</p></div>
			</div>
		);
	}

	if (error || !markdown) {
		return (
			<div className="guide-view">
				<div className="panel error-banner">Kunde inte ladda guiden.</div>
			</div>
		);
	}

	const html = DOMPurify.sanitize(marked.parse(markdown) as string);

	return (
		<div className="guide-view">
			<header className="page-header">
				<button type="button" className="secondary-button" onClick={() => navigate(-1)}>
					← Tillbaka
				</button>
			</header>
			<main className="content">
				<div className="panel guide-view__content">
					<div className="guide-view__actions">
						<button
							type="button"
							className="secondary-button"
							onClick={handleCopy}
						>
							{copied ? "Kopierat!" : "Kopiera text"}
						</button>
						<a
							href="/api/guide"
							target="_blank"
							rel="noopener noreferrer"
							className="secondary-button"
						>
							Råtext (för AI-botar)
						</a>
					</div>
					<div
						className="guide-view__body"
						dangerouslySetInnerHTML={{ __html: html }}
					/>
				</div>
			</main>
		</div>
	);
}
