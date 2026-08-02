import React, { useCallback, useEffect, useRef, useState } from "react";
// Assuming you have this API helper for fetching suggestions
// import { getSessionHeaders } from "../api"; 

// Placeholder for getSessionHeaders if you don't have it defined
const getSessionHeaders = () => ({}); 

export type MentionSuggestion = {
  name: string;
  _key?: string;
};

type Props = {
  value: string;
  onChange: (next: string) => void;
  onPick?: (suggestion: MentionSuggestion) => void;
  fetchUrl?: string;
  minChars?: number;
  maxSuggestions?: number;
  placeholder?: string;
  className?: string;
  inputProps?: React.InputHTMLAttributes<HTMLInputElement>;
};

const MIN_POPOVER_WIDTH = 180;
// Only look for new suggestions: @token
const TOKEN_REGEX = /@([^\s@]+(?:\s+[^\s@]*)*)$/;

// Utility function to escape HTML special characters for plain text
const escapeHtml = (unsafe: string) => {
  return unsafe.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
};

/**
 * NEW UTILITY: Extracts the selected names from the input value.
 * This is the function you will use externally for filtering/processing.
 */
export const extractMentions = (value: string, suggestions: MentionSuggestion[]): string[] => {
  // A simple way is to match all known selected names that exist in the value.
  // This assumes suggestions are unique and case-insensitive matching is OK.
  
  const selectedNames = suggestions.map(s => s.name.trim());
  const foundNames: string[] = [];

  // Match the names in the order they appear in the text
  for (const name of selectedNames) {
    if (value.includes(name) && !foundNames.includes(name)) {
      // NOTE: This is a simple existence check. For more robust extraction 
      // (e.g., ensuring "John" in "John Johnson" is not counted twice), 
      // you would need a more complex regex to match whole words.
      foundNames.push(name);
    }
  }
  return foundNames;
};


/**
 * Updated to render the text as PLAIN text, but still uses the display DIV
 * for multi-line support and perfect alignment of the transparent input.
 */
const renderHighlightedMentions = (value: string): string => {
  // We don't need bolding anymore, so we just escape the entire value.
  // The display DIV is still needed to enable word wrapping (pre-wrap) and multi-line text.
  return escapeHtml(value) || "&#8203;";
};


/** If no value is present we render the placeholder text instead of showing the raw input placeholder. */
const renderPlaceholder = (placeholder?: string): string => {
  // Render the placeholder in italic and normal weight for clarity
  if (!placeholder) return "&#8203;";
  return `<span style="opacity: 0.5; font-style: italic; font-weight: 400;">${escapeHtml(placeholder)}</span>`;
};

