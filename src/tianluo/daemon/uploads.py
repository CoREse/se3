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
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Union

from tianluo.runtime_paths import UPLOADS_DIR_NAME, runtime_dir_name, uploads_dir

from . import protocol

logger = logging.getLogger(__name__)

# Re-exported so this module stays the one place the upload layer is read from,
# while the definitions live in ``runtime_paths`` — the module ``luo init`` and
# worktree creation can import without pulling in the daemon package.
__all__ = [
    "UPLOADS_DIR_NAME",
    "UploadError",
    "UploadStored",
    "sanitize_upload_filename",
    "store_upload",
    "uploads_dir",
]

#: Hex characters of the content sha256 used as the stored file's prefix. 12
#: hex chars (48 bits) makes an accidental collision between two distinct
#: attachments in one project effectively impossible, while keeping the path
#: short enough to stay readable inside a prompt.
HASH_PREFIX_LEN = 12

#: Cap on the sanitized name (the part after the hash prefix), counted in
#: **encoded UTF-8 bytes**. The filesystem's NAME_MAX is a byte budget (255 on
#: ext4/xfs/apfs), so a character count cannot express it: one CJK codepoint
#: costs 3 bytes and an emoji 4, which is how a name that looks short to a
#: zh-CN operator produced an ENAMETOOLONG on ``os.replace`` and surfaced as an
#: unexplainable "the machine could not save the file to disk". 200 bytes leaves
#: the whole ``<12 hex>_<name>`` component at 213 bytes — inside the limit with
#: room for the filesystems that budget below 255.
MAX_NAME_BYTES = 200

#: Fallback when sanitization consumes the entire name (e.g. the browser sent
#: ``".."`` or a string of control characters).
FALLBACK_NAME = "file"

#: Characters replaced with ``_`` on sight. Path separators would let a name
#: address another directory; the rest are either illegal on common filesystems
#: or invisible, which makes a path a human cannot verify by reading it.
_UNSAFE_CHARS = frozenset('/\\:*?"<>|\0')


def _truncate_utf8(text: str, limit: int) -> str:
    """Longest prefix of *text* that encodes to at most *limit* UTF-8 bytes.

    Cuts on a codepoint boundary — slicing the encoded bytes directly would
    leave a half-written multibyte sequence, i.e. a name the operator cannot
    type back and some tools refuse to open.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    # "ignore" drops exactly the trailing partial sequence the cut created; the
    # input is a valid str, so nothing else in it can fail to decode.
    return encoded[: max(limit, 0)].decode("utf-8", "ignore")


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


def sanitize_upload_filename(name: str) -> str:
    """Reduce a browser-supplied filename to a safe single path component.

    The input is fully attacker-controlled (it travels from a browser through
    the server to this machine's disk), so nothing about it is trusted: the
    directory part is discarded, anything that could redirect or hide the path
    is folded to ``_``, and the result is capped at :data:`MAX_NAME_BYTES`
    *encoded bytes* — the unit the filesystem itself counts in, so a name of
    3-byte CJK characters is truncated on the same budget as an ASCII one
    instead of sailing past it into an ENAMETOOLONG at write time. The
    extension is preserved across truncation because it is what tells the agent
    — and the web UI's thumbnail logic — what kind of file this is.
    """
    raw = unicodedata.normalize("NFC", str(name or ""))
    # Both separator styles are stripped regardless of the daemon's OS: the
    # name comes from a browser on an unknown platform, so a Windows client's
    # "C:\\shots\\a.png" must reduce to "a.png" on a posix daemon too.
    for sep in ("\\", "/"):
        raw = raw.rsplit(sep, 1)[-1]

    # Lone surrogates join the unsafe set: they cannot be UTF-8 encoded, so a
    # name carrying one would blow up the byte-budget truncation below (and
    # land on disk as a name no operator can retype).
    cleaned = "".join(
        "_"
        if (
            ch in _UNSAFE_CHARS
            or ord(ch) < 32
            or ch.isspace()
            or 0xD800 <= ord(ch) <= 0xDFFF
        )
        else ch
        for ch in raw
    )
    # Leading dots/dashes are dropped so a stored file can never turn into a
    # dotfile (invisible to the operator listing the directory) or read as a
    # command-line option to a tool the agent later runs on the path.
    cleaned = cleaned.lstrip("._-")

    if len(cleaned.encode("utf-8")) > MAX_NAME_BYTES:
        stem, dot, ext = cleaned.rpartition(".")
        ext_bytes = len(ext.encode("utf-8"))
        # The extension only survives if there is still room for a stem after
        # it; an absurdly long "extension" is treated as no extension at all
        # rather than eating the whole budget.
        if dot and stem and ext_bytes < MAX_NAME_BYTES - 1:
            cleaned = _truncate_utf8(stem, MAX_NAME_BYTES - ext_bytes - 1) + "." + ext
        else:
            cleaned = _truncate_utf8(cleaned, MAX_NAME_BYTES)
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
        #
        # The per-call random token is what makes that guarantee hold under
        # concurrency: two uploads of the same content+name (a double paste) run
        # on separate to_thread workers and would otherwise share one temp path,
        # so each would truncate and unlink the other's in-flight file —
        # publishing a half-written target, the exact state this dance exists to
        # prevent. O_EXCL turns the (astronomically unlikely) token collision
        # into a loud failure rather than that same corruption.
        #
        # The component is built from the *digest* and a short token, not from
        # target.name, because it must fit the same 255-byte filename budget
        # MAX_NAME_BYTES sizes the target for: appending a suffix to a 213-byte
        # target name would make a name that is perfectly storable as an
        # attachment unstorable as its own temp file, failing the upload for a
        # reason the operator can neither see nor act on.
        tmp = target_dir / f".{digest}.{os.getpid()}.{uuid.uuid4().hex[:8]}.part"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            with os.fdopen(fd, "wb") as fh:
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
