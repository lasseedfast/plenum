// src/components/MentionTextarea.tsx
import React, { useCallback, useEffect, useRef, useState, forwardRef, useImperativeHandle } from "react";
import type { MentionSuggestion } from "./MentionInput";
import type { PersonSuggestion } from "../types";
import { PersonSuggestionList } from "./PersonSuggestionList";
import { getSessionHeaders } from "../api";

// Wide enough for a photo, a name, a party chip and a line of constituency.
const MIN_POPOVER_WIDTH = 320;
const TOKEN_REGEX = /@([^\s@]+(?:\s+[^\s@]*)*)$/; // Allow multi-word mention tokens.

/** Matches MentionInput helper to keep escaping rules identical. */
const escapeHtml = (raw: string): string => raw.replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char] ?? char));

type Props = {
  value: string;
  onChange: (next: string) => void;
  onSubmit?: (finalText: string) => void; // finalText includes metadata lines
  fetchUrl?: string; // default /api/suggest
  minChars?: number; // minimum chars after @ to start suggesting
  maxSuggestions?: number;
  placeholder?: string;
  className?: string;
  rows?: number;
  onPick?: (suggestion: MentionSuggestion) => void;
};

// Helper to serialize selected mentions as metadata lines for LLM
function serializeMentions(mentions: MentionSuggestion[]): string {
  if (!mentions.length) return "";
  return mentions
    .filter(m => m._key)
    .map(m => `\nINTRESSENT_IDS${m.name} har person_id ${m._key}`)
    .join("");
}