export function MentionInput({
  value,
  onChange,
  onPick,
  fetchUrl = "/api/suggest",
  minChars = 3,
  maxSuggestions = 8,
  placeholder,
  className,
  inputProps,
}: Props) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [suggestions, setSuggestions] = useState<MentionSuggestion[]>([]);
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const [anchorRect, setAnchorRect] = useState<{ left: number; top: number; width: number } | null>(null);
  const debounceTimer = useRef<number | null>(null);
  const inflightFetch = useRef<AbortController | null>(null);

  // We need a list of all *possible* suggestions to extract the ones that were picked.
  const allKnownSuggestions = useRef<MentionSuggestion[]>([]);


  const updateAnchorRect = useCallback(() => {
    const el = inputRef.current;
    const container = containerRef.current;
    if (!el || !container) return;
    const elRect = el.getBoundingClientRect();
    const containerRect = container.getBoundingClientRect();
    setAnchorRect({
      left: elRect.left - containerRect.left,
      top: elRect.bottom - containerRect.top,
      width: elRect.width,
    });
  }, []);

  useEffect(() => {
    updateAnchorRect();
  }, [updateAnchorRect, value]);

  // returns token info for @token immediately before caret, or null
  const getTokenInfo = useCallback(() => {
    const el = inputRef.current;
    if (!el) return null;
    const pos = el.selectionStart ?? value.length;
    const before = value.slice(0, pos);
    const match = before.match(TOKEN_REGEX);
    if (!match) return null;
    const rawToken = match[1];
    const token = rawToken.trimEnd();
    if (!token) return null;
    const atIndex = pos - match[0].length;
    return { token, tokenStart: atIndex, tokenEnd: pos, caret: pos };
  }, [value]);

  // fetch suggestions when token meets minChars
  useEffect(() => {
    const info = getTokenInfo();
    if (!info || info.token.length < minChars) {
      setOpen(false);
      setSuggestions([]);
      setActiveIndex(0);
      return;
    }
    updateAnchorRect();

    if (debounceTimer.current) window.clearTimeout(debounceTimer.current);
    debounceTimer.current = window.setTimeout(() => {
      if (inflightFetch.current) inflightFetch.current.abort();
      const ctrl = new AbortController();
      inflightFetch.current = ctrl;
      (async () => {
        try {
          const url = `${fetchUrl}?q=${encodeURIComponent(info.token)}&limit=${maxSuggestions}`;
          const res = await fetch(url, { signal: ctrl.signal, headers: getSessionHeaders() });
          // ... (fetch and normalization logic remains the same)
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
                : { name: entry.name ?? entry.namn ?? "", _key: entry._key ?? (entry as any)?.key },
            )
            .filter((entry) => entry.name.length > 0);
          
          const still = getTokenInfo();
          if (!still || still.token !== info.token) return;
          
          setSuggestions(normalized);
          setActiveIndex(0);
          setOpen(normalized.length > 0);

          // Update the list of all known suggestions
          normalized.forEach(s => {
              if (!allKnownSuggestions.current.some(known => known.name === s.name)) {
                  allKnownSuggestions.current.push(s);
              }
          });
        } catch (e) {
          if ((e as any)?.name === "AbortError") return;
          console.error("mention-input fetch error", e);
          setSuggestions([]);
          setOpen(false);
        } finally {
          inflightFetch.current = null;
        }
      })();
    }, 180);
    return () => {
      if (debounceTimer.current) window.clearTimeout(debounceTimer.current);
    };
  }, [value, getTokenInfo, fetchUrl, maxSuggestions, minChars, updateAnchorRect]);

  useEffect(() => {
    return () => {
      if (inflightFetch.current) inflightFetch.current.abort();
      if (debounceTimer.current) window.clearTimeout(debounceTimer.current);
    };
  }, []);

  const pickSuggestion = useCallback(
    (suggestion: MentionSuggestion) => {
      const info = getTokenInfo();
      if (!info) return;

      // 1. Get the parts before and after the @token
      const before = value.slice(0, info.tokenStart);
      const after = value.slice(info.tokenEnd);

      // 2. Insert the full name (keeping the @ symbol for visual clarity)
      // This allows the user to see what they selected, but we'll strip it before sending to backend
      const inserted = `@"${suggestion.name}" `;
      const next = `${before}${inserted}${after}`;

      onChange(next);

      // 3. Set the caret position after the inserted name
      requestAnimationFrame(() => {
        const el = inputRef.current;
        if (!el) return;
        const newCaret = (before + inserted).length;
        el.focus();
        el.setSelectionRange(newCaret, newCaret);
      });

      setOpen(false);
      setSuggestions([]);
      if (onPick) onPick(suggestion);
    },
    [getTokenInfo, onChange, value, onPick],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
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
          if (suggestions.length > 0) {
            e.preventDefault();
            pickSuggestion(suggestions[activeIndex]);
            return;
          }
        }
        if (e.key === "Escape") {
          e.preventDefault();
          setOpen(false);
          return;
        }
      }
    },
    [open, suggestions, activeIndex, pickSuggestion],
  );

  const handleChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      // Since the input value is now the plain text we want, the change handler is simple.
      onChange(e.target.value);
    },
    [onChange],
  );

  const popupStyle: React.CSSProperties = anchorRect
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
    : { position: "absolute", zIndex: 9999 };

  // --- Styles for the visual overlay trick ---

  // NOTE: We remove the fontWeight: "bold" from inputStyle because the visual DIV is now plain text.
  // Remove inline style objects, use CSS classes instead

  // The HTML to display: just the value, no custom placeholder
  const displayHtml = renderHighlightedMentions(value);

  return (
    <div
      ref={containerRef}
      className={className}
      style={{ position: "relative", width: "100%" }}
    >
      {/* 1. The visual display area (plain text only) */}
      <div
        aria-hidden="true"
        className="mention-input-display"
        dangerouslySetInnerHTML={{ __html: displayHtml }}
      />

      {/* 2. The actual text input area, placed on top and transparent when not focused */}
      <input
        {...inputProps}
        ref={inputRef}
        type="text"
        value={value}
        placeholder={placeholder} // Show native placeholder
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        onClick={updateAnchorRect}
        onFocus={updateAnchorRect}
        onBlur={() => {
          window.setTimeout(() => setOpen(false), 150);
        }}
        aria-autocomplete="list"
        aria-expanded={open}
        aria-haspopup="listbox"
        className="mention-input-field"
      />

      {/* 3. The suggestions popover */}
      {open && suggestions.length > 0 && (
        <div role="listbox" aria-label="Talare" style={popupStyle}>
          <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {suggestions.map((s, i) => (
              <li
                key={s._key ?? s.name}
                role="option"
                aria-selected={i === activeIndex}
                onMouseDown={(ev) => ev.preventDefault()}
                onClick={() => pickSuggestion(s)}
                style={{
                  padding: "8px 10px",
                  cursor: "pointer",
                  background: i === activeIndex ? "rgba(0,0,0,0.06)" : undefined,
                  borderRadius: 4,
                }}
              >
                {s.name}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export default MentionInput;