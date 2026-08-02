import re
from typing import List, Tuple
from dataclasses import dataclass


class TextChunker:
    """
    A smart text chunker that analyzes text structure and automatically
    determines the best splitting strategy based on detected patterns.
    Always splits on sentence boundaries.
    """

    def __init__(self, chunk_limit: int = 500, chunk_overlap: int = 0):
        """
        Initialize the smart chunker.

        Args:
            chunk_limit: Target maximum characters per chunk (may be exceeded to preserve sentences)
            chunk_overlap: Number of characters to overlap between chunks
        """
        self.chunk_limit = chunk_limit
        self.chunk_overlap = chunk_overlap

    @dataclass
    class SeparatorInfo:
        """Information about a detected separator in the text."""

        pattern: str
        count: int
        priority: int
        description: str
        keep_separator: bool = True

    def _detect_separators(self, text: str) -> List["TextChunker.SeparatorInfo"]:
        """
        Analyze the text and detect available separators with their priority.
        Returns a list of separators ordered by priority (best to worst).
        """
        separators = []

        # Markdown headers (# Header, ## Header, etc.)
        md_headers = re.findall(r"^#{1,6}\s+.+$", text, re.MULTILINE)
        if md_headers:
            separators.append(
                self.SeparatorInfo(
                    pattern=r"\n(?=#{1,6}\s+)",
                    count=len(md_headers),
                    priority=1,
                    description=f"Markdown headers ({len(md_headers)} found)",
                )
            )

        # HTML headers (<h1>, <h2>, etc.)
        html_headers = re.findall(
            r"<h[1-6][^>]*>.*?</h[1-6]>", text, re.IGNORECASE | re.DOTALL
        )
        if html_headers:
            separators.append(
                self.SeparatorInfo(
                    pattern=r"\n(?=<h[1-6])",
                    count=len(html_headers),
                    priority=2,
                    description=f"HTML headers ({len(html_headers)} found)",
                )
            )

        # HTML divs or sections
        html_divs = re.findall(r"<(?:div|section)[^>]*>", text, re.IGNORECASE)
        if html_divs:
            separators.append(
                self.SeparatorInfo(
                    pattern=r"\n(?=<(?:div|section))",
                    count=len(html_divs),
                    priority=3,
                    description=f"HTML divs/sections ({len(html_divs)} found)",
                )
            )

        # Horizontal rules (---, ***, ___)
        hr_count = len(re.findall(r"^(?:---+|\*\*\*+|___+)\s*$", text, re.MULTILINE))
        if hr_count:
            separators.append(
                self.SeparatorInfo(
                    pattern=r"\n(?:---+|\*\*\*+|___+)\s*\n",
                    count=hr_count,
                    priority=4,
                    description=f"Horizontal rules ({hr_count} found)",
                )
            )

        # Bullet points or numbered lists
        list_items = re.findall(r"^[\s]*(?:[-*+]|\d+\.)\s+", text, re.MULTILINE)
        if list_items:
            # Group consecutive list items
            list_groups = len(
                re.findall(r"(?:^[\s]*(?:[-*+]|\d+\.)\s+.*\n)+", text, re.MULTILINE)
            )
            if list_groups > 1:
                separators.append(
                    self.SeparatorInfo(
                        pattern=r"\n(?=[\s]*(?:[-*+]|\d+\.)\s+)",
                        count=list_groups,
                        priority=5,
                        description=f"List groups ({list_groups} found)",
                    )
                )

        # Double newlines (paragraphs)
        double_newlines = text.count("\n\n")
        if double_newlines > 0:
            separators.append(
                self.SeparatorInfo(
                    pattern=r"\n\n",
                    count=double_newlines,
                    priority=6,
                    description=f"Paragraphs ({double_newlines} found)",
                )
            )

        # Single newlines
        single_newlines = text.count("\n") - (double_newlines * 2)
        if single_newlines > 0:
            separators.append(
                self.SeparatorInfo(
                    pattern=r"\n",
                    count=single_newlines,
                    priority=7,
                    description=f"Lines ({single_newlines} found)",
                )
            )

        # Sentence endings
        sentences = re.findall(r"[.!?]+[\s\n]+", text)
        if sentences:
            separators.append(
                self.SeparatorInfo(
                    pattern=r"(?<=[.!?])\s+",
                    count=len(sentences),
                    priority=8,
                    description=f"Sentences ({len(sentences)} found)",
                )
            )

        # Sort by priority (lower number = higher priority)
        separators.sort(key=lambda x: x.priority)

        return separators

    def _split_by_separator(self, text: str, separator_pattern: str) -> List[str]:
        """Split text by a separator pattern, preserving the separator."""
        if not text:
            return []

        # Split while keeping the separator
        parts = re.split(f"({separator_pattern})", text)

        # Reconstruct pieces with separators
        result = []
        current = ""

        for part in parts:
            if part:
                current += part
                # If we just added a separator, save this piece
                if re.match(separator_pattern, part):
                    if current.strip():
                        result.append(current)
                    current = ""

        # Add any remaining text
        if current.strip():
            result.append(current)

        # If no splits occurred, return the original text
        if not result:
            result = [text]

        return result

    def _split_by_sentences(self, text: str) -> List[str]:
        """
        Split text into complete sentences, ensuring no mid-sentence breaks.
        Returns chunks that respect sentence boundaries and tries to balance chunk sizes.
        """
        # Match sentence boundaries: period, exclamation, or question mark followed by space/newline
        sentence_pattern = r"(?<=[.!?])\s+"
        sentences = re.split(sentence_pattern, text)

        if not sentences:
            return [text]

        # Filter out empty sentences
        sentences = [s.strip() for s in sentences if s.strip()]

        if not sentences:
            return [text]

        # If all sentences fit in one chunk, return as is
        total_length = (
            sum(len(s) for s in sentences) + len(sentences) - 1
        )  # +1 for spaces between
        if total_length <= self.chunk_limit:
            return [" ".join(sentences)]

        # Build chunks greedily first
        chunks = []
        current_chunk = ""

        for sentence in sentences:
            # If adding this sentence would exceed limit and we have content, start new chunk
            if (
                current_chunk
                and len(current_chunk) + len(sentence) + 1 > self.chunk_limit
            ):
                chunks.append(current_chunk)
                current_chunk = sentence
            else:
                # Add sentence to current chunk
                if current_chunk:
                    current_chunk += " " + sentence
                else:
                    current_chunk = sentence

        # Add final chunk
        if current_chunk:
            chunks.append(current_chunk)

        # Now balance the chunks: if the last chunk is too small, redistribute
        if len(chunks) >= 2:
            last_chunk_size = len(chunks[-1])
            # If last chunk is less than 40% of chunk_limit, try to rebalance
            if last_chunk_size < self.chunk_limit * 0.5:
                # Rebuild from sentences, distributing more evenly
                chunks = self._balance_sentence_chunks(sentences)

        return chunks if chunks else [text]

    def _balance_sentence_chunks(self, sentences: List[str]) -> List[str]:
        """
        Distribute sentences across chunks to minimize size variance.
        Uses a greedy approach that looks ahead to avoid tiny final chunks.
        """
        if not sentences:
            return []

        total_length = sum(len(s) for s in sentences) + len(sentences) - 1
        # Estimate number of chunks needed
        estimated_chunks = max(
            1, (total_length + self.chunk_limit - 1) // self.chunk_limit
        )
        target_size = total_length / estimated_chunks

        chunks = []
        current_chunk = ""
        remaining_sentences = len(sentences)

        for i, sentence in enumerate(sentences):
            remaining_sentences -= 1

            if not current_chunk:
                current_chunk = sentence
            else:
                # Calculate what's left to process
                remaining_text_length = sum(len(s) for s in sentences[i + 1 :])
                if remaining_sentences > 0:
                    remaining_text_length += remaining_sentences  # spaces

                current_length = len(current_chunk)
                new_length = current_length + len(sentence) + 1

                # Decide whether to add to current chunk or start new one
                # Start new chunk if:
                # 1. Adding would exceed limit AND current chunk is at least 60% of target
                # 2. OR we're getting close to target size and have plenty of text left
                if (
                    new_length > self.chunk_limit
                    and current_length >= target_size * 0.7
                ):
                    chunks.append(current_chunk)
                    current_chunk = sentence
                elif (
                    current_length >= target_size * 0.9
                    and remaining_text_length > target_size * 0.5
                ):
                    # We're near target and there's enough remaining - start new chunk
                    chunks.append(current_chunk)
                    current_chunk = sentence
                else:
                    current_chunk += " " + sentence

        if current_chunk:
            chunks.append(current_chunk)

        return chunks

    def _merge_small_chunks(self, chunks: List[str]) -> List[str]:
        """
        Merge chunks that are smaller than the limit to optimize chunk sizes.
        Ensures the last chunk is not much smaller than the chunk_limit by merging it with the previous chunk if needed.
        """
        if not chunks:
            return []

        merged = []
        current = chunks[0]

        for next_chunk in chunks[1:]:
            # If combining won't exceed limit, merge them
            if len(current) + len(next_chunk) <= self.chunk_limit:
                current += next_chunk
            else:
                merged.append(current)
                current = next_chunk

        # Add the last chunk
        merged.append(current)

        # If the last chunk is much smaller than chunk_limit, merge it with the previous one
        # (unless there's only one chunk)
        if len(merged) >= 2 and len(merged[-1]) < self.chunk_limit * 0.5:
            # Merge last two chunks
            merged[-2] += merged[-1]
            merged.pop(-1)

        return merged

    def _recursive_split(
        self, text: str, separators: List[SeparatorInfo], separator_idx: int = 0
    ) -> List[str]:
        """
        Recursively split text using available separators until chunks fit the limit.
        Always falls back to sentence-aware splitting to avoid mid-sentence breaks.
        """
        # Base case: if text fits, return it
        if len(text) <= self.chunk_limit:
            return [text]

        # If we've exhausted all separators, use sentence-aware splitting
        if separator_idx >= len(separators):
            return self._split_by_sentences(text)

        # Try current separator
        separator = separators[separator_idx]
        splits = self._split_by_separator(text, separator.pattern)

        # If no split occurred or only one piece, try next separator
        if len(splits) <= 1:
            return self._recursive_split(text, separators, separator_idx + 1)

        # Process each split
        result = []
        for split in splits:
            if len(split) <= self.chunk_limit:
                result.append(split)
            else:
                # This split is still too large, recurse with next separator
                sub_chunks = self._recursive_split(split, separators, separator_idx + 1)
                result.extend(sub_chunks)

        # Merge small consecutive chunks
        result = self._merge_small_chunks(result)

        return result

    def chunk(self, text: str, verbose: bool = False, headings: str = "") -> List[str]:
        """
        Chunk the text using automatically detected separators.
        Always splits on complete sentences.

        Args:
            text: The text to chunk
            verbose: If True, print information about detected separators
            headings: Optional headings/context to prepend to each chunk (string)

        Returns:
            List of text chunks, each optionally prefixed with the provided headings
        """
        if not text:
            return []

        # Detect available separators
        separators = self._detect_separators(text)

        if verbose:
            print(f"Detected {len(separators)} separator types:")
            for sep in separators:
                print(f"  - {sep.description} (priority {sep.priority})")
            print()

        # If no separators found, use sentence-aware splitting
        if not separators:
            if verbose:
                print("No natural separators found, splitting by sentences")
            chunks = self._split_by_sentences(text)
        else:
            # Recursively split using detected separators
            chunks = self._recursive_split(text, separators)

        # Clean up chunks
        chunks = [chunk.strip() for chunk in chunks if chunk.strip()]

        # Add headings to each chunk if provided
        if headings and headings.strip():
            # Ensure headings end with newlines for proper formatting
            formatted_headings = headings.strip()
            if not formatted_headings.endswith("\n"):
                formatted_headings += "\n\n"
            else:
                formatted_headings += "\n"

            # Prepend headings to each chunk
            chunks = [f"#{formatted_headings}" + chunk for chunk in chunks]

        if verbose:
            print(f"Created {len(chunks)} chunks")
            if headings:
                print(f"Added headings to each chunk: '{headings.strip()}'")
            print(f"Chunk sizes: {[len(c) for c in chunks]}")

        return chunks


