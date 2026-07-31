"""On-disk landing zone for files the operator attaches in the web UI.

This module is the *pure* half of the upload channel: no asyncio, no network,
no protocol frames — just "given a project root, a browser-supplied filename
and some bytes, put them somewhere an agent can read and tell me the relative
path". Keeping it free of the transport lets the security-critical parts (name
sanitization, directory containment, atomic replace) be tested directly against
the filesystem rather than through a WebSocket fixture.

Stored files land in ``<runtime dir>/uploads/`` and are named
``<sha256[:12]>_<sanitized original name>``. The hash prefix is what makes the
name collision-proof (two different files called ``screenshot.png`` coexist),
and the original name is what keeps the path readable — the path text ends up
verbatim in the operator's prompt, so an unreadable name would degrade the
prompt itself.
"""

from __future__ import annotations

import hashlib
import logging
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Union

from tianluo.runtime_paths import runtime_dir, runtime_dir_name

from . import protocol

logger = logging.getLogger(__name__)

#: Directory (under the project's runtime dir) holding operator attachments.
UPLOADS_DIR_NAME = "uploads"

#: Hex characters of the content sha256 used as the stored file's prefix. 12
#: hex chars (48 bits) makes an accidental collision between two distinct
#: attachments in one project effectively impossible, while keeping the path
#: short enough to stay readable inside a prompt.
HASH_PREFIX_LEN = 12

#: Cap on the sanitized name (the part after the hash prefix). Chosen so the
#: whole ``<12 hex>_<name>`` stays comfortably inside the 255-byte filename
#: limit even when every character is a 2-byte UTF-8 codepoint.
MAX_NAME_LEN = 100

#: Fallback when sanitization consumes the entire name (e.g. the browser sent
#: ``".."`` or a string of control characters).
FALLBACK_NAME = "file"

#: Characters replaced with ``_`` on sight. Path separators would let a name
#: address another directory; the rest are either illegal on common filesystems
#: or invisible, which makes a path a human cannot verify by reading it.
_UNSAFE_CHARS = frozenset('/\\:*?"<>|\0')


class UploadError(Exception):
    """A refused upload, carrying a stable :data:`protocol.UPLOAD_ERROR_CODES` code.

    The code — not the message — is the contract: the server maps it to an HTTP
    status and the web UI maps it to a localized string, so the prose here is
    only a diagnostic fallback that never reaches the operator untranslated.
    """

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


@dataclass
class UploadStored:
    """Result of a successful :func:`store_upload`.

    *path* is relative to the project root with posix separators — that exact
    string is what the operator's prompt carries and what the agent opens with
    the project root as its working directory. *deduplicated* reports that the
    identical content was already on disk, so nothing was written.
    """

    path: str
    size: int
    deduplicated: bool = False


def uploads_dir(project_root: Union[str, Path]) -> Path:
    """Return the attachments directory for *project_root*.

    Resolved through :func:`~tianluo.runtime_paths.runtime_dir` rather than
    hard-coding ``tianluo/``: a project still on the legacy ``se3/`` layout
    would otherwise get a stray top-level directory that no gitignore rule
    covers, and that stray directory would then be committed by accident.
    """
    return runtime_dir(project_root) / UPLOADS_DIR_NAME


def sanitize_upload_filename(name: str) -> str:
    """Reduce a browser-supplied filename to a safe single path component.

    The input is fully attacker-controlled (it travels from a browser through
    the server to this machine's disk), so nothing about it is trusted: the
    directory part is discarded, anything that could redirect or hide the path
    is folded to ``_``, and the result is length-capped. The extension is
    preserved across truncation because it is what tells the agent — and the
    web UI's thumbnail logic — what kind of file this is.
    """
    raw = unicodedata.normalize("NFC", str(name or ""))
    # Both separator styles are stripped regardless of the daemon's OS: the
    # name comes from a browser on an unknown platform, so a Windows client's
    # "C:\\shots\\a.png" must reduce to "a.png" on a posix daemon too.
    for sep in ("\\", "/"):
        raw = raw.rsplit(sep, 1)[-1]

    cleaned = "".join(
        "_" if (ch in _UNSAFE_CHARS or ord(ch) < 32 or ch.isspace()) else ch
        for ch in raw
    )
    # Leading dots/dashes are dropped so a stored file can never turn into a
    # dotfile (invisible to the operator listing the directory) or read as a
    # command-line option to a tool the agent later runs on the path.
    cleaned = cleaned.lstrip("._-")

    if len(cleaned) > MAX_NAME_LEN:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and stem and len(ext) < MAX_NAME_LEN - 1:
            cleaned = stem[: MAX_NAME_LEN - len(ext) - 1] + "." + ext
        else:
            cleaned = cleaned[:MAX_NAME_LEN]
        cleaned = cleaned.lstrip("._-")

    if not cleaned or cleaned in {".", ".."}:
        return FALLBACK_NAME
    return cleaned


