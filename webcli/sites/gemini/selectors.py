"""Every CSS selector llmcli gemini uses on gemini.google.com, in one place. Fix this file first when the UI moves.

Gemini is an Angular app: its custom element names (`rich-textarea`, `model-response`,
`message-content`, `gem-menu-item`) and `data-test-id` / `aria-label` attributes are the
durable hooks. Never select on `ng-*` or `mat-*` generated classes alone. aria-labels are
English: a Gemini UI in another language needs its own labels here.

A value may list alternatives separated by ", ". Where the order matters (menu rows, answer
text), llmcli gemini uses the first alternative that matches anything, so put the most specific first.
`python -m webcli.core.capture gemini` counts the matches for each alternative on the live page.

Sources (none checked against a llmcli gemini profile yet):
  [L] read off the live gemini.google.com page by hand on 2026-10-15
  [C] a live capture of gemini.google.com from 2026-07-29 (nuncaeslupus/ai-chat-exporter fixtures)
  [R] open-source Gemini automation and exporter code dated 2026-03 to 2026-10
Debug with `llmcli gemini <command> --headed`.
"""

# Composer. The Veo composer shares the aria-label but is contenteditable="false".
PROMPT_INPUT = (
    'rich-textarea .ql-editor[contenteditable="true"], '
    '[contenteditable="true"][aria-label="Enter a prompt for Gemini"], '
    '[contenteditable="true"][aria-label*="prompt" i]'
)  # [R]
SEND_BUTTON = 'button[aria-label="Send message"], .send-button button, button.send-button'  # [R]
STOP_BUTTON = (
    'button[aria-label="Stop response"], '
    'button[aria-label="Stop generating"], '
    'button[data-test-id="stop-button"], '
    'button:has(mat-icon[fonticon="stop"])'
)  # [R]: labels differ between sources

# Conversation
MODEL_RESPONSE = "model-response"  # [C]
RESPONSE_CONTENT = '[id^="model-response-message-content"], message-content .markdown, message-content'  # [C]
RESPONSE_BUSY = '[aria-busy="true"]'  # [C] finished answers carry aria-busy="false"
# A generated image in a response: generated-image > single-image > ... > img.image.loaded, whose src is a
# blob: of the 1024px preview. The text around it is only a googleusercontent placeholder. [L 2026-10-17]
GENERATED_IMAGE = "generated-image"

# Model ("mode") picker, in the composer. Its aria-label reads "Open mode picker, currently Flash-Lite" [L].
MODEL_BUTTON = 'button[data-test-id="bard-mode-menu-button"], bard-mode-switcher button'  # [L] [C]
MODEL_BUTTON_LABEL = '[data-test-id="bard-mode-menu-button"] .picker-primary-text'  # [C]
# The button opens gem-menu[data-test-id="gem-mode-menu"]: one gem-menu-item[data-mode-id] per model
# (class "selected" on the current one), then gem-menu-item[value="thinking_level"], the
# "Thinking level" submenu (aria-haspopup, Low/Medium/High), which has no data-mode-id and is not a model.
MODEL_ITEM = (
    '[data-test-id="gem-mode-menu"] gem-menu-item[data-mode-id], '
    'gem-menu-item[data-mode-id], '
    '[data-test-id="gem-mode-menu"] [role="menuitemradio"], '
    '[data-test-id="gem-mode-menu"] [role="menuitem"]'
)  # [L] [R]
MODEL_ITEM_TITLE = "span.label, .mode-title, .title-text"  # [L] span.label: "4 Argon"
MODEL_ITEM_DESCRIPTION = ".sublabel, .description, .subtitle, .mode-desc"  # [L] div.sublabel: "Complex tasks"
# Clicking the Thinking level row opens a second gem-menu (no data-test-id; its id is the row's aria-controls)
# with Low / Medium / High rows: span.label "High", div.sublabel "Extra thorough", class "selected" on the
# current one. Clicking a row closes both menus. The picker's label never shows the level. [L]
THINKING_ROW = 'gem-menu-item[value="thinking_level"]'  # [L]
THINKING_ITEM = 'gem-menu:not([data-test-id="gem-mode-menu"]) gem-menu-item'  # [L]

