import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

type TalkDrawerContextValue = {
	openTalkId: string | null;
	openTalk: (id: string) => void;
	closeTalk: () => void;
};

const TalkDrawerContext = createContext<TalkDrawerContextValue | null>(null);

/** Strips the ArangoDB "speeches/" collection prefix so callers can pass either form. */
export function normalizeTalkId(rawId: string): string {
	return rawId.startsWith("speeches/") ? rawId.slice("speeches/".length) : rawId;
}

export function TalkDrawerProvider({ children }: { children: ReactNode }) {
	const [openTalkId, setOpenTalkId] = useState<string | null>(null);

	const openTalk = useCallback((id: string) => {
		setOpenTalkId(normalizeTalkId(id));
	}, []);
	const closeTalk = useCallback(() => setOpenTalkId(null), []);

	const value = useMemo(() => ({ openTalkId, openTalk, closeTalk }), [openTalkId, openTalk, closeTalk]);

	return <TalkDrawerContext.Provider value={value}>{children}</TalkDrawerContext.Provider>;
}

export function useTalkDrawer(): TalkDrawerContextValue {
	const ctx = useContext(TalkDrawerContext);
	if (!ctx) throw new Error("useTalkDrawer must be used within a TalkDrawerProvider");
	return ctx;
}
