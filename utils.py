import re
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
            chunk_overlap: Number of characters to overlap between speech_chunks
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

    def _detect_separators(self, text: str) -> list["TextChunker.SeparatorInfo"]:
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

    def _split_by_separator(self, text: str, separator_pattern: str) -> list[str]:
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

    def _split_by_sentences(self, text: str) -> list[str]:
        """
        Split text into complete sentences, ensuring no mid-sentence breaks.
        Returns speech_chunks that respect sentence boundaries and tries to balance chunk sizes.
        """
        # Match sentence boundaries: year, exclamation, or question mark followed by space/newline
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

        # Build speech_chunks greedily first
        speech_chunks = []
        current_chunk = ""

        for sentence in sentences:
            # If adding this sentence would exceed limit and we have content, start new chunk
            if (
                current_chunk
                and len(current_chunk) + len(sentence) + 1 > self.chunk_limit
            ):
                speech_chunks.append(current_chunk)
                current_chunk = sentence
            else:
                # Add sentence to current chunk
                if current_chunk:
                    current_chunk += " " + sentence
                else:
                    current_chunk = sentence

        # Add final chunk
        if current_chunk:
            speech_chunks.append(current_chunk)

        # Now balance the speech_chunks: if the last chunk is too small, redistribute
        if len(speech_chunks) >= 2:
            last_chunk_size = len(speech_chunks[-1])
            # If last chunk is less than 40% of chunk_limit, try to rebalance
            if last_chunk_size < self.chunk_limit * 0.5:
                # Rebuild from sentences, distributing more evenly
                speech_chunks = self._balance_sentence_chunks(sentences)

        return speech_chunks if speech_chunks else [text]

    def _balance_sentence_chunks(self, sentences: list[str]) -> list[str]:
        """
        Distribute sentences across speech_chunks to minimize size variance.
        Uses a greedy approach that looks ahead to avoid tiny final speech_chunks.
        """
        if not sentences:
            return []

        total_length = sum(len(s) for s in sentences) + len(sentences) - 1
        # Estimate number of speech_chunks needed
        estimated_chunks = max(
            1, (total_length + self.chunk_limit - 1) // self.chunk_limit
        )
        target_size = total_length / estimated_chunks

        speech_chunks = []
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
                    speech_chunks.append(current_chunk)
                    current_chunk = sentence
                elif (
                    current_length >= target_size * 0.9
                    and remaining_text_length > target_size * 0.5
                ):
                    # We're near target and there's enough remaining - start new chunk
                    speech_chunks.append(current_chunk)
                    current_chunk = sentence
                else:
                    current_chunk += " " + sentence

        if current_chunk:
            speech_chunks.append(current_chunk)

        return speech_chunks

    def _merge_small_chunks(self, speech_chunks: list[str]) -> list[str]:
        """
        Merge speech_chunks that are smaller than the limit to optimize chunk sizes.
        Ensures the last chunk is not much smaller than the chunk_limit by merging it with the previous chunk if needed.
        """
        if not speech_chunks:
            return []

        merged = []
        current = speech_chunks[0]

        for next_chunk in speech_chunks[1:]:
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
            # Merge last two speech_chunks
            merged[-2] += merged[-1]
            merged.pop(-1)

        return merged

    def _recursive_split(
        self, text: str, separators: list[SeparatorInfo], separator_idx: int = 0
    ) -> list[str]:
        """
        Recursively split text using available separators until speech_chunks fit the limit.
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

        # Merge small consecutive speech_chunks
        result = self._merge_small_chunks(result)

        return result

    def chunk(self, text: str, verbose: bool = False, headings: str = "") -> list[str]:
        """
        Chunk the text using automatically detected separators.
        Always splits on complete sentences.

        Args:
            text: The text to chunk
            verbose: If True, print information about detected separators
            headings: Optional headings/context to prepend to each chunk (string)

        Returns:
            List of text speech_chunks, each optionally prefixed with the provided headings
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
            speech_chunks = self._split_by_sentences(text)
        else:
            # Recursively split using detected separators
            speech_chunks = self._recursive_split(text, separators)

        # Clean up speech_chunks
        speech_chunks = [chunk.strip() for chunk in speech_chunks if chunk.strip()]

        # Add headings to each chunk if provided
        if headings and headings.strip():
            # Ensure headings end with newlines for proper formatting
            formatted_headings = headings.strip()
            if not formatted_headings.endswith("\n"):
                formatted_headings += "\n\n"
            else:
                formatted_headings += "\n"

            # Prepend headings to each chunk
            speech_chunks = [f"#{formatted_headings}" + chunk for chunk in speech_chunks]

        if verbose:
            print(f"Created {len(speech_chunks)} speech_chunks")
            if headings:
                print(f"Added headings to each chunk: '{headings.strip()}'")
            print(f"Chunk sizes: {[len(c) for c in speech_chunks]}")

        return speech_chunks
