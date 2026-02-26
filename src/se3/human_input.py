"""Human input module for file-based human-to-agent communication.

Provides support for human input via prompt files as an alternative to MCP calls.
This complements the existing human-as-mcp system by allowing humans to write
requests in files that agents can process.

Features:
- File-based input format with structured sections
- Input archival after processing
- Listing pending inputs
- Response writing back to input files
"""

import re
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
import hashlib
import shutil


class InputStatus(Enum):
    """Human input status states."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"


@dataclass
class HumanInput:
    """Represents a human input file with metadata and content.

    Attributes:
        id: Unique identifier for the input
        file_path: Path to the input file
        status: Current status
        created: Creation timestamp
        title: Input title/subject
        context: Context provided by human
        request: What the human wants the agent to do
        response: Agent response content
        response_timestamp: When response was written
        metadata: Additional metadata
    """
    id: str
    file_path: Path
    status: InputStatus = InputStatus.PENDING
    created: datetime = field(default_factory=datetime.now)
    title: str = ""
    context: str = ""
    request: str = ""
    response: str = ""
    response_timestamp: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "id": self.id,
            "file_path": str(self.file_path),
            "status": self.status.value,
            "created": self.created.isoformat(),
            "title": self.title,
            "context": self.context,
            "request": self.request,
            "response": self.response,
            "response_timestamp": self.response_timestamp.isoformat() if self.response_timestamp else None,
            "metadata": self.metadata,
        }


class HumanInputStore:
    """Manages human input files with processing and archival.

    Features:
    - Structured input file format with sections
    - Input archival after processing
    - Pending input listing
    - Response writing back to files
    """

    # File patterns
    INPUT_FILE_PATTERN = "*.md"
    ARCHIVE_DIR = "archive"

    # Section headers
    CONTEXT_HEADERS = ["## Context", "## 上下文", "## Contexte"]
    REQUEST_HEADERS = ["## Request", "## 请求", "## Demande"]
    RESPONSE_HEADERS = ["## Response", "## 回复", "## Réponse"]

    def __init__(self, inputs_dir: Path):
        """Initialize the store.

        Args:
            inputs_dir: Directory containing human input files
        """
        self.inputs_dir = Path(inputs_dir)
        self.inputs_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir = self.inputs_dir / self.ARCHIVE_DIR
        self.archive_dir.mkdir(exist_ok=True)

        # State tracking for efficient change detection
        self._file_hashes: Dict[str, str] = {}
        self._file_mtimes: Dict[str, float] = {}
        self._input_cache: Dict[str, HumanInput] = {}

        # Pre-scan to populate initial state
        self._initialize_cache()

    def _initialize_cache(self):
        """Initialize cache with existing input files."""
        for filepath in self.inputs_dir.glob(self.INPUT_FILE_PATTERN):
            if filepath.is_file() and filepath.parent == self.inputs_dir:
                path_str = str(filepath)
                self._file_mtimes[path_str] = filepath.stat().st_mtime
                self._file_hashes[path_str] = self._compute_file_hash(filepath)
                inp = self.parse_input_file(filepath)
                if inp:
                    self._input_cache[path_str] = inp

    def _compute_file_hash(self, filepath: Path) -> str:
        """Compute hash of file content for change detection.

        Optimized to read only first 1KB for faster hash computation
        """
        with open(filepath, 'rb') as f:
            content = f.read(1024)
            return hashlib.sha256(content).hexdigest()[:16]

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
            if in_section and line.strip().startswith('## '):
                break

            if in_section:
                section_lines.append(line)

        return '\n'.join(section_lines).strip()

    def _extract_title(self, content: str) -> str:
        """Extract title from markdown content (H1 heading)."""
        match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
        if match:
            return match.group(1).strip()
        return ""

    def _generate_input_id(self, title: str, timestamp: Optional[datetime] = None) -> str:
        """Generate a unique input ID.

        Format: {YYYYMMDD}-{HHmmss}-{short-description}
        """
        ts = timestamp or datetime.now()
        # Clean title for filename
        clean_title = re.sub(r'[^\w\s-]', '', title.lower())
        clean_title = re.sub(r'[-\s]+', '-', clean_title).strip('-')[:30]
        return f"{ts.strftime('%Y%m%d-%H%M%S')}-{clean_title}"

    def _has_meaningful_response(self, response: str) -> bool:
        """Check if response section contains meaningful content.

        Args:
            response: Response section content

        Returns:
            True if response appears meaningful, False otherwise
        """
        if not response or not response.strip():
            return False

        # Remove HTML comments
        cleaned = re.sub(r'<!--.*?-->', '', response, flags=re.DOTALL).strip()
        if not cleaned:
            return False

        # Check for placeholder text
        placeholders = [
            "agent will fill this in",
            "agent fills this in",
            "to be filled",
            "placeholder",
        ]
        if any(p in cleaned.lower() for p in placeholders):
            return False

        # Check minimum length (at least 3 meaningful chars)
        meaningful = re.sub(r'[\s\W]', '', cleaned)
        if len(meaningful) < 3:
            return False

        return True

    def parse_input_file(self, filepath: Path) -> Optional[HumanInput]:
        """Parse a human input file.

        Args:
            filepath: Path to the input file

        Returns:
            HumanInput object or None if parsing fails
        """
        try:
            content = filepath.read_text(encoding="utf-8")

            # Extract title from H1 heading
            title = self._extract_title(content)

            # Extract sections
            context = self._extract_section(content, self.CONTEXT_HEADERS)
            request = self._extract_section(content, self.REQUEST_HEADERS)
            response = self._extract_section(content, self.RESPONSE_HEADERS)

            # Determine status based on meaningful response presence
            status = InputStatus.PENDING
            response_timestamp = None

            if self._has_meaningful_response(response):
                status = InputStatus.COMPLETED
                # Try to get response timestamp from file mtime
                stat = filepath.stat()
                response_timestamp = datetime.fromtimestamp(stat.st_mtime)

            # Parse creation time from filename or use file ctime
            created = datetime.fromtimestamp(filepath.stat().st_ctime)
            match = re.match(r'^(\d{8})-(\d{6})-', filepath.stem)
            if match:
                try:
                    created = datetime.strptime(
                        f"{match.group(1)}{match.group(2)}",
                        "%Y%m%d%H%M%S"
                    )
                except ValueError:
                    pass

            return HumanInput(
                id=filepath.stem,
                file_path=filepath,
                status=status,
                created=created,
                title=title,
                context=context,
                request=request,
                response=response,
                response_timestamp=response_timestamp,
            )
        except (IOError, ValueError) as e:
            return None

    def get_all_inputs(self) -> List[HumanInput]:
        """Get all human inputs (excluding archived).

        Returns:
            List of HumanInput objects
        """
        inputs = []

        for filepath in self.inputs_dir.glob(self.INPUT_FILE_PATTERN):
            # Skip files in subdirectories (like archive)
            if not filepath.is_file() or filepath.parent != self.inputs_dir:
                continue

            inp = self.parse_input_file(filepath)
            if inp:
                inputs.append(inp)

        return sorted(inputs, key=lambda i: i.created, reverse=True)

    def get_pending_inputs(self) -> List[HumanInput]:
        """Get all pending inputs (no response yet)."""
        return [i for i in self.get_all_inputs() if i.status == InputStatus.PENDING]

    def get_input(self, input_id: str) -> Optional[HumanInput]:
        """Get a specific input by ID.

        Args:
            input_id: The input ID (filename without extension)

        Returns:
            HumanInput object or None if not found
        """
        filepath = self.inputs_dir / f"{input_id}.md"
        if filepath.exists():
            return self.parse_input_file(filepath)
        return None

    def read_input_file(self, filepath: Path) -> Optional[HumanInput]:
        """Read and parse an input file from any path.

        Args:
            filepath: Path to the input file (can be outside inputs_dir)

        Returns:
            HumanInput object or None if parsing fails
        """
        if not filepath.exists():
            return None

        return self.parse_input_file(filepath)

    def write_response(self, input_id: str, response: str) -> bool:
        """Write a response to an input file.

        Args:
            input_id: The input ID
            response: The response content to write

        Returns:
            True if successful, False otherwise
        """
        filepath = self.inputs_dir / f"{input_id}.md"
        if not filepath.exists():
            return False

        try:
            content = filepath.read_text(encoding="utf-8")

            # Check if Response section exists
            response_section_found = any(
                header in content for header in self.RESPONSE_HEADERS
            )

            if response_section_found:
                # Replace existing response section
                lines = content.split('\n')
                new_lines = []
                in_response = False
                response_added = False

                for line in lines:
                    if any(line.strip().startswith(header) for header in self.RESPONSE_HEADERS):
                        in_response = True
                        new_lines.append(line)
                        new_lines.append(response)
                        response_added = True
                        continue

                    if in_response:
                        if line.strip().startswith('## '):
                            in_response = False
                            new_lines.append(line)
                        # Skip old response content
                        continue

                    new_lines.append(line)

                if not response_added:
                    # Append response section at end
                    new_lines.append(f"\n## Response\n{response}")

                new_content = '\n'.join(new_lines)
            else:
                # Append response section at end
                new_content = content.rstrip() + f"\n\n## Response\n{response}\n"

            filepath.write_text(new_content, encoding="utf-8")

            # Update cache
            inp = self.parse_input_file(filepath)
            if inp:
                self._input_cache[str(filepath)] = inp
                self._file_hashes[str(filepath)] = self._compute_file_hash(filepath)
                self._file_mtimes[str(filepath)] = filepath.stat().st_mtime

            return True
        except (IOError, ValueError):
            return False

    def archive_input(self, input_id: str) -> bool:
        """Archive a processed input file.

        Args:
            input_id: The input ID

        Returns:
            True if successful, False otherwise
        """
        filepath = self.inputs_dir / f"{input_id}.md"
        if not filepath.exists():
            return False

        try:
            # Move to archive directory
            archive_path = self.archive_dir / f"{input_id}.md"

            # Handle name collision
            counter = 1
            while archive_path.exists():
                archive_path = self.archive_dir / f"{input_id}_{counter}.md"
                counter += 1

            shutil.move(str(filepath), str(archive_path))

            # Update cache
            path_str = str(filepath)
            if path_str in self._file_hashes:
                del self._file_hashes[path_str]
            if path_str in self._file_mtimes:
                del self._file_mtimes[path_str]
            if path_str in self._input_cache:
                del self._input_cache[path_str]

            return True
        except (IOError, OSError):
            return False

    def process_input(self, input_id: str, processor: callable) -> Tuple[bool, str]:
        """Process an input with a custom processor function.

        Args:
            input_id: The input ID to process
            processor: Function that takes (context, request) and returns response string

        Returns:
            Tuple of (success, message)
        """
        inp = self.get_input(input_id)
        if not inp:
            return False, f"Input {input_id} not found"

        if inp.status != InputStatus.PENDING:
            return False, f"Input {input_id} is not pending (status: {inp.status.value})"

        try:
            # Process the input
            response = processor(inp.context, inp.request)

            # Write response
            if not self.write_response(input_id, response):
                return False, f"Failed to write response for {input_id}"

            # Archive the input
            if not self.archive_input(input_id):
                return False, f"Failed to archive {input_id}"

            return True, f"Successfully processed and archived {input_id}"
        except Exception as e:
            return False, f"Error processing {input_id}: {str(e)}"

    def create_input_template(self, title: str, context: str = "", request: str = "") -> Path:
        """Create a new input file template.

        Args:
            title: Input title
            context: Context for the input
            request: The request content

        Returns:
            Path to the created file
        """
        input_id = self._generate_input_id(title)
        filepath = self.inputs_dir / f"{input_id}.md"

        content = f"""# {title}

## Context
{context}

## Request
{request}

## Response
<!-- Agent will fill this in -->
"""

        filepath.write_text(content, encoding="utf-8")

        # Update tracking
        self._file_hashes[str(filepath)] = self._compute_file_hash(filepath)
        self._file_mtimes[str(filepath)] = filepath.stat().st_mtime

        return filepath


def discover_human_inputs(path: str = "human-inputs") -> List[Dict[str, Any]]:
    """Discover all human input files and their status.

    Legacy compatibility function. Uses the new HumanInputStore internally.

    Args:
        path: Path to human-inputs directory

    Returns:
        List of dicts with file info and parsed content
    """
    store = HumanInputStore(Path(path))
    inputs = store.get_all_inputs()

    result = []
    for inp in inputs:
        result.append({
            'file': inp.file_path.name,
            'path': str(inp.file_path),
            'status': inp.status.value,
            'created': inp.created.strftime('%Y-%m-%d'),
            'title': inp.title,
        })

    return result
