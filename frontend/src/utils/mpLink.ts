/**
 * Readable URLs for member profiles.
 *
 * A profile is identified by an opaque id from the source data — a long number,
 * occasionally a UUID. `/mp/0218878014918` is a link nobody can read, check or
 * remember, which matters because these are the links people paste to each
 * other. Prefixing the name gives `/mp/stefan-lofven-0218878014918`, which says
 * who it points at while staying unique across the members who share a name.
 *
 * The id stays the authority. The slug is decoration: it is never parsed back
 * into a lookup, so a renamed or mistyped slug still resolves.
 */

/** Characters that do not decompose to ASCII under NFD and need spelling out. */
const TRANSLITERATIONS: Record<string, string> = {
	æ: "ae",
	ø: "o",
	œ: "oe",
	ß: "ss",
	đ: "d",
	ð: "d",
	þ: "th",
	ł: "l",
};

/**
 * "Stefan Löfven" → "stefan-lofven".
 *
 * NFD splits an accented letter into its base plus a combining mark, so
 * stripping the marks turns å/ä/ö/é into a/a/o/e. Letters that have no such
 * decomposition are mapped by hand above.
 */
export function slugifyName(name: string): string {
	return name
		.toLowerCase()
		.replace(/[æøœßđðþł]/g, (char) => TRANSLITERATIONS[char] ?? char)
		.normalize("NFD")
		.replace(/[\u0300-\u036f]/g, "")
		.replace(/[^a-z0-9]+/g, "-")
		.replace(/^-+|-+$/g, "");
}

/**
 * The trailing id in a slugged path segment: a run of digits, or a UUID.
 *
 * Anchored at a word start so it cannot bite into the name, and matched against
 * both id shapes because the imported data contains a handful of UUIDs whose
 * own hyphens would defeat a naive "split on the last hyphen".
 */
const ID_SUFFIX =
	/(?:^|-)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\d{4,})$/i;

/** Path to a member's profile, named when we know the name. */
export function mpPath(personId: string, name?: string | null): string {
	const slug = name ? slugifyName(name) : "";
	return slug ? `/mp/${slug}-${personId}` : `/mp/${personId}`;
}

/**
 * Recover the person id from a `/mp/:id` route param.
 *
 * Accepts both the slugged form and the bare id, so links shared before slugs
 * existed keep working.
 */
export function parseMpParam(param: string | undefined): string {
	if (!param) return "";
	const decoded = decodeURIComponent(param);
	return decoded.match(ID_SUFFIX)?.[1] ?? decoded;
}

/** True when `param` is already the canonical path segment for this person. */
export function isCanonicalMpParam(param: string | undefined, personId: string, name?: string | null): boolean {
	return !!param && decodeURIComponent(param) === mpPath(personId, name).slice("/mp/".length);
}