def detect_sql_syntax(query: str) -> dict:
    """
    Detects if a query contains SQL syntax instead of AQL.

    Args:
        query: The query string to check

    Returns:
        dict with keys:
            - is_sql: bool, True if SQL patterns detected
            - issues: list of detected SQL patterns
            - suggestion: str, helpful message for the LLM
    """
    query_upper = query.upper()
    issues = []

    # Common SQL patterns that don't exist in AQL
    sql_patterns = [
        (r"\bINNER\s+JOIN\b", "Found 'INNER JOIN'"),
        (r"\bIS\s+NULL\b", "Found 'IS NULL' - SQL null test"),
        (r"\bHAVING\b", "Found 'HAVING' - SQL post-aggregation filter"),
        (r"\bHAVING\b", "Found 'HAVING' - use FILTER after COLLECT instead"),
        (r"\bINSTR\s*\(", "Found 'INSTR' - Oracle string position function"),
        (r"\bORDER\s+BY\b", "Found 'ORDER BY' - use 'SORT' instead"),
        (r"\bPOSITION\s*\(", "Found 'POSITION' - SQL POSITION function"),
        (
            r"\bCASE\b[\s\S]{0,200}\bWHEN\b",
            "Found 'CASE ... WHEN' - SQL conditional expression",
        ),
        (r"\bINNER\s+JOIN\b", "Found 'INNER JOIN' - use nested FOR loops instead"),
        (r"\bSTRING_AGG\s*\(", "Found 'STRING_AGG(' - Postgres aggregate"),
        (r"\bRIGHT\s+JOIN\b", "Found 'RIGHT JOIN'"),
        (
            r"\bSUBSTRING\s*\(",
            "Found 'SUBSTRING' - SQL substring function (AQL uses SUBSTRING() but with diff. semantics; beware false positives)",
        ),
        (r"\bOVER\s*\(", "Found 'OVER(' - SQL window clause"),
        (r"\bWHERE\b", "Found 'WHERE' - SQL WHERE (AQL uses FILTER)"),
        (r"\bREGEXP_LIKE\s*\(", "Found 'REGEXP_LIKE' - SQL regex function, not in AQL"),
        (r"\bPATINDEX\s*\(", "Found 'PATINDEX' - T-SQL pattern search"),
        (
            r"\bJOIN\s+\w+\s+ON\b",
            "Found 'JOIN ... ON' - use nested FOR loops with FILTER instead",
        ),
        (r"\bSTRPOS\s*\(", "Found 'STRPOS' - Postgres string position function"),
        (r"\bWHERE\b", "Found 'WHERE' - use 'FILTER' instead"),
        (r"\bPARTITION\s+BY\b", "Found 'PARTITION BY' - window function partitioning"),
        (r"\bCAST\s*\([^)]*\s+AS\s+\w+\)", "Found 'CAST(... AS type)' - SQL cast"),
        (r"\bLIKE\b", "Found 'LIKE' - SQL pattern match (AQL uses LIKE(field, pattern) as a function, not an operator)"),
        (r"\bILIKE\b", "Found 'ILIKE' - Postgres case-insensitive LIKE"),
        (r"\bMATCHES\b", "Found 'MATCHES' - not an AQL operator; use FILTER with == or LIKE() function, or SEARCH + TOKENS"),
        (r"CONTAINS\s+['\"%]", "Found 'CONTAINS value' as operator - not valid AQL; use LIKE(field, pattern) or SEARCH + TOKENS"),
        (r"\bGROUP\s+BY\b", "Found 'GROUP BY' - AQL equivalent: COLLECT"),
        (
            r"\bMIN\s*\(\s*\w+\.\w+\s*\)",
            "Found 'MIN(table.column)' - use 'RETURN MIN(doc.field)' or aggregate in COLLECT instead",
        ),
        (r"\bCOUNT\s*\(", "Found 'COUNT(' - SQL aggregate"),
        (
            r"\bREGEXP_REPLACE\s*\(",
            "Found 'REGEXP_REPLACE' - SQL regex function, not in AQL",
        ),
        (r"\bMIN\s*\(", "Found 'MIN(' - SQL aggregate"),
        (
            r"\bOFFSET\s+\d+",
            "Found 'OFFSET' alone - in AQL use 'LIMIT offset, count' format",
        ),
        (
            r"\bAVG\s*\(\s*\w+\.\w+\s*\)",
            "Found 'AVG(table.column)' - use 'RETURN AVG(doc.field)' or aggregate in COLLECT instead",
        ),
        (
            r"\bAS\s+\w+\s+FROM\b",
            "Found table alias with 'AS' - AQL doesn't use AS for collections",
        ),
        (r"\bUNION\b", "Found 'UNION' - SQL set union"),
        (
            r"\bWITH\s+\w+\s+AS\s*\(",
            "Found CTE 'WITH name AS (' - common table expression",
        ),
        (r"\bGROUP_CONCAT\s*\(", "Found 'GROUP_CONCAT(' - MySQL aggregate"),
        (r"\bMAX\s*\(", "Found 'MAX(' - SQL aggregate"),
        (r"\bTOP\s+\d+\b", "Found 'TOP N' - SQL Server style (pagination)"),
        (
            r"\bREGEXP_INSTR\s*\(",
            "Found 'REGEXP_INSTR' - SQL regex function, not in AQL",
        ),
        (r"\bROW_NUMBER\s*\(", "Found 'ROW_NUMBER(' - SQL window function"),
        (r"\bLEFT\s+JOIN\b", "Found 'LEFT JOIN'"),
        (r"\bJOIN\b", "Found 'JOIN' - use nested FOR loops in AQL"),
        (
            r"\bLENGTH\s*\(",
            "Found 'LENGTH' - SQL string length (AQL uses LENGTH() but semantics differ: counts array elements too)",
        ),
        (r"\bSELECT\s+", "Found 'SELECT' - use 'FOR ... IN ... RETURN' instead"),
        (
            r"\bCOUNT\s*\(\s*\*\s*\)",
            "Found 'COUNT(*)' - use 'COLLECT WITH COUNT INTO var' instead",
        ),
        (
            r"\bSUM\s*\(\s*\w+\.\w+\s*\)",
            "Found 'SUM(table.column)' - use 'RETURN SUM(doc.field)' or aggregate in COLLECT instead",
        ),
        (r"\bSELECT\s+", "Found 'SELECT' - SQL-style SELECT"),
        (r"\bOFFSET\b", "Found 'OFFSET' - SQL-style pagination (watch variants)"),
        (
            r"\bSELECT\b[\s\S]{0,400}\bFROM\b",
            "Found 'SELECT ... FROM' - SQL-style query (use 'FOR ... IN ... RETURN')",
        ),
        (r"\bDISTINCT\b", "Found 'DISTINCT' - SQL DISTINCT (AQL uses COLLECT/UNIQUE)"),
        (
            r"\bEXISTS\s*\(\s*SELECT\b",
            "Found 'EXISTS (SELECT ...)' - SQL subquery existence check",
        ),
        (r"\(\s*SELECT\b", "Found '(SELECT ...)' - SQL subquery (nested select)"),
        (r"\bON\s+", "Found 'ON' (JOIN condition) - SQL join condition indicator"),
        (r"\bSUM\s*\(", "Found 'SUM(' - SQL aggregate"),
        (r"\bGROUP\s+BY\b", "Found 'GROUP BY' - use 'COLLECT' instead"),
        (r"\bAVG\s*\(", "Found 'AVG(' - SQL aggregate"),
        (r"\bRIGHT\s+JOIN\b", "Found 'RIGHT JOIN' - use nested FOR loops instead"),
        (
            r"\bFROM\s+\w+\s+WHERE\b",
            "Found 'FROM ... WHERE' - use 'FOR ... IN ... FILTER' instead",
        ),
        (r"\bLEFT\s+JOIN\b", "Found 'LEFT JOIN' - use nested FOR loops instead"),
        (
            r"\bMAX\s*\(\s*\w+\.\w+\s*\)",
            "Found 'MAX(table.column)' - use 'RETURN MAX(doc.field)' or aggregate in COLLECT instead",
        ),
        (r"\bCONVERT\s*\([^)]*\)", "Found 'CONVERT(...)' - SQL convert/cast"),
        (r"\bBETWEEN\b", "Found 'BETWEEN' - SQL range operator"),
        (
            r"\bREGEXP_SUBSTR\s*\(",
            "Found 'REGEXP_SUBSTR' - SQL regex function, not in AQL",
        ),
        (r"\bCHARINDEX\s*\(", "Found 'CHARINDEX' - T-SQL string search"),
        (
            r"\bFROM\s+\w+\s+WHERE\b",
            "Found 'FROM ... WHERE' - SQL-style; use 'FOR ... IN ... FILTER' in AQL",
        ),
        (
            r"\bREGEXP_COUNT\s*\(",
            "Found 'REGEXP_COUNT' - SQL regex function, not in AQL",
        ),
        (r"\bORDER\s+BY\b", "Found 'ORDER BY' - AQL uses SORT"),
        (r"\bUNION\s+ALL\b", "Found 'UNION ALL' - SQL set union"),
        (r"\bIS\s+NOT\s+NULL\b", "Found 'IS NOT NULL' - SQL null test"),
    ]

    for pattern, message in sql_patterns:
        if re.search(pattern, query_upper):
            issues.append(message)

    # Special case: SELECT without FROM (common typo)
    if re.search(r"\bSELECT\b", query_upper) and not re.search(
        r"\bFOR\s+\w+\s+IN\b", query_upper
    ):
        if "Found 'SELECT'" not in [i for i in issues]:
            issues.append(
                "Query starts with SELECT but has no FOR loop - this is SQL, not AQL"
            )

    is_sql = len(issues) > 0

    suggestion = ""
    if is_sql:
        suggestion = (
            "ERROR: This query uses SQL syntax, not AQL! "
            "AQL (ArangoDB Query Language) syntax:\n"
            "- Start with: FOR doc IN collection\n"
            "- Filter with: FILTER doc.field == value\n"
            "- End with: RETURN doc (or specific fields)\n"
            "- For joins: use nested FOR loops\n"
            "- For grouping: use COLLECT\n\n"
            f"Detected issues:\n" + "\n".join(f"- {issue}" for issue in issues)
        )

    return {"is_sql": is_sql, "issues": issues, "suggestion": suggestion}



