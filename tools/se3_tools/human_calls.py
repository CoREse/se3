"""Human calls detection and processing module for SE 3.0.

Provides optimized human call file handling with:
- Structured storage format with frontmatter
- Efficient change-based detection using file timestamps
- Response integrity validation
- Multi-language support
"""

import re
import os
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable, Set, Tuple
import hashlib


class CallStatus(Enum):
    """Human call status states."""
    PENDING = "pending"
    RESPONDED = "responded"
    PROCESSING = "processing"
    COMPLETED = "completed"
    EXPIRED = "expired"


class CallType(Enum):
    """Human call types."""
    DECISION = "decision"
    ACTION = "action"
    INFORMATION = "information"
    ESCALATION = "escalation"


class CallPriority(Enum):
    """Human call priority levels."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class HumanCall:
    """Represents a human call with metadata and content.

    Attributes:
        id: Unique identifier for the call
        file_path: Path to the call file
        call_type: Type of call (decision, action, information, escalation)
        priority: Priority level
        status: Current status
        created: Creation timestamp
        source: Source component (e.g., collab-orchestrator, worker)
        title: Call title/subject
        context: Call context/description
        response: Human response content
        response_timestamp: When response was received
        metadata: Additional metadata
    """
    id: str
    file_path: Path
    call_type: CallType = CallType.ACTION
    priority: CallPriority = CallPriority.MEDIUM
    status: CallStatus = CallStatus.PENDING
    created: datetime = field(default_factory=datetime.now)
    source: str = "unknown"
    title: str = ""
    context: str = ""
    response: str = ""
    response_timestamp: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "id": self.id,
            "file_path": str(self.file_path),
            "type": self.call_type.value,
            "priority": self.priority.value,
            "status": self.status.value,
            "created": self.created.isoformat(),
            "source": self.source,
            "title": self.title,
            "context": self.context,
            "response": self.response,
            "response_timestamp": self.response_timestamp.isoformat() if self.response_timestamp else None,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HumanCall":
        """Create from dictionary."""
        return cls(
            id=data["id"],
            file_path=Path(data["file_path"]),
            call_type=CallType(data.get("type", "action")),
            priority=CallPriority(data.get("priority", "medium")),
            status=CallStatus(data.get("status", "pending")),
            created=datetime.fromisoformat(data["created"]) if "created" in data else datetime.now(),
            source=data.get("source", "unknown"),
            title=data.get("title", ""),
            context=data.get("context", ""),
            response=data.get("response", ""),
            response_timestamp=datetime.fromisoformat(data["response_timestamp"]) if data.get("response_timestamp") else None,
            metadata=data.get("metadata", {}),
        )


class HumanCallStore:
    """Manages human call files with optimized detection.

    Features:
    - Efficient change detection using file system events and caching
    - Structured frontmatter format
    - Comprehensive response integrity validation
    - Multi-language support
    - Batch processing capabilities
    """

    # File patterns
    CALL_FILE_PATTERN = "*.md"
    RESPONDED_SUFFIX = ".responded"
    PROCESSING_SUFFIX = ".processing"
    COMPLETED_SUFFIX = ".completed"

    # Response section headers (multi-language)
    RESPONSE_HEADERS = ["### Response", "### 回复", "### Réponse"]
    CONTEXT_HEADERS = ["### Context", "### 上下文", "### Contexte"]

    # Default prompt markers that indicate no response yet
    DEFAULT_PROMPT_MARKERS = [
        "<!-- Human: write your response below -->",
        "<!-- 人类：请在下方输入您的回复 -->",
        "<!-- Humain: écrivez votre réponse ci-dessous -->",
    ]

    # Minimum response requirements
    MIN_RESPONSE_LENGTH = 10
    MIN_MEANINGFUL_CHARS = 5
    MAX_RESPONSE_LENGTH = 10000

    def __init__(self, calls_dir: Path):
        """Initialize the store.

        Args:
            calls_dir: Directory containing human call files
        """
        self.calls_dir = Path(calls_dir)
        self.calls_dir.mkdir(parents=True, exist_ok=True)

        # State tracking for efficient change detection
        self._last_scan_time: Optional[datetime] = None
        self._file_hashes: Dict[str, str] = {}
        self._file_mtimes: Dict[str, float] = {}
        self._call_cache: Dict[str, HumanCall] = {}

        # Pre-scan to populate initial state
        self._initialize_cache()

    def _initialize_cache(self):
        """Initialize cache with existing call files."""
        for filepath in self.calls_dir.glob(self.CALL_FILE_PATTERN):
            if filepath.is_file():
                path_str = str(filepath)
                self._file_mtimes[path_str] = filepath.stat().st_mtime
                self._file_hashes[path_str] = self._compute_file_hash(filepath)
                call = self.parse_call_file(filepath)
                if call:
                    self._call_cache[path_str] = call
        self._last_scan_time = datetime.now()

    def _compute_file_hash(self, filepath: Path) -> str:
        """Compute hash of file content for change detection.

        Optimized to read only first 1KB for faster hash computation
        """
        with open(filepath, 'rb') as f:
            content = f.read(1024)  # Read first 1KB for faster hashing
            return hashlib.sha256(content).hexdigest()[:16]

    def _parse_frontmatter(self, content: str) -> Tuple[Dict[str, Any], str]:
        """Parse YAML-like frontmatter from markdown content.

        Returns:
            Tuple of (frontmatter dict, body content)
        """
        frontmatter = {}
        body = content

        # Match frontmatter between --- markers
        match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)', content, re.DOTALL)
        if match:
            fm_text = match.group(1)
            body = match.group(2)

            # Parse key: value pairs
            for line in fm_text.strip().split('\n'):
                if ':' in line:
                    key, value = line.split(':', 1)
                    frontmatter[key.strip()] = value.strip()

        return frontmatter, body

    def _extract_section(self, content: str, headers: List[str]) -> str:
        """Extract content under a section header.

        Args:
            content: Full markdown content
            headers: List of possible section headers to match

        Returns:
            Section content or empty string if not found
        """
        lines = content.split('\n')
        in_section = False
        section_lines = []

        for line in lines:
            # Check if this is the target section header
            if any(line.strip().startswith(header) for header in headers):
                in_section = True
                continue

            # Check if we've reached the next section
            if in_section and line.strip().startswith('### '):
                break

            if in_section:
                section_lines.append(line)

        return '\n'.join(section_lines).strip()

    def _has_meaningful_response(self, content: str) -> bool:
        """Check if content contains a meaningful human response.

        Args:
            content: Response section content

        Returns:
            True if response appears meaningful, False otherwise
        """
        if not content:
            return False

        # Check for default prompt markers (indicates no real response)
        for marker in self.DEFAULT_PROMPT_MARKERS:
            if marker in content:
                return False

        # Check for HTML comments only (empty response)
        cleaned = re.sub(r'<!--.*?-->', '', content, flags=re.DOTALL).strip()
        if not cleaned:
            return False

        # Check minimum meaningful content length (at least 10 chars)
        if len(cleaned) < 10:
            return False

        return True

    def _generate_call_id(self, title: str, timestamp: Optional[datetime] = None) -> str:
        """Generate a unique call ID.

        Format: {YYYYMMDD}-{HHmmss}-{short-description}
        """
        ts = timestamp or datetime.now()
        # Clean title for filename
        clean_title = re.sub(r'[^\w\s-]', '', title.lower())
        clean_title = re.sub(r'[-\s]+', '-', clean_title).strip('-')[:30]
        return f"{ts.strftime('%Y%m%d-%H%M%S')}-{clean_title}"

    def create_call(
        self,
        title: str,
        context: str,
        call_type: CallType = CallType.ACTION,
        priority: CallPriority = CallPriority.MEDIUM,
        source: str = "unknown",
        language: str = "en",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> HumanCall:
        """Create a new human call file.

        Args:
            title: Call title
            context: Call context/description
            call_type: Type of call
            priority: Priority level
            source: Source component
            language: Language code (en, zh, etc.)
            metadata: Additional metadata

        Returns:
            Created HumanCall object
        """
        call_id = self._generate_call_id(title)
        filepath = self.calls_dir / f"{call_id}.md"

        # Load language labels from config module
        from .config import get_language_labels
        t = get_language_labels(language)

        # Build frontmatter and content
        content = f"""---
