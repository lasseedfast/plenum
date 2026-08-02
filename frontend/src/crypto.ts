/**
 * Zero-knowledge crypto for optional login. WebCrypto only — no dependencies.
 *
 * The password never leaves the browser: PBKDF2 stretches it, HKDF splits the
 * result into an auth key (sent to the server, bcrypt-hashed there) and a KEK
 * (never sent) that wraps a random data-encryption key (DEK). All owned
 * content is AES-GCM ciphertext under the DEK (or a per-board key wrapped by
 * the DEK) before it leaves the browser.
 *
 * Blob format, shared with backend/services/crypto_blob.py:
 *   "v1:" + base64(iv[12] || ciphertext+tag)
 */

const te = new TextEncoder();
const td = new TextDecoder();

export const KDF_ITERATIONS = 600_000;
const BLOB_PREFIX = "v1:";

/* ── bytes/base64 ────────────────────────────────────────────────── */

export function toB64(data: Uint8Array | ArrayBuffer): string {
	const bytes = data instanceof Uint8Array ? data : new Uint8Array(data);
	let bin = "";
	for (const b of bytes) bin += String.fromCharCode(b);
	return btoa(bin);
}

export function fromB64(s: string): Uint8Array {
	const bin = atob(s);
	const bytes = new Uint8Array(bin.length);
	for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
	return bytes;
}

export function randomBytes(n: number): Uint8Array {
	const bytes = new Uint8Array(n);
	crypto.getRandomValues(bytes);
	return bytes;
}

/* ── key derivation (login/signup) ───────────────────────────────── */

export type DerivedKeys = {
	/** base64; the only password-derived value the server ever sees */
	authKey: string;
	/** wraps/unwraps the DEK; never leaves the browser */
	kek: CryptoKey;
};

export async function deriveKeys(
	password: string,
	kdfSaltB64: string,
	iterations: number = KDF_ITERATIONS,
): Promise<DerivedKeys> {
	const passwordKey = await crypto.subtle.importKey(
		"raw", te.encode(password), "PBKDF2", false, ["deriveBits"],
	);
	const masterBits = await crypto.subtle.deriveBits(
		{ name: "PBKDF2", hash: "SHA-256", salt: fromB64(kdfSaltB64) as BufferSource, iterations },
		passwordKey,
		256,
	);
	const hkdfKey = await crypto.subtle.importKey("raw", masterBits, "HKDF", false, ["deriveBits"]);
	const expand = (info: string) =>
		crypto.subtle.deriveBits(
			{ name: "HKDF", hash: "SHA-256", salt: new Uint8Array(0), info: te.encode(info) },
			hkdfKey,
			256,
		);
	const authBits = await expand("riksdagen-auth-v1");
	const kekBits = await expand("riksdagen-enc-v1");
	const kek = await crypto.subtle.importKey("raw", kekBits, "AES-GCM", false, ["encrypt", "decrypt"]);
	return { authKey: toB64(authBits), kek };
}

/* ── AES-GCM "v1:" blobs ─────────────────────────────────────────── */

export function isEncrypted(value: unknown): value is string {
	return typeof value === "string" && value.startsWith(BLOB_PREFIX);
}

export async function encryptBytes(key: CryptoKey, data: Uint8Array): Promise<string> {
	const iv = randomBytes(12);
	const ct = new Uint8Array(
		await crypto.subtle.encrypt({ name: "AES-GCM", iv: iv as BufferSource }, key, data as BufferSource),
	);
	const out = new Uint8Array(iv.length + ct.length);
	out.set(iv);
	out.set(ct, iv.length);
	return BLOB_PREFIX + toB64(out);
}

export async function decryptBytes(key: CryptoKey, blob: string): Promise<Uint8Array> {
	if (!isEncrypted(blob)) throw new Error("not an encrypted blob");
	const raw = fromB64(blob.slice(BLOB_PREFIX.length));
	const pt = await crypto.subtle.decrypt(
		{ name: "AES-GCM", iv: raw.slice(0, 12) as BufferSource }, key, raw.slice(12) as BufferSource,
	);
	return new Uint8Array(pt);
}