import re
from typing import List, Tuple

def _norm_whitespace(s: str) -> str:
    return re.sub(r'\s+', ' ', s).strip()

def _extract_clause(sql: str, name: str, terminators: List[str]) -> Tuple[str, str]:
    """Extract clause `name` (like 'where') returning (content, remainder)"""
    pattern = rf'(?i)\b{name}\b\s*(.+)'
    m = re.search(pattern, sql)
    if not m:
        return '', sql
    rest = m.group(1)
    # cut at first terminator token
    min_pos = len(rest)
    for t in terminators:
        t_re = re.search(rf'(?i)\b{t}\b', rest)
        if t_re:
            min_pos = min(min_pos, t_re.start())
    return rest[:min_pos].strip(), rest[min_pos:].strip()

def sql_to_aql(sql: str) -> str:
    s = _norm_whitespace(sql).rstrip(';')
    s_low = s.lower()

    # SELECT clause
    m = re.search(r'(?i)\bselect\b\s+(.+?)\s+\bfrom\b\s', s)
    if not m:
        raise ValueError("Cannot parse SELECT clause")
    select_part = m.group(1).strip()

    # FROM clause (capture table and optional alias)
    m = re.search(r'(?i)\bfrom\b\s+([^\s,]+)(?:\s+([a-zA-Z_][\w]*))?', s)
    if not m:
        raise ValueError("Cannot parse FROM clause")
    from_table = m.group(1)
    from_alias = m.group(2) if m.group(2) else from_table

    # Find joins (simple INNER JOIN / JOIN)
    joins = []
    for jm in re.finditer(r'(?i)\bjoin\b\s+([^\s]+)(?:\s+([a-zA-Z_][\w]*))?\s+\bon\b\s+([^ ]+)', s):
        j_table, j_alias, j_on = jm.group(1), (jm.group(2) or jm.group(1)), jm.group(3)
        joins.append((j_table, j_alias, j_on))

    # WHERE
    where_part, _ = _extract_clause(s, 'where', ['group by', 'order by', 'limit'])

    # GROUP BY
    group_by = ''
    m = re.search(r'(?i)\bgroup\s+by\b\s+(.+?)(?:\s+\border\s+by\b|\s+\blimit\b|$)', s)
    if m:
        group_by = m.group(1).strip()

    # ORDER BY
    order_by = ''
    m = re.search(r'(?i)\border\s+by\b\s+(.+?)(?:\s+\blimit\b|$)', s)
    if m:
        order_by = m.group(1).strip()

    # LIMIT / OFFSET
    offset = None
    limit = None
    m = re.search(r'(?i)\blimit\b\s+(\d+)\s*,\s*(\d+)', s)
    if m:
        offset = int(m.group(1)); limit = int(m.group(2))
    else:
        m = re.search(r'(?i)\blimit\b\s+(\d+)', s)
        if m:
            limit = int(m.group(1))
        m = re.search(r'(?i)\boffset\b\s+(\d+)', s)
        if m:
            offset = int(m.group(1))

    # Heuristic: if WHERE contains anforandetext LIKE '%term%' or LIKE '%term%' or anforandetext ILIKE, map to talks_search + SEARCH TOKENS
    use_view_search = False
    search_term = None
    like_m = re.search(r"(?i)(anforandetext)\s+like\s+'%([^%']+)%'", s)
    if like_m:
        use_view_search = True
        search_term = like_m.group(2)
    else:
        # also check generic LIKE on any column -- if column looks like text, map to view
        like_m = re.search(r"(?i)([a-zA-Z0-9_\.]+)\s+like\s+'%([^%']+)%'", s)
        if like_m and 'anforandetext' in like_m.group(1).lower():
            use_view_search = True
            search_term = like_m.group(2)

    # Start building AQL
    aql_lines = []
    if use_view_search:
        aql_lines.append(f"FOR {from_alias} IN {from_table}_search".replace('_search_search','_search'))  # if talks -> talks_search
    else:
        aql_lines.append(f"FOR {from_alias} IN {from_table}")

    # add join FOR loops
    for j_table, j_alias, j_on in joins:
        aql_lines.append(f"  FOR {j_alias} IN {j_table}")

    # Convert ON conditions and WHERE into FILTERs
    filters = []
    # Add join ON conditions as FILTERs
    for _, j_alias, j_on in joins:
        # j_on example: p._key = t.intressent_id
        cond = j_on.replace('=', '==')
        filters.append(cond.strip())

    if where_part:
        # Basic transformations: = stays ==, <> => !=, AND/OR uppercase, remove table aliases if needed
        cond = where_part
        cond = re.sub(r'(?i)\s+and\s+', ' AND ', cond)
        cond = re.sub(r'(?i)\s+or\s+', ' OR ', cond)
        cond = cond.replace('<>', '!=')
        cond = cond.replace('=', '==', 1) if ('=' in cond and '==' not in cond) else cond
        # don't blindly replace all = -> ==; do cautious: replace operators like ' = ' with ' == '
        cond = re.sub(r'\s=\s', ' == ', cond)
        # if LIKE already handled above, skip adding raw LIKE filter
        cond = re.sub(r"(?i)\s+like\s+'%[^']+%'", '', cond)
        filters.append(cond.strip())

    for f in filters:
        if f:
            aql_lines.append(f"  FILTER {f}")

    # If use_view_search, add SEARCH line
    if use_view_search and search_term:
        aql_lines.append(f"  SEARCH ANALYZER({from_alias}.anforandetext IN TOKENS(\"{search_term}\", \"text_sv\"), \"text_sv\")")

    # SORT / ORDER BY conversion
    if order_by:
        # simple conversion: replace table.column with same
        order_expr = order_by.replace(' desc', ' DESC').replace(' asc', ' ASC')
        aql_lines.append(f"  SORT {order_expr}")

    # GROUP BY -> COLLECT (simple support for COUNT(*) and grouping by single key)
    if group_by:
        group_cols = [c.strip() for c in group_by.split(',')]
        if len(group_cols) == 1 and re.search(r'(?i)count\(\s*\*\s*\)', select_part):
            key = group_cols[0]
            # map table.column -> alias.column if no alias
            aql_lines.append(f"  COLLECT key = {key} WITH COUNT INTO cnt")
            aql_lines.append("  SORT cnt DESC")
            aql_lines.append("  RETURN { key, count: cnt }")
            return "\n".join(aql_lines)

    # LIMIT/OFFSET
    if limit is not None:
        if offset is None:
            aql_lines.append(f"  LIMIT {limit}")
        else:
            aql_lines.append(f"  LIMIT {offset}, {limit}")

    # Build the RETURN clause
    # if select_part is COUNT(*) or COUNT(1)
    if re.search(r'(?i)^count\s*\(\s*\*\s*\)\s*$', select_part.strip()):
        aql_lines.append("  COLLECT WITH COUNT INTO c")
        aql_lines.append("  RETURN c")
    else:
        # map columns: simply return them as-is (user may need to adapt aliases)
        # Build a nice returned object if multiple columns
        cols = [c.strip() for c in select_part.split(',')]
        if len(cols) == 1:
            col = cols[0]
            aql_lines.append(f"  RETURN {col}")
        else:
            ret_items = []
            for c in cols:
                # try to make a key: if "t._id" -> _id, if "p.fodd_ar" -> p_fodd_ar
                key = re.sub(r'[^a-zA-Z0-9_]', '_', c)
                ret_items.append(f'"{key}": {c}')
            ret_map = "{ " + ", ".join(ret_items) + " }"
            aql_lines.append(f"  RETURN {ret_map}")

    return "\n".join(aql_lines)


# ---- small CLI for quick tests ----
if __name__ == "__main__":
    examples = [
        "SELECT COUNT(*) FROM talks WHERE anforandetext LIKE '%korallrev%';",
        "SELECT t._id, p.fodd_ar FROM talks t JOIN people p ON p._key = t.intressent_id WHERE t.year = 2016;",
        "SELECT parti, COUNT(*) FROM talks WHERE dok_datum >= '2016-01-01' AND dok_datum <= '2016-12-31' GROUP BY parti ORDER BY COUNT(*) DESC;"
    ]
    for sql in examples:
        print("SQL:", sql)
        try:
            print("AQL:\n", sql_to_aql(sql))
        except Exception as e:
            print("Error:", e)
        print("-" * 60)




# Example usage:
if __name__ == "__main__":
    pass