// Add imperative handle to expose getFinalText to parent
export const MentionTextarea = forwardRef(function MentionTextarea(
  {
    value,
    onChange,
    onSubmit,
    fetchUrl = "/api/suggest",
    minChars = 2,
    maxSuggestions = 8,
    placeholder,
    className,
    rows = 1,
    onPick,
  }: Props,
  ref: React.Ref<{ getFinalText: () => string }>
) {
  const taRef = useRef<HTMLTextAreaElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [suggestions, setSuggestions] = useState<MentionSuggestion[]>([]);
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const [queryText, setQueryText] = useState<string | null>(null);
  const [anchorRect, setAnchorRect] = useState<{ left: number; top: number; width: number } | null>(null);

  const debounceTimer = useRef<number | null>(null);
  const inflightFetch = useRef<AbortController | null>(null);

  // Track selected mentions for metadata lines (not shown to user)
  const [selectedMentions, setSelectedMentions] = useState<MentionSuggestion[]>([]);

  // Expose getFinalText to parent via ref
  useImperativeHandle(ref, () => ({
    getFinalText: () => value + serializeMentions(selectedMentions),
  }), [value, selectedMentions]);

  const updateAnchorRect = useCallback(() => {
    // Recalculate dropdown placement every time the textarea moves or resizes.
    const ta = taRef.current;
    const container = containerRef.current;
    if (!ta || !container) return;
    const taRect = ta.getBoundingClientRect();
    const containerRect = container.getBoundingClientRect();
    setAnchorRect({
      left: taRect.left - containerRect.left,
      top: taRect.bottom - containerRect.top,
      width: taRect.width,
    });
  }, []);

  useEffect(() => {
    updateAnchorRect();
  }, [updateAnchorRect]);

  // Helper: find @token immediately before caret; returns { token, atIndex, tokenStart, tokenEnd } or null
  const getTokenInfo = useCallback(() => {
    const ta = taRef.current;
    if (!ta) return null;
    const pos = ta.selectionStart ?? value.length;
    const textBefore = value.slice(0, pos);
    const match = textBefore.match(TOKEN_REGEX);
    if (!match) return null;
    const rawToken = match[1];
    const token = rawToken.trimEnd();
    if (!token) return null;
    const atIndex = pos - match[0].length;
    const tokenStart = atIndex;
    const tokenEnd = pos;
    return { token, atIndex, tokenStart, tokenEnd, caret: pos };
  }, [value]);

  // Open/close suggestion popup whenever token meets minChars
  useEffect(() => {
    const info = getTokenInfo();
    if (!info || info.token.length < minChars) {
      // close
      setOpen(false);
      setSuggestions([]);
      setQueryText(null);
      setActiveIndex(0);
      return;
    }
    setQueryText(info.token);
    updateAnchorRect();

    // debounce fetch
    if (debounceTimer.current) window.clearTimeout(debounceTimer.current);
    debounceTimer.current = window.setTimeout(() => {
      // cancel previous fetch
      if (inflightFetch.current) inflightFetch.current.abort();
      const ctrl = new AbortController();
      inflightFetch.current = ctrl;

      (async () => {
        try {
          const url = `${fetchUrl}?q=${encodeURIComponent(info.token)}&limit=${maxSuggestions}`;
          const res = await fetch(url, { signal: ctrl.signal, headers: getSessionHeaders() });
          if (!res.ok) {
            setSuggestions([]);
            setOpen(false);
            return;
          }
          const json = (await res.json()) as Array<string | MentionSuggestion>;
          const normalized = (json ?? [])
            .map((entry) =>
              typeof entry === "string"
                ? { name: entry }
                // Spread first: party, constituency and photo are what the
                // dropdown shows, and dropping them here is what left it a
                // list of indistinguishable names.
                : { ...entry, name: entry.name ?? "", _key: entry._key ?? (entry as any)?.key },
            )
            .filter((entry) => entry.name.length > 0);
          const still = getTokenInfo();
          if (!still || still.token !== info.token) return;
          setSuggestions(normalized);
          setActiveIndex(0);
          setOpen(normalized.length > 0);
        } catch (e) {
          if ((e as any)?.name === "AbortError") return;
          console.error("mention-suggest error", e);
          setSuggestions([]);
          setOpen(false);
        } finally {
          inflightFetch.current = null;
        }
      })();
    }, 180); // 180ms debounce
    return () => {
      if (debounceTimer.current) window.clearTimeout(debounceTimer.current);
    };
  }, [value, getTokenInfo, fetchUrl, maxSuggestions, minChars, updateAnchorRect]);

  // Clean up inflight fetch on unmount
  useEffect(() => {
    return () => {
      if (inflightFetch.current) inflightFetch.current.abort();
      if (debounceTimer.current) window.clearTimeout(debounceTimer.current);
    };
  }, []);

  // Helper: Resize textarea to fit content
  function resizeTextarea() {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto"; // Reset height so scrollHeight is accurate
    ta.style.height = ta.scrollHeight + "px"; // Set height to fit content
  }

  // Resize on mount and whenever value changes
  useEffect(() => {
    resizeTextarea();
  }, [value]);

  // When user selects a suggestion, replace token in text and focus textarea
  const pickSuggestion = useCallback(
    (suggestion: MentionSuggestion) => {
      const info = getTokenInfo();
      if (!info) return;

      // Remove @token and insert clean name
      const before = value.slice(0, info.tokenStart);
      const after = value.slice(info.tokenEnd);
      const inserted = `${suggestion.name} `;
      const nextValue = `${before}${inserted}${after}`;
      onChange(nextValue);

      // Track selected mention (for metadata lines)
      setSelectedMentions(prev => {
        // Avoid duplicates by _key
        if (suggestion._key && prev.some(m => m._key === suggestion._key)) return prev;
        return suggestion._key ? [...prev, suggestion] : prev;
      });

      requestAnimationFrame(() => {
        const ta = taRef.current;
        if (!ta) return;
        const newCaret = (before + inserted).length;
        ta.focus();
        ta.selectionStart = ta.selectionEnd = newCaret;
        resizeTextarea(); // Resize after inserting suggestion
      });

      setOpen(false);
      setSuggestions([]);
      setQueryText(null);

      if (onPick) onPick(suggestion);
    },
    [getTokenInfo, onChange, value, onPick],
  );

  // When submitting, append metadata lines to the visible text before sending to backend
  const handleSubmit = useCallback(() => {
    if (!onSubmit) return;
    // Combine visible text and metadata lines
    const finalText = value + serializeMentions(selectedMentions);
    onSubmit(finalText);
  }, [onSubmit, value, selectedMentions]);

  // Keyboard handling inside textarea
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (open) {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          setActiveIndex((i) => Math.min(i + 1, suggestions.length - 1));
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          setActiveIndex((i) => Math.max(i - 1, 0));
          return;
        }
        if (e.key === "Enter") {
          // If suggestion open and one active, pick it
          if (suggestions.length > 0) {
            e.preventDefault();
            pickSuggestion(suggestions[activeIndex]);
            return;
          }
          // else allow parent to handle submit (if not shift/ctrl/meta)
          if (!e.shiftKey && !e.ctrlKey && !e.metaKey) {
            if (onSubmit) {
              e.preventDefault();
              onSubmit(value);
            }
          }
          return;
        }
        if (e.key === "Escape") {
          e.preventDefault();
          setOpen(false);
          return;
        }
      } else {
        // suggestions closed: catch Enter for submit
        if (e.key === "Enter") {
          if (!e.shiftKey && !e.ctrlKey && !e.metaKey) {
            if (onSubmit) {
              e.preventDefault();
              onSubmit(value);
            }
          }
        }
      }
    },
    [open, suggestions, activeIndex, pickSuggestion, onSubmit, value],
  );

  const handleChange = useCallback(
    (event: React.ChangeEvent<HTMLTextAreaElement>) => {
      onChange(event.target.value);
      resizeTextarea(); // Resize after every change
    },
    [onChange],
  );

  return (
    <div
      ref={containerRef}
      style={{ position: "relative", width: "100%" }}
    >
      <textarea
        ref={taRef}
        rows={rows}
        className={className}
        value={value}
        placeholder={placeholder}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        onClick={updateAnchorRect}
        onFocus={updateAnchorRect}
        onBlur={() => {
          window.setTimeout(() => setOpen(false), 150);
        }}
        style={{ resize: "none", width: "100%" }}
      />
      {open && suggestions.length > 0 && (
        <div
          role="listbox"
          aria-label="Ledamöter"
          className="person-suggest"
          style={
            anchorRect
              ? {
                  position: "absolute",
                  left: anchorRect.left,
                  top: anchorRect.top + 6,
                  width: Math.max(anchorRect.width, MIN_POPOVER_WIDTH),
                  zIndex: 9999,
                  background: "white",
                  border: "1px solid rgba(0,0,0,0.12)",
                  boxShadow: "0 6px 20px rgba(0,0,0,0.08)",
                  borderRadius: 6,
                  padding: 6,
                }
              : { position: "absolute", zIndex: 9999 }
          }
        >
          <PersonSuggestionList
            suggestions={suggestions as PersonSuggestion[]}
            activeIndex={activeIndex}
            onPick={(person) => pickSuggestion(person as MentionSuggestion)}
          />
        </div>
      )}
    </div>
  );
});

export default MentionTextarea;
