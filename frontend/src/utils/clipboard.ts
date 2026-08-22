/**
 * Clipboard writes, with fallbacks for browsers (or non-secure contexts) where
 * navigator.clipboard is unavailable.
 */

/** Plain text only — used for share links. */
export async function copyToClipboard(text: string): Promise<void> {
	if (navigator.clipboard?.writeText) {
		try {
			await navigator.clipboard.writeText(text);
			return;
		} catch {
			// Present but refusing — no permission, the document lost focus, or
			// the user gesture has already expired. The legacy path below is
			// held to a looser standard, so it is still worth a try.
		}
	}
	writeViaCopyEvent(null, text);
}

/**
 * Copies a string that does not exist yet at click time — a share link that a
 * request has to mint first.
 *
 * Awaiting that request and then writing is the obvious shape and it fails on
 * Safari: WebKit only honours a clipboard write while the gesture that asked
 * for it is still live, and a network round trip outlives that window, so
 * writeText() rejects with NotAllowedError however fast the server answers.
 * Handing ClipboardItem the *pending promise* claims the clipboard
 * synchronously, inside the gesture, and fills it in once the link lands.
 *
 * Must therefore be called synchronously from the event handler — an `await`
 * before this point puts the gesture back out of reach.
 */
export async function copyToClipboardWhenReady(pending: Promise<string>): Promise<void> {
	if (navigator.clipboard?.write && typeof ClipboardItem !== "undefined") {
		try {
			await navigator.clipboard.write([
				new ClipboardItem({
					"text/plain": pending.then(
						text => new Blob([text], { type: "text/plain" }),
					),
				}),
			]);
			return;
		} catch {
			// Firefox takes only a resolved Blob here, not a promise. Falling
			// through also covers `pending` itself rejecting: the await below
			// re-raises that, so a failed request reads as a failed request
			// rather than as a clipboard problem.
		}
	}
	await copyToClipboard(await pending);
}

/**
 * Puts the same content on the clipboard twice, as HTML and as plain text, so
 * the paste target decides which it wants: Notes/Word/Google Docs take the
 * text/html flavour and paste formatted text with live hyperlinks, while
 * markdown editors and terminals take text/plain and get the markdown source.
 */
export async function copyRichText(html: string, plain: string): Promise<void> {
	if (navigator.clipboard?.write && typeof ClipboardItem !== "undefined") {
		try {
			await navigator.clipboard.write([
				new ClipboardItem({
					"text/html": new Blob([html], { type: "text/html" }),
					"text/plain": new Blob([plain], { type: "text/plain" }),
				}),
			]);
			return;
		} catch {
			// Firefox before 127 has clipboard.write() but rejects text/html,
			// and Safari rejects it outside a tight user-gesture window.
		}
	}
	if (writeViaCopyEvent(html, plain)) return;
	await copyToClipboard(plain);
}

/**
 * Legacy path: hijack the `copy` event so both flavours can be set by hand.
 * execCommand("copy") only fires that event when something is selected, hence
 * the throwaway textarea.
 */
function writeViaCopyEvent(html: string | null, plain: string): boolean {
	let wrote = false;
	const onCopy = (e: ClipboardEvent) => {
		if (!e.clipboardData) return;
		if (html !== null) e.clipboardData.setData("text/html", html);
		e.clipboardData.setData("text/plain", plain);
		e.preventDefault();
		wrote = true;
	};
	const holder = document.createElement("textarea");
	holder.value = plain;
	holder.setAttribute("aria-hidden", "true");
	holder.style.position = "fixed";
	holder.style.top = "0";
	holder.style.opacity = "0";
	document.addEventListener("copy", onCopy);
	document.body.appendChild(holder);
	holder.select();
	try {
		document.execCommand("copy");
	} finally {
		document.body.removeChild(holder);
		document.removeEventListener("copy", onCopy);
	}
	return wrote;
}