export async function encryptString(key: CryptoKey, s: string): Promise<string> {
	return encryptBytes(key, te.encode(s));
}

export async function decryptString(key: CryptoKey, blob: string): Promise<string> {
	return td.decode(await decryptBytes(key, blob));
}

/** Decrypt when the value is a v1 blob; pass anything else through. */
export async function decryptMaybe(key: CryptoKey, value: string): Promise<string> {
	return isEncrypted(value) ? decryptString(key, value) : value;
}

export async function encryptJson(key: CryptoKey, value: unknown): Promise<string> {
	return encryptString(key, JSON.stringify(value));
}

export async function decryptJson<T>(key: CryptoKey, blob: string): Promise<T> {
	return JSON.parse(await decryptString(key, blob)) as T;
}

/* ── DEK lifecycle ───────────────────────────────────────────────── */

export function generateDekRaw(): Uint8Array {
	return randomBytes(32);
}

/** Import as non-extractable: the stored (IndexedDB) copy can be used but never exported. */
export async function importAesKey(raw: Uint8Array): Promise<CryptoKey> {
	return crypto.subtle.importKey("raw", raw as BufferSource, "AES-GCM", false, ["encrypt", "decrypt"]);
}

export async function wrapDek(kek: CryptoKey, dekRaw: Uint8Array): Promise<string> {
	return encryptBytes(kek, dekRaw);
}

export async function unwrapDek(kek: CryptoKey, wrappedDek: string): Promise<CryptoKey> {
	return importAesKey(await decryptBytes(kek, wrappedDek));
}

/* ── per-board research keys ─────────────────────────────────────── */

export type BoardKey = {
	/** base64 of the raw key — sent to the server only on job-spawning requests */
	rawB64: string;
	key: CryptoKey;
};

export async function generateBoardKey(): Promise<BoardKey> {
	const raw = randomBytes(32);
	return { rawB64: toB64(raw), key: await importAesKey(raw) };
}

export async function wrapBoardKey(dek: CryptoKey, boardKey: BoardKey): Promise<string> {
	return encryptBytes(dek, fromB64(boardKey.rawB64));
}

export async function unwrapBoardKey(dek: CryptoKey, wrapped: string): Promise<BoardKey> {
	const raw = await decryptBytes(dek, wrapped);
	return { rawB64: toB64(raw), key: await importAesKey(raw) };
}

/* ── IndexedDB CryptoKey store (survives reloads; cleared on logout) ─ */

const DB_NAME = "riksdagen-auth";
const STORE = "keys";

function openDb(): Promise<IDBDatabase> {
	return new Promise((resolve, reject) => {
		const req = indexedDB.open(DB_NAME, 1);
		req.onupgradeneeded = () => {
			if (!req.result.objectStoreNames.contains(STORE)) req.result.createObjectStore(STORE);
		};
		req.onsuccess = () => resolve(req.result);
		req.onerror = () => reject(req.error);
	});
}

function idbRequest<T>(makeReq: (store: IDBObjectStore) => IDBRequest<T>, mode: IDBTransactionMode): Promise<T> {
	return openDb().then(
		(db) =>
			new Promise<T>((resolve, reject) => {
				const tx = db.transaction(STORE, mode);
				const req = makeReq(tx.objectStore(STORE));
				req.onsuccess = () => resolve(req.result);
				req.onerror = () => reject(req.error);
				tx.oncomplete = () => db.close();
			}),
	);
}

export async function idbPutKey(name: string, key: CryptoKey): Promise<void> {
	await idbRequest((s) => s.put(key, name), "readwrite");
}

export async function idbGetKey(name: string): Promise<CryptoKey | null> {
	try {
		const value = await idbRequest((s) => s.get(name), "readonly");
		return (value as CryptoKey) ?? null;
	} catch {
		return null;
	}
}

export async function idbDeleteKey(name: string): Promise<void> {
	try {
		await idbRequest((s) => s.delete(name), "readwrite");
	} catch {
		/* best effort */
	}
}