def store_upload(
    project_root: Union[str, Path],
    filename: str,
    data: bytes,
) -> UploadStored:
    """Store *data* under *project_root*'s uploads directory and return its path.

    Raises :class:`UploadError` with a stable code when the content is over
    :data:`protocol.MAX_UPLOAD_BYTES` (``too_large``), when the sanitized name
    would still escape the uploads directory (``invalid_filename``), or when
    the write itself fails (``write_failed``).
    """
    payload = bytes(data or b"")
    # Checked before anything touches the disk: an oversized upload must cost
    # this machine no I/O at all, otherwise the size limit is not a defence.
    if len(payload) > protocol.MAX_UPLOAD_BYTES:
        raise UploadError(
            protocol.UPLOAD_ERR_TOO_LARGE,
            f"upload of {len(payload)} bytes exceeds the "
            f"{protocol.MAX_UPLOAD_BYTES}-byte limit",
        )

    root = Path(project_root)
    target_dir = uploads_dir(root)
    safe_name = sanitize_upload_filename(filename)
    digest = hashlib.sha256(payload).hexdigest()[:HASH_PREFIX_LEN]
    target = target_dir / f"{digest}_{safe_name}"

    # INVARIANT: the file written must be a direct child of the uploads
    # directory. Filename sanitization above is the intended defence, but it is
    # a transformation over an untrusted string; this comparison over *resolved*
    # paths is the independent check that a traversal surviving sanitization
    # (or an uploads dir symlinked elsewhere) still cannot place bytes outside
    # the attachment area. Failing closed here is always correct — a legitimate
    # attachment never needs to leave this directory.
    resolved_dir = target_dir.resolve()
    resolved_target = target.resolve()
    if resolved_target.parent != resolved_dir:
        raise UploadError(
            protocol.UPLOAD_ERR_INVALID_FILENAME,
            f"refusing to store {filename!r} outside {target_dir}",
        )

    # The name embeds the content hash, so an existing file at this exact path
    # already holds this exact content — the size check is only a cheap guard
    # against a truncated leftover. Re-reading and comparing 20 MB to confirm
    # what the name already states would buy nothing.
    try:
        existing = target.stat()
    except OSError:
        existing = None
    if existing is not None and existing.st_size == len(payload):
        return UploadStored(
            path=_relative_path(root, target),
            size=len(payload),
            deduplicated=True,
        )

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        # Written to a sibling temp file and moved into place: an agent may be
        # reading this directory at any moment, and os.replace is what
        # guarantees it observes either no file or the whole file — never the
        # prefix of an upload still in flight.
        tmp = target_dir / f".{target.name}.{os.getpid()}.part"
        try:
            with open(tmp, "wb") as fh:
                fh.write(payload)
            os.replace(tmp, target)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
    except UploadError:
        raise
    except OSError as exc:
        logger.warning("Failed to store upload %s under %s: %s", safe_name, root, exc)
        raise UploadError(
            protocol.UPLOAD_ERR_WRITE_FAILED,
            f"could not write the attachment: {exc}",
        ) from exc

    return UploadStored(
        path=_relative_path(root, target),
        size=len(payload),
        deduplicated=False,
    )


def _relative_path(project_root: Path, target: Path) -> str:
    """Project-relative posix path for *target* (e.g. ``tianluo/uploads/ab…_x.png``).

    Built from the runtime directory *name* rather than by relativizing the
    resolved absolute path: the daemon machine's absolute layout must not leak
    to the browser, and a symlinked project root would otherwise produce a path
    the agent cannot open from the project root it was given.
    """
    return Path(
        runtime_dir_name(project_root), UPLOADS_DIR_NAME, target.name
    ).as_posix()