id: {call_id}
type: {call_type.value}
priority: {priority.value}
status: pending
created: {datetime.now().isoformat()}
source: {source}
language: {language}
---

## Request: {title}

**{t['type']}**: {call_type.value}
**{t['urgency']}**: {priority.value}
**{t['source']}**: {source}

### {t['context']}
{context}

### {t['response']}
{t['prompt']}
"""

        filepath.write_text(content, encoding="utf-8")

        call = HumanCall(
            id=call_id,
            file_path=filepath,
            call_type=call_type,
            priority=priority,
            status=CallStatus.PENDING,
            created=datetime.now(),
            source=source,
            title=title,
            context=context,
            metadata=metadata or {},
        )

        # Update tracking
        self._file_hashes[str(filepath)] = self._compute_file_hash(filepath)
        self._file_mtimes[str(filepath)] = filepath.stat().st_mtime

        return call

    def parse_call_file(self, filepath: Path) -> Optional[HumanCall]:
        """Parse a human call file.

        Args:
            filepath: Path to the call file

        Returns:
            HumanCall object or None if parsing fails
        """
        try:
            content = filepath.read_text(encoding="utf-8")
            frontmatter, body = self._parse_frontmatter(content)

            call_id = frontmatter.get("id") or filepath.stem

            # Determine status from file suffix or frontmatter
            status = CallStatus(frontmatter.get("status", "pending"))
            if filepath.suffix == ".responded" or filepath.name.endswith(".responded.md"):
                status = CallStatus.RESPONDED
            elif filepath.suffix == ".processing" or filepath.name.endswith(".processing.md"):
                status = CallStatus.PROCESSING

            # Extract response if present
            response = self._extract_section(body, self.RESPONSE_HEADERS)
            response_timestamp = None

            # If we have a meaningful response, update status
            if self._has_meaningful_response(response):
                status = CallStatus.RESPONDED
                # Try to get response timestamp from file mtime
                stat = filepath.stat()
                response_timestamp = datetime.fromtimestamp(stat.st_mtime)

            # Parse created timestamp
            created_str = frontmatter.get("created", "")
            try:
                created = datetime.fromisoformat(created_str) if created_str else datetime.fromtimestamp(filepath.stat().st_ctime)
            except ValueError:
                created = datetime.fromtimestamp(filepath.stat().st_ctime)

            return HumanCall(
                id=call_id,
                file_path=filepath,
                call_type=CallType(frontmatter.get("type", "action")),
                priority=CallPriority(frontmatter.get("priority", "medium")),
                status=status,
                created=created,
                source=frontmatter.get("source", "unknown"),
                title=self._extract_title(body) or filepath.stem,
                context=self._extract_section(body, self.CONTEXT_HEADERS),
                response=response,
                response_timestamp=response_timestamp,
                metadata={k: v for k, v in frontmatter.items() if k not in ["id", "type", "priority", "status", "created", "source", "language"]},
            )
        except (IOError, ValueError, KeyError) as e:
            return None

    def _extract_title(self, body: str) -> str:
        """Extract title from markdown body."""
        match = re.search(r'^## Request:\s*(.+)$', body, re.MULTILINE)
        if match:
            return match.group(1).strip()
        # Fallback: look for any level 2 heading
        match = re.search(r'^##\s*(.+)$', body, re.MULTILINE)
        if match:
            return match.group(1).strip()
        return ""

    def get_all_calls(self, include_completed: bool = False) -> List[HumanCall]:
        """Get all human calls.

        Args:
            include_completed: Whether to include completed calls

        Returns:
            List of HumanCall objects
        """
        calls = []

        for filepath in self.calls_dir.glob(self.CALL_FILE_PATTERN):
            # Skip non-call files
            if not filepath.is_file():
                continue

            call = self.parse_call_file(filepath)
            if call:
                if not include_completed and call.status in (CallStatus.COMPLETED, CallStatus.EXPIRED):
                    continue
                calls.append(call)

        return sorted(calls, key=lambda c: c.created, reverse=True)

    def get_pending_calls(self) -> List[HumanCall]:
        """Get all pending calls (no response yet)."""
        return [c for c in self.get_all_calls() if c.status == CallStatus.PENDING]

    def get_responded_calls(self) -> List[HumanCall]:
        """Get all calls with responses waiting to be processed."""
        return [c for c in self.get_all_calls() if c.status == CallStatus.RESPONDED]

    def get_changed_calls(self, force_rescan: bool = False) -> List[HumanCall]:
        """Get calls that have changed since last scan.

        Optimized change detection using:
        - File system metadata (mtime) for fast checks
        - Caching parsed calls for repeated access
        - Batch processing for efficiency

        Args:
            force_rescan: Force a full rescan even if no changes detected

        Returns:
            List of changed HumanCall objects
        """
        changed = []
        current_files = set()

        for filepath in self.calls_dir.glob(self.CALL_FILE_PATTERN):
            if not filepath.is_file():
                continue

            path_str = str(filepath)
            current_files.add(path_str)
            current_mtime = filepath.stat().st_mtime

            # Check if file is new or changed
            is_new = path_str not in self._file_mtimes
            is_modified = (
                path_str in self._file_mtimes and
                self._file_mtimes[path_str] != current_mtime
            )

            if is_new or is_modified or force_rescan:
                # Compute hash only if mtime changed (optimization)
                current_hash = self._compute_file_hash(filepath)
                content_changed = (
                    path_str in self._file_hashes and
                    self._file_hashes[path_str] != current_hash
                )

                if is_new or content_changed or force_rescan:
                    call = self.parse_call_file(filepath)
                    if call:
                        changed.append(call)
                        self._call_cache[path_str] = call
                self._file_hashes[path_str] = current_hash

            # Update tracking
            self._file_mtimes[path_str] = current_mtime

        # Handle deleted files
        for path_str in list(self._file_mtimes.keys()):
            if path_str not in current_files:
                del self._file_mtimes[path_str]
                if path_str in self._file_hashes:
                    del self._file_hashes[path_str]
                if path_str in self._call_cache:
                    del self._call_cache[path_str]

        self._last_scan_time = datetime.now()
        return changed

    def get_cached_call(self, filepath: Path) -> Optional[HumanCall]:
        """Get a call from cache or parse it if not cached.

        Args:
            filepath: Path to the call file

        Returns:
            Cached or parsed HumanCall object
        """
        path_str = str(filepath)
        if path_str in self._call_cache:
            # Check if file has been modified since last cache
            current_mtime = filepath.stat().st_mtime
            if self._file_mtimes.get(path_str, 0) == current_mtime:
                return self._call_cache[path_str]

        # Parse and cache
        call = self.parse_call_file(filepath)
        if call:
            self._call_cache[path_str] = call
            self._file_mtimes[path_str] = filepath.stat().st_mtime
            self._file_hashes[path_str] = self._compute_file_hash(filepath)
        return call

    def mark_processing(self, call: HumanCall) -> None:
        """Mark a call as being processed.

        Args:
            call: The call to mark
        """
        call.status = CallStatus.PROCESSING
        new_path = call.file_path.with_suffix(".processing.md")

        # Rename file to indicate processing state
        if call.file_path.exists():
            call.file_path.rename(new_path)
            call.file_path = new_path

        # Update tracking
        old_path_str = str(call.file_path.with_suffix(".md"))
        if old_path_str in self._file_hashes:
            del self._file_hashes[old_path_str]
        if old_path_str in self._file_mtimes:
            del self._file_mtimes[old_path_str]
        if old_path_str in self._call_cache:
            del self._call_cache[old_path_str]

        self._file_hashes[str(new_path)] = self._compute_file_hash(new_path)
        self._file_mtimes[str(new_path)] = new_path.stat().st_mtime
        self._call_cache[str(new_path)] = call

    def mark_completed(self, call: HumanCall) -> None:
        """Mark a call as completed.

        Args:
            call: The call to mark
        """
        call.status = CallStatus.COMPLETED
        new_path = call.file_path.with_suffix(".completed.md")

        # Rename file to indicate completed state
        if call.file_path.exists():
            call.file_path.rename(new_path)
            call.file_path = new_path

        # Update tracking
        old_path_str = str(call.file_path.with_suffix(".processing.md"))
        if old_path_str in self._file_hashes:
            del self._file_hashes[old_path_str]
        if old_path_str in self._file_mtimes:
            del self._file_mtimes[old_path_str]
        if old_path_str in self._call_cache:
            del self._call_cache[old_path_str]

        self._call_cache[str(new_path)] = call

    def process_responded_calls(self, processor: Callable[[HumanCall], bool]) -> List[HumanCall]:
        """Process all responded calls with a custom processor.

        Args:
            processor: Function to process each call. Returns True if processing succeeded.

        Returns:
            List of calls that were successfully processed
        """
        processed = []
        responded_calls = self.get_responded_calls()

        for call in responded_calls:
            self.mark_processing(call)
            try:
                success = processor(call)
                if success:
                    self.mark_completed(call)
                    processed.append(call)
                else:
                    # Revert to responded state if processing failed
                    self._revert_to_responded(call)
            except Exception as e:
                self._revert_to_responded(call)
                raise e

        return processed

    def _revert_to_responded(self, call: HumanCall) -> None:
        """Revert a call from processing state to responded state.

        Args:
            call: The call to revert
        """
        call.status = CallStatus.RESPONDED
        new_path = call.file_path.with_suffix(".md")

        if call.file_path.exists():
            call.file_path.rename(new_path)
            call.file_path = new_path

        # Update tracking
        self._file_hashes[str(new_path)] = self._compute_file_hash(new_path)
        self._file_mtimes[str(new_path)] = new_path.stat().st_mtime
        self._call_cache[str(new_path)] = call

    def batch_validate_responses(self, calls: List[HumanCall]) -> List[Tuple[HumanCall, bool, str]]:
        """Validate multiple responses in batch.

        Args:
            calls: List of calls to validate

        Returns:
            List of (call, is_valid, reason) tuples
        """
        results = []
        for call in calls:
            is_valid, reason = self.validate_response(call)
            results.append((call, is_valid, reason))
        return results

    def validate_response(self, call: HumanCall) -> Tuple[bool, str]:
        """Validate a human response for completeness and accuracy.

        Comprehensive validation including:
        - Presence of response content
        - Absence of default prompt markers
        - Length checks (minimum and maximum)
        - Content quality checks
        - Structural validity

        Args:
            call: The call to validate

        Returns:
            Tuple of (is_valid, reason)
        """
        if not call.response:
            return False, "No response content"

        # Check for default prompt markers
        for marker in self.DEFAULT_PROMPT_MARKERS:
            if marker in call.response:
                return False, "Response contains default prompt marker"

        # Clean response by removing comments
        cleaned = re.sub(r'<!--.*?-->', '', call.response, flags=re.DOTALL).strip()

        # Check minimum length
        if len(cleaned) < self.MIN_RESPONSE_LENGTH:
            return False, f"Response too short ({len(cleaned)} chars, minimum {self.MIN_RESPONSE_LENGTH})"

        # Check maximum length
        if len(cleaned) > self.MAX_RESPONSE_LENGTH:
            return False, f"Response too long ({len(cleaned)} chars, maximum {self.MAX_RESPONSE_LENGTH})"

        # Check for meaningful content (not just whitespace or punctuation)
        meaningful_chars = re.sub(r'[\s\W]', '', cleaned)
        if len(meaningful_chars) < self.MIN_MEANINGFUL_CHARS:
            return False, "Response lacks meaningful content"

        # Check for repetitive content
        if self._is_repetitive(cleaned):
            return False, "Response contains repetitive or boilerplate content"

        # Check for valid response structure
        if not self._has_valid_structure(cleaned):
            return False, "Response has invalid structure"

        return True, "Response is valid"

    def _is_repetitive(self, content: str) -> bool:
        """Check if content is repetitive or boilerplate.

        Args:
            content: Response content to check

        Returns:
            True if content is repetitive
        """
        # Check for repeated patterns
        patterns = [
            r'(\w+)(\s+\1){3,}',  # Same word repeated 4+ times
            r'(.{5,})(\1){2,}',    # 5+ char sequence repeated 3+ times
        ]

        for pattern in patterns:
            if re.search(pattern, content, re.IGNORECASE):
                return True

        # Check for excessive punctuation
        punctuation_ratio = sum(1 for c in content if c in '.,!?;:') / len(content)
        if punctuation_ratio > 0.3:
            return True

        return False

    def _has_valid_structure(self, content: str) -> bool:
        """Check if response has a valid structure.

        Args:
            content: Response content to check

        Returns:
            True if content has valid structure
        """
        # At least one sentence with subject and verb structure
        # Simple check for common sentence patterns
        has_sentence = re.search(r'[A-Za-z]\w*.*?[.!?]', content)
        if has_sentence:
            return True

        # For non-English responses, check for basic structure
        if len(re.sub(r'[\s\W]', '', content)) >= 10:
            return True

        return False

    def get_stale_calls(self, timeout_days: int = 7) -> List[HumanCall]:
        """Get calls that have been pending for too long.

        Args:
            timeout_days: Days after which a call is considered stale

        Returns:
            List of stale HumanCall objects
        """
        cutoff = datetime.now() - timedelta(days=timeout_days)
        return [
            c for c in self.get_all_calls()
            if c.status == CallStatus.PENDING and c.created < cutoff
        ]

    def export_to_json(self, output_path: Path, include_completed: bool = False) -> None:
        """Export all calls to JSON.

        Args:
            output_path: Path to write JSON file
            include_completed: Whether to include completed calls
        """
        calls = [c.to_dict() for c in self.get_all_calls(include_completed)]
        output_path.write_text(json.dumps(calls, indent=2, default=str), encoding="utf-8")


# Backwards compatibility: maintain the old discover_human_calls function
def discover_human_calls(path: str = "human-calls") -> List[Dict[str, Any]]:
    """Discover all human call files and their status.

    Legacy compatibility function. Uses the new HumanCallStore internally.

    Args:
        path: Path to human-calls directory

    Returns:
        List of dicts with file info and parsed frontmatter
    """
    store = HumanCallStore(Path(path))
    calls = store.get_all_calls(include_completed=True)

    result = []
    for call in calls:
        result.append({
            'file': call.file_path.name,
            'path': str(call.file_path),
            'type': call.call_type.value,
            'priority': call.priority.value,
            'status': call.status.value,
            'created': call.created.strftime('%Y-%m-%d'),
            'title': call.title,
        })

    return result
