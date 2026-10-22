// Tab recording (0.12.1): pure helpers shared by the service worker, the offscreen recorder, the popup and the
// confirm window. No chrome.*, so node --test can check them.
//
// How a recording works: `chromecli record start TAB --confirm` -> the confirm window ("Start recording this tab")
// -> Kevin clicks Start -> chrome.tabCapture.getMediaStreamId (needs the tab's activeTab grant: Kevin clicked the
// chromecli icon on that tab since it last changed site; sharing it from the popup does that) -> the offscreen
// document (offscreen.html, reason USER_MEDIA) opens the stream and runs MediaRecorder (WebM VP9/VP8) -> chunks of at
// most CHUNK_BYTES go to the service worker, then to chromecli-host, which appends them to the file the CLI chose.
// No debugger permission: tabCapture only sees the tab's own pixels (with its iframes), never other windows.

// Raw bytes per native message. Base64 makes it 4/3 bigger (~683 KiB of JSON), under both directions' limits
// (Chrome -> host 64 MiB, host -> Chrome 1 MiB).
export const CHUNK_BYTES = 512 * 1024;
export const FPS_DEFAULT = 30;
export const FPS_MAX = 60;
export const MAX_DEFAULT_SEC = 600;
export const MAX_LIMIT_SEC = 4 * 3600;
export const WAIT_DEFAULT_SEC = 120;
export const VIDEO_BPS = 8_000_000;
// Recording states. Active ones hold the tab (one recording per tab); the rest are history.
export const ACTIVE_STATES = ["awaiting_popup", "starting", "recording", "stopping"];
export const isActive = (rec) => !!rec && ACTIVE_STATES.includes(rec.state);
// chrome.tabCapture's error when the extension wasn't invoked on the tab (no activeTab grant for it).
export const GRANT_ERROR = /not been invoked|activeTab|Chrome pages cannot be captured/i;

export class RecordError extends Error {
  constructor(code, message, extra = {}) { super(message); this.code = code; this.extra = extra; }
}

const intIn = (v, lo, hi, dflt, name) => {
  if (v == null || v === "") return dflt;
  const n = Number(v);
  if (!Number.isInteger(n) || n < lo || n > hi) throw new RecordError("bad_request", `${name} must be a whole number from ${lo} to ${hi}.`);
  return n;
};

// record.start args -> { fps, audio, maxSec, waitSec }, or throws bad_request.
export function recordOptions(args = {}) {
  return {
    fps: intIn(args.fps, 1, FPS_MAX, FPS_DEFAULT, "fps"),
    audio: args.audio === true,
    maxSec: intIn(args.maxSec, 1, MAX_LIMIT_SEC, MAX_DEFAULT_SEC, "maxSec"),
    waitSec: intIn(args.waitSec, 5, 900, WAIT_DEFAULT_SEC, "waitSec"),
  };
}

export const validRecId = (id) => typeof id === "string" && /^[a-f0-9]{8,64}$/.test(id);

// Why this tab can't be recorded, or null. Only tabs Kevin shared himself (Read or Act from the popup): never a
// bot-opened tab, a share inherited from an opener, or the implicit active tab. Pause, deny and Off are checked by
// the caller's access() first (paused / origin_blocked / not_shared).
export function refusal({ share, bot }) {
  if (bot) return { code: "not_shared", reason: "bot_opened", message: "Recording needs a tab Kevin shared himself; an agent opened this one." };
  if (!share) return { code: "not_shared", reason: "not_shared", message: "Recording needs a tab Kevin shared (Read or Act)." };
  if (share.botOpened) return { code: "not_shared", reason: "bot_opened", message: "Recording needs a tab Kevin shared himself; an agent opened this one." };
  if (share.inheritedFrom != null) {
    return { code: "not_shared", reason: "inherited_share", message: "Recording needs a tab Kevin shared himself; this one inherited its share from the tab that opened it." };
  }
  if (!["read", "act"].includes(share.level)) return { code: "not_shared", reason: "not_shared", message: "Recording needs a Read or Act share." };
  return null;
}

// The MediaRecorder type: VP9 first, then VP8, then whatever WebM the browser has.
export function pickMime(isSupported, audio = false) {
  const tries = audio ? ["video/webm;codecs=vp9,opus", "video/webm;codecs=vp8,opus", "video/webm"]
    : ["video/webm;codecs=vp9", "video/webm;codecs=vp8", "video/webm"];
  return tries.find((t) => { try { return isSupported(t); } catch { return false; } }) || "video/webm";
}

// Uint8Array -> base64 pieces of at most `size` raw bytes each.
export function base64Pieces(bytes, size = CHUNK_BYTES) {
  const out = [];
  for (let i = 0; i < bytes.length; i += size) out.push(toBase64(bytes.subarray(i, i + size)));
  return out;
}

export function toBase64(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

// The tab badge while a recording holds it (wins over R / A).
export function recBadge(rec) {
  if (!isActive(rec)) return null;
  return rec.state === "awaiting_popup" ? { text: "REC?", color: "#E8710A" } : { text: "REC", color: "#D93025" };
}

const trunc = (s, n) => (String(s).length > n ? String(s).slice(0, n - 1) + "…" : String(s));

// The confirm window's headline: "Start recording this tab: Shopify admin (https://admin.shopify.com)".
export function recordHeadline(info) {
  return `Start recording this tab: ${trunc(info?.title || "(untitled)", 80)} (${info?.origin || ""})`;
}

// Plain words for the confirm window's details card.
export function recordRows(info) {
  const r = info?.record || {};
  const mins = (s) => (s % 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s / 60}m`);
  return [
    ["Captures", "Only this tab's page (with its embedded frames): no browser toolbar, no other windows"],
    ["Audio", r.audio ? "Tab audio is recorded (you keep hearing it)" : "Off"],
    ["Frame rate", `up to ${r.fps || FPS_DEFAULT} fps`],
    ["Stops", `after ${mins(r.maxSec || MAX_DEFAULT_SEC)}, on chromecli record stop, on Pause, on Unshare, or when the tab leaves ${info?.origin || "this site"}`],
  ];
}

// The public view of a recording (status rows, replies, the popup).
export function recView(rec, now = Date.now()) {
  if (!rec) return null;
  const end = rec.stoppedAt || now;
  return {
    recId: rec.recId, tab: rec.tab, origin: rec.origin, title: rec.title || "", state: rec.state,
    fps: rec.fps, audio: !!rec.audio, maxSec: rec.maxSec, via: rec.via || null,
    mime: rec.mime || null, width: rec.width ?? null, height: rec.height ?? null,
    startedAt: rec.startedAt || null, stoppedAt: rec.stoppedAt || null,
    durationMs: rec.startedAt ? Math.max(0, end - rec.startedAt) : 0,
    bytes: rec.bytes || 0, chunks: rec.chunks || 0,
    ...(rec.reason ? { reason: rec.reason } : {}), ...(rec.error ? { error: rec.error } : {}),
    ...(rec.state === "awaiting_popup" ? { waitUntil: rec.waitUntil || null } : {}),
    ...(rec.frames != null ? { frames: rec.frames } : {}),
  };
}

// The popup's shared-tab row: which button the recording needs.
export function recButton(rec) {
  if (!isActive(rec)) return null;
  if (rec.state === "awaiting_popup") {
    return { kind: "record", label: "Record", title: "An agent asked to record this tab. Chrome needs the click here: press Record (on this tab's popup)." };
  }
  return { kind: "stop", label: "Stop", title: "Stop recording this tab (the file is kept)" };
}
