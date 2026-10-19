"""chatgpt.com UI selectors, in one place. VERIFIED 2026-10-18 against the live page (model_picker_version 2).

The page has almost no data-testids left, so these lean on aria-labels. Fix this file first when the page changes;
`--headed` shows the window.
"""

PROMPT_INPUT = '.ProseMirror[contenteditable="true"]'  # aria-label "Ask ChatGPT"
SEND_BUTTON = 'form button[aria-label="Send"], #composer-submit-button, button[data-testid="send-button"]'
STOP_BUTTON = 'button[aria-label="Stop streaming"], button[aria-label^="Stop"], button[data-testid="stop-button"]'
FILE_INPUT = 'input[type="file"][aria-label="Attach files"], input[type="file"]:not([accept])'
MODEL_BUTTON = 'button[aria-label="Select ChatGPT model"]'  # its text is the preset: Instant / Medium / High
MENU = '[role="menu"]'
VERSION_ITEM = '[role="menu"] [role="menuitemradio"]'  # "GPT-6", "GPT-5.6 Sol"; aria-checked marks the current one
POWER_SLIDER = '[role="menu"] [role="slider"]'  # aria-valuenow: the preset's index (0 Instant, 1 Medium, 2 High)
DIALOG = '[role="dialog"]'  # announcements ("You now have access to Health in ChatGPT"); Escape closes them
# Notices outside the transcript (toasts, banners, dialogs): where a usage-cap notice shows up.
NOTICE = '[role="alert"], [role="status"], [role="dialog"], [aria-live="polite"], [aria-live="assertive"]'
NOTICE_SKIP = '[data-message-author-role], .ProseMirror, [role="menu"]'
