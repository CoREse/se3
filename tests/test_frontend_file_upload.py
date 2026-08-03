"""Pytest bridge for the WebUI file-attachment upload feature (Group G6).

The behavioural assertions live in the Node suites
``tests/frontend/file_upload.test.mjs`` (the strip and the prompt text) and
``tests/frontend/inline_upload_images.test.mjs`` (reading a stored attachment
back as a conversation thumbnail), both registered into the shared harness
``tests/frontend/test_app_pure.mjs`` (they need that harness's DOM stub, so
unlike ``i18n_render_switch.test.mjs`` they are not standalone-runnable). This
module:

  1. runs the harness and asserts the upload cases actually executed — a suite
     that silently stopped being registered would otherwise still "pass";
  2. statically guards the parts of the feature that live in shipped assets and
     therefore cannot be reached from Node:

     * the 20 MiB bound is duplicated in ``app.js`` because the browser cannot
       import ``protocol.py``; nothing but this assertion stops the two copies
       from drifting apart, after which the browser would either wave through a
       file the daemon refuses or refuse one it would have accepted;
     * every ``upload.*`` / ``common.size.*`` key the app references exists in
       BOTH language packs (en-US is the baseline superset; a missing zh-CN key
       silently falls back to English mid-sentence);
     * ``index.html`` carries the per-scope element ids the wiring binds to, and
       ``style.css`` carries the strip/thumbnail/drop-target rules — a rename on
       either side unbinds the feature with no JS error to show for it.

The Node leg is skipped when ``node`` is not on PATH; the static guards always
run. The suite is runnable by hand via ``node tests/frontend/test_app_pure.mjs``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tianluo.daemon import protocol


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "src" / "tianluo" / "server" / "static"
APP_JS = STATIC_DIR / "app.js"
STYLE_CSS = STATIC_DIR / "style.css"
INDEX_HTML = STATIC_DIR / "index.html"
I18N_DIR = STATIC_DIR / "i18n"
EN_JSON = I18N_DIR / "en-US.json"
ZH_JSON = I18N_DIR / "zh-CN.json"
UPLOAD_TEST = REPO_ROOT / "tests" / "frontend" / "file_upload.test.mjs"
INLINE_IMAGE_TEST = REPO_ROOT / "tests" / "frontend" / "inline_upload_images.test.mjs"
HARNESS = REPO_ROOT / "tests" / "frontend" / "test_app_pure.mjs"

# The ids the two upload scopes bind to. Respond and interject share the docked
# textarea, so the three prompt inputs need only two scopes' worth of markup.
UPLOAD_ELEMENT_IDS = (
    "nt-task",
    "nt-attachments",
    "nt-file-input",
    "nt-attach-btn",
    "flow-reply-input",
    "flow-attachments",
    "flow-file-input",
    "flow-attach-btn",
)

# Every key the feature can paint, listed outright so deleting a key AND its
# only reference still fails here rather than quietly shrinking the UI.
REQUIRED_I18N_KEYS = (
    "upload.attachTitle",
    "upload.attachmentsLabel",
    "upload.removeTitle",
    "upload.cancelTitle",
    "upload.placeholder",
    "upload.uploading",
    "upload.errTooLarge",
    "upload.errFailed",
    "upload.errNoTarget",
    "upload.errNoFlow",
    "upload.errUnregisteredProject",
    "upload.errUnsupportedDaemon",
    "upload.errNotConnected",
    "upload.errTimeout",
    "upload.errWriteFailed",
    "upload.errInvalidFilename",
    "upload.errNetwork",
    "upload.errPending",
    "upload.targetChanged",
    "common.size.bytes",
    "common.size.kb",
    "common.size.mb",
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. The 20 MiB bound is one number wearing two hats
# ---------------------------------------------------------------------------
def test_app_js_upload_limit_matches_protocol():
    js = APP_JS.read_text(encoding="utf-8")
    m = re.search(r"const MAX_UPLOAD_BYTES\s*=\s*([0-9][0-9_\s*]*?);", js)
    assert m, "app.js no longer declares a literal `const MAX_UPLOAD_BYTES = ...;`"
    factors = [int(part.replace("_", "")) for part in m.group(1).split("*")]
    value = 1
    for factor in factors:
        value *= factor
    assert value == protocol.MAX_UPLOAD_BYTES, (
        "the browser pre-flight limit drifted from protocol.MAX_UPLOAD_BYTES: "
        f"app.js says {value}, protocol.py says {protocol.MAX_UPLOAD_BYTES}"
    )
    # A sanity floor on the shape of the constant itself, so a future edit to
    # protocol.py that also breaks the parse above cannot pass both ways.
    assert value == 20 * 1024 * 1024


# ---------------------------------------------------------------------------
# 2. i18n coverage — en-US baseline plus a complete zh-CN
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("lang_file", [EN_JSON, ZH_JSON], ids=["en-US", "zh-CN"])
def test_upload_keys_present_in_both_language_packs(lang_file: Path):
    data = _load(lang_file)
    missing = [k for k in REQUIRED_I18N_KEYS if k not in data]
    assert missing == [], f"{lang_file.name} is missing upload keys: {missing}"
    empty = [k for k in REQUIRED_I18N_KEYS if not str(data[k]).strip()]
    assert empty == [], f"{lang_file.name} has blank strings for: {empty}"


def test_every_referenced_upload_key_is_translated():
    """No ``upload.*`` / ``common.size.*`` key may be referenced without both
    packs carrying it — an unresolved key paints the raw token at the user."""
    en, zh = _load(EN_JSON), _load(ZH_JSON)
    js = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    referenced = set(re.findall(r'"((?:upload|common\.size)\.[A-Za-z0-9_]+)"', js))
    referenced |= set(
        re.findall(
            r'data-i18n(?:-[a-z-]+)?="((?:upload|common\.size)\.[A-Za-z0-9_]+)"',
            html,
        )
    )
    assert referenced, "no upload i18n keys found — did the feature move out of app.js?"
    assert sorted(k for k in referenced if k not in en) == []
    assert sorted(k for k in referenced if k not in zh) == []


def test_placeholder_string_keeps_its_interpolation_slots():
    """The in-flight marker must name the file and carry the per-paste sequence
    number: two pastes of the same file mint the same token otherwise, and the
    first answer would replace the second paste's marker."""
    for lang_file in (EN_JSON, ZH_JSON):
        value = _load(lang_file)["upload.placeholder"]
        assert "{name}" in value and "{seq}" in value, (
            f"{lang_file.name} upload.placeholder lost a slot: {value!r}"
        )