# Attachments: the "Upload & tools" button opens a menu whose "Upload files" item opens a file chooser.
UPLOAD_MENU_BUTTON = (
    'button[aria-label="Upload & tools"], '
    'button[aria-label="Open upload file menu"], '
    'button[aria-label*="upload" i]'
)  # [R]
UPLOAD_FILES_ITEM = (
    '[data-test-id="local-images-files-uploader-button"], '
    '[data-test-id="uploader-images-files-button-advanced"], '
    '[role="menuitem"]:has-text("Upload files")'
)  # [R]; :has-text is Playwright-only
FILE_INPUT = 'input[type="file"]'  # [R]: fallback when no file chooser opens
# One uploader-file-preview per attached file. It holds a spinner while uploading; a failed upload gets an error
# mark and the toast "File upload error, Couldn't upload". The send button stays enabled either way. [L 2026-10-18]
ATTACHMENT = "uploader-file-preview"
ATTACHMENT_BUSY = 'mat-progress-spinner, [role="progressbar"], [class*="loading" i]'
ATTACHMENT_ERROR = '[class*="error" i], [aria-label*="error" i], [aria-label*="failed" i]'
# The same menu's "Create image" tool (a menuitemcheckbox). Once on, the composer shows an "Images" chip
# (aria-label "Deselect Images") and every prompt makes an image. [L 2026-10-17]
CREATE_IMAGE_ITEM = '[role="menuitemcheckbox"]:has-text("Create image")'  # [L]; :has-text is Playwright-only
IMAGE_TOOL_CHIP = 'button[aria-label="Deselect Images"], button[aria-label^="Deselect Image"]'  # [L]
# The same menu's "Canvas" tool, another menuitemcheckbox; once on, the composer shows a "Canvas" chip. A reply
# that writes a doc shows an immersive-entry-chip (title, "Export", a preview) and opens immersive-editor, whose
# .ProseMirror[aria-label="Canvas editor"] holds the rendered doc. The doc's markdown comes from the transcript.
CANVAS_ITEM = '[role="menuitemcheckbox"]:has-text("Canvas")'  # [L 2026-10-18]; :has-text is Playwright-only
CANVAS_TOOL_CHIP = 'button[aria-label="Deselect Canvas"]'  # [L 2026-10-18]
CANVAS_CHIP = "immersive-entry-chip"  # [L 2026-10-18]
# Citation chips, one at the end of each cited paragraph or list item: sources-carousel-inline > source-inline-chip >
# button[aria-label="View source details for citations from MD: a.md and MD: a.md. Press Enter to open sources
# dialog."] > span.source-title "MD" (a file's extension, or a site name: "Wikipedia") + span.source-count "+ 1"
# when it holds more. Each cited sentence also gets an empty source-footnote (sup). [L 2026-10-18]
SOURCE_CHIP = "sources-carousel-inline"
SOURCE_CHIP_TITLE = ".source-title"

# Notices: toasts, dialogs, and banners. llmcli gemini reads the text of each visible one that is outside (and doesn't
# hold) the transcript or the prompt box; parse.limit_notice decides whether it is a usage limit.
NOTICE = (
    '[role="alert"], [role="alertdialog"], [role="dialog"], [role="status"], [aria-live], '
    'mat-snack-bar-container, mat-dialog-container, [role="banner"], '
    '[class*="banner"], [class*="toast"], [class*="snackbar"]'
)  # Angular Material / ARIA, not Gemini-specific
NOTICE_SKIP = 'user-query, model-response, rich-textarea, [contenteditable="true"]'

# Account: Google's account button, labelled "Google Account: Name  (email)".
ACCOUNT_BUTTON = 'a[aria-label^="Google Account"], a[href^="https://accounts.google.com/SignOutOptions"]'
