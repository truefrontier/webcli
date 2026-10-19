# Fix spec: ChatGPT fails Cloudflare headless on a freshly-logged-in profile

## Symptom (live, 2026-10-19)
Kevin signed in to `chatgpt:default` today via `webcli identity login chatgpt:default` (a visible window; sign-in completed, chat page shown). But EVERY headless `llmcli chatgpt` command now exits 4 `hidden_browser_blocked` ("Cloudflare needs a visible check"): `llmcli chatgpt status`, `llmcli chatgpt -p "..."`, etc. Gemini and Muse work fine.

## What I already proved (do not re-litigate these)
- The OLD cgpt tool (`~/Sites/truefrontier/chatgpt-web-cli`, separate ESTABLISHED profile `~/.chatgpt-web-cli`) passes Cloudflare **headless right now** on this same machine/IP, with code that is byte-for-byte equivalent to webcli's. So the machine, IP, Chrome (155), UA, and launch flags are all fine. `cgpt -p "..."` → exit 0.
- webcli's headless UA == old cgpt's headless UA (identical string, "Chrome/155.0.0.0", no "HeadlessChrome").
- webcli's launch options == old cgpt's (channel=chrome, STEALTH=["--disable-blink-features=AutomationControlled"], ignore_default_args drops --enable-automation and --use-mock-keychain, same viewport, same service_workers policy).
- The Cloudflare challenge-detection/grace code is identical (CHALLENGE_GRACE headless=10, wait_out_challenge). Bumping the grace to 45s did NOT help (still exits 4 after 46s). So it is not a timing problem.
- NEITHER tool sets Sec-CH-UA client hints / userAgentMetadata / init scripts.

## Root cause (confident)
A fresh profile with no valid Cloudflare `cf_clearance` forces Cloudflare to run its fingerprint check. webcli overrides the **UA string** to hide "HeadlessChrome", but it does NOT override the **Sec-CH-UA client-hint headers**, which in headless Chrome still advertise a `HeadlessChrome` brand. Cloudflare reads the client hints, sees HeadlessChrome, and serves a challenge the headless browser can't clear. The OLD cgpt profile dodges this only because it already holds a valid `cf_clearance`, so Cloudflare skips the fingerprint check entirely. Kevin's fresh visible login produced a clearance that is not honored headless, so headless falls back to the fingerprint check and fails.

## The fix
Make the **headless** browser present a self-consistent, non-headless fingerprint so a signed-in fresh profile can EARN `cf_clearance` headless (no human, no window). Specifically: when launching headless, override the User-Agent Client Hints metadata to match the overridden UA, with NO "HeadlessChrome" brand — e.g. via CDP `Network.setUserAgentOverride` with a full `userAgentMetadata` (brands incl. a "Google Chrome"/"Chromium"/"Not;A=Brand" set for Chrome 155, platform "macOS", platform version, architecture "arm", model "", mobile false, fullVersion), applied to the context / every page (and the in-page fetch context the guard uses). Equivalent Playwright approaches are fine as long as the result is: in headless, both `navigator.userAgent` AND `navigator.userAgentData`/`Sec-CH-UA` report normal Chrome with no HeadlessChrome anywhere. Keep the existing UA-string override. Do NOT change visible/login mode (it already uses the native UA).

Apply it in the shared core browser so it is consistent for every site, but it must not regress Gemini or Muse (they already pass headless — a more-normal fingerprint only helps). Keep the exit-4 `hidden_browser_blocked` fallback for the genuinely-unclearable case; this fix should make the NORMAL signed-in case clear automatically.

## Constraints
- DO NOT log in and DO NOT ask Kevin to; he is already signed in to `chatgpt:default`. The auth cookies are present — only `cf_clearance` needs earning, which a consistent headless fingerprint can do on the next request.
- Normal commands must stay HEADLESS and show/focus NO window. You MAY use `--headed` strictly for your own diagnosis if you must, but the shipped behavior and tests keep commands headless. Verify no-window with `webcli/scripts/window_check.py`.
- Don't touch the old tools or their data (`~/.chatgpt-web-cli`, chatgpt-web-cli repo, etc.).

## Verify (all must pass, headless, 0 windows)
1. `~/.local/pipx/venvs/webcli/bin/python -m webcli.scripts.window_check -- llmcli chatgpt -p "Reply with exactly: pong"` → the command exits 0 with answer "pong"; report `"ok": true`, `maxVisibleProfileWindows: 0`, `profileChromeActivated: false`. (frontmostChanged can be noisy on this shared machine; the authoritative signals are 0 profile windows and chrome-not-activated.)
2. `llmcli chatgpt status` → exit 0, signedIn true.
3. `LLMCLI_FORMAT=json llmcli chatgpt --image-out /tmp/cg-img -p "Create an image of a blue circle"` (leave -m OFF) → exit 0, an image saved, JSON has a non-null `servedModel`.
4. `.venv/bin/python -m pytest -q` stays green; ADD a regression test asserting the headless context/launch sets client-hint metadata with no "HeadlessChrome" brand (and that visible/login mode is unchanged).

## Then
`pipx install --force -e .`, commit in logical chunks, push origin main, and print a summary + pytest count + final SHA. If, after a correct and verified fingerprint fix, the signed-in profile STILL cannot clear Cloudflare headless, say so explicitly with evidence and describe exactly what a one-time human step would need to be — do not fake a pass.