# ---------------------------------------------------------------------------
# 3. index.html / style.css — the markup the wiring binds to
# ---------------------------------------------------------------------------
def test_index_html_carries_both_upload_scopes():
    html = INDEX_HTML.read_text(encoding="utf-8")
    for element_id in UPLOAD_ELEMENT_IDS:
        assert f'id="{element_id}"' in html, f"index.html is missing id={element_id!r}"
    # The native pickers ship hidden — the styled attach button is what opens
    # them — and accept anything, since images and plain files share one channel.
    assert html.count('type="file"') == 2
    assert 'id="nt-file-input" class="hidden"' in html
    assert 'id="flow-file-input" class="hidden"' in html


def test_reset_reply_box_clears_the_docked_strip():
    """Blanking the docked textarea must retire the strip that mirrors it.

    ``resetReplyBox`` runs on every flow open/switch. Left unwired, flow A's
    rows would sit under flow B's input advertising paths that are not in its
    text (and live in another project), and their preview object URLs would
    never be revoked. Guarded statically because the function drives half the
    reply dock, which the DOM stub does not model.
    """
    js = APP_JS.read_text(encoding="utf-8")
    body = js.split("function resetReplyBox()", 1)
    assert len(body) == 2, "app.js no longer declares `function resetReplyBox()`"
    body = body[1].split("\nfunction ", 1)[0]
    assert 'clearAttachments("flow-attachments")' in body, (
        "resetReplyBox() blanks #flow-reply-input without clearing the "
        "attachment strip that mirrors it"
    )


def test_new_task_target_selects_recheck_the_attachments():
    """Both New Task target selects must re-run the attachment check.

    An uploaded path resolves only under the project it was written into, and
    ``submitNewTask`` ships the task text verbatim — so a machine/project change
    that leaves the old paths in ``#nt-task`` publishes a prompt naming files
    that do not exist where the flow runs, with no error anywhere. The discard
    itself is covered by the Node suite; only the two ``init()`` bindings that
    trigger it live outside the DOM stub's reach.
    """
    js = APP_JS.read_text(encoding="utf-8")
    body = js.split("\nfunction init()", 1)
    assert len(body) == 2, "app.js no longer declares `function init()`"
    body = body[1].split("\nfunction ", 1)[0]
    for element_id in ("nt-machine", "nt-project"):
        anchor = f'$("{element_id}").addEventListener("change"'
        assert anchor in body, f"init() no longer binds the {element_id} change handler"
        handler = body.split(anchor, 1)[1].split("addEventListener", 1)[0]
        assert "syncNewTaskUploadTarget()" in handler, (
            f"a {element_id} change re-points the task without re-checking the "
            "attachments its paths belong to"
        )


def test_style_css_styles_the_strip_and_the_drop_target():
    css = STYLE_CSS.read_text(encoding="utf-8")
    for selector in (
        ".attachment-strip",
        ".attachment-thumb",
        ".attachment-icon",
        ".attachment-remove",
        ".drop-active",
        # The stored-name line: app.js emits the inner span purely so this rule
        # can slide it on hover, so a dropped rule leaves dead markup and a
        # stored name nothing can reveal in full.
        ".attachment-size-text",
        "attachment-scroll",
        # The conversation's inline thumbnails. Without the size cap a single
        # screenshot renders at its native height and buries the turn it belongs
        # to, so the rule is load-bearing rather than decorative.
        ".inline-uploads",
        ".inline-upload-link",
        ".inline-upload-img",
    ):
        assert selector in css, f"style.css is missing the {selector} rule"


# ---------------------------------------------------------------------------
# 4. Node suite — the upload cases actually run and pass
# ---------------------------------------------------------------------------
def test_upload_node_module_present_and_registered():
    assert UPLOAD_TEST.is_file(), f"missing {UPLOAD_TEST}"
    harness = HARNESS.read_text(encoding="utf-8")
    assert "registerFileUploadTests" in harness, (
        "file_upload.test.mjs is no longer registered in test_app_pure.mjs; "
        "the whole suite would stop running without failing anything"
    )


def test_inline_image_node_module_present_and_registered():
    assert INLINE_IMAGE_TEST.is_file(), f"missing {INLINE_IMAGE_TEST}"
    harness = HARNESS.read_text(encoding="utf-8")
    assert "registerInlineUploadImagesTests" in harness, (
        "inline_upload_images.test.mjs is no longer registered in "
        "test_app_pure.mjs; the whole suite would stop running without "
        "failing anything"
    )


def test_frontend_file_upload_node_suite_passes():
    """Run the shared frontend harness and confirm the upload cases executed.

    Skipped if ``node`` is not available on PATH; still runnable by hand via
    ``node tests/frontend/test_app_pure.mjs``.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")
    result = subprocess.run(
        [node, str(HARNESS)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=180,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"frontend test runner exited {result.returncode}:\n{combined}"
    )
    for needle in (
        # pure helpers
        "G6 validateUploadFile: the limit itself passes, one byte over does not",
        "G6 formatFileSize: unit thresholds, and the .0 tail is dropped",
        "G6 isImageFile: the MIME type wins, the extension is the fallback",
        "G6 uploadErrorKey: every wire code maps, and an unknown one still reads",
        "G6 replaceTokenOnce: literal, first occurrence, miss is a no-op",
        "G6 removePathOnce: takes the one occurrence it put there, no more",
        "G6 insertAtCaret: lands at the caret and leaves it past the insert",
        "G6 attachmentRowModel: removable only once a path is in the text",
        # interaction wiring
        "G6 a real paste gesture uploads once and lands at the caret",
        "G5 performUpload: the path lands exactly where it was pasted",
        "G5 performUpload: a failure leaves no placeholder and no half path",
        "G5 performUpload: a placeholder deleted mid-flight is not resurrected",
        "G5 startUploads: an over-sized file never leaves the browser",
        "G5 startUploads: an unresolved target toasts and sends nothing",
        "G5 renderAttachmentStrip: an image row renders a thumbnail",
        "G5 renderAttachmentStrip: a plain file renders icon + name + size",
        # the stored path on the row — the only thing separating two pasted
        # screenshots, both of which the clipboard calls "image.png"
        "G4 attachmentRowModel: the stored name is exposed only once it exists",
        "G4 renderAttachmentStrip: a landed row shows and titles its stored path",
        "G4 renderAttachmentStrip: a failed row keeps its reason as the tooltip",
        "G5 removeAttachment: deletes the path from the text and nothing else",
        "G5 clearAttachments: empties the strip and recycles preview URLs",
        # escaping a stalled upload — an in-flight row shuts the submit gate
        "G5 renderAttachmentStrip: an in-flight row shows status and offers cancel",
        "G5 cancelAttachment: a stalled upload releases the send gate and keeps the draft",
        "G5 cancelAttachment: an answer arriving after the cancel changes nothing",
        "G5 performUpload: a request that never answers times out instead of pinning the row",
        "G5 bindUploadScope: both scopes bind all four gestures",
        # the submit gate: a placeholder must never ship as prompt prose
        "G6 pendingUploadNames: only the rows still in flight count",
        "G5 submitReply: an in-flight paste blocks the send instead of shipping the token",
        "G5 submitNewTask: an in-flight paste blocks Publish",
        "G5 submitNewTask: publishes once the path has landed",
        # the conversation's other half: the stored path is read back and shown
        # as a picture WITHOUT the path text ever leaving the message
        "G5 extractUploadImagePaths: both layout prefixes are recognised",
        "G5 renderInlineUploadImages: one anchor-wrapped img per path",
        "G5 renderInlineUploadImages: a failed load hides itself, never breaks",
        "G5 assistant turn: the thumbnail joins the path text, never replaces it",
        "G5 user prompt (marker split): the user's own half is scanned",
        "G5 no flow open: the conversation degrades to plain path text",
    ):
        assert needle in combined, (
            f"expected upload check {needle!r} in node output:\n{combined}"
        )
