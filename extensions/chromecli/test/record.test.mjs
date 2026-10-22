// 0.12.1 tab recording in the service worker: Kevin's Start is required, refusals, the popup-grant fallback, auto-stop
// on pause / unshare / origin change, chunk forwarding, covered screenshots from the stream (mocked tabCapture +
// offscreen recorder, see harness.mjs).
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadBackground } from "./harness.mjs";
import { refusal, recordOptions, recBadge, recButton, recordHeadline, base64Pieces, toBase64, pickMime, CHUNK_BYTES } from "../recorder.js";
import { headline, detailRows } from "../confirm-view.js";

const RID = "0123456789abcdef0123456789abcdef";
const RID2 = "fedcba9876543210fedcba9876543210";
const tick = (ms = 10) => new Promise((r) => setTimeout(r, ms));
const events = (h, kind) => h.posted.filter((m) => m.event === kind);

async function startRecording(h, tab = 1, recId = RID, extra = {}) {
  const id = h.start("record.start", { tab, recId, fps: 30, maxSec: 600, ...extra });
  const cid = await h.confirmWindow(h.windowsCreated.filter((w) => w.type === "popup").length + 1);
  await h.answer(cid, "allow");
  return h.reply(id);
}

test("record.start without confirm: true is refused; nothing captured", async () => {
  const h = await loadBackground();
  await h.share(1, "read");
  const r = await h.call("record.start", { tab: 1, recId: RID }, { confirm: false });
  assert.equal(r.ok, false);
  assert.equal(r.error.code, "bad_request");
  assert.equal(h.streamIds.length + h.offscreenCreated + h.windowsCreated.length, 0);
});

test("--confirm alone never records: the confirm window must get Kevin's Start first", async () => {
  const h = await loadBackground();
  await h.share(1, "read");
  const id = h.start("record.start", { tab: 1, recId: RID, fps: 24, maxSec: 120 });
  const cid = await h.confirmWindow();
  await tick(30);
  assert.equal(h.streamIds.length, 0, "no stream ID before the click");
  assert.equal(h.offscreenCreated, 0);
  assert.equal(h.posted.some((m) => m.id === id), false, "still waiting");
  const info = await h.message({ type: "confirmGet", id: cid }, `confirm.html?id=${cid}`, { id: 999 });
  assert.equal(info.info.op, "record");
  assert.equal(headline(info.info), "Start recording this tab: Form (http://127.0.0.1:8765)");
  await h.answer(cid, "allow");
  const r = await h.reply(id);
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.result.state, "recording");
  assert.equal(r.result.fps, 24);
  assert.equal(r.result.confirmedInBrowser, true);
  assert.deepEqual(h.streamIds, [1]);
  assert.deepEqual(h.offscreenReasons, ["USER_MEDIA"]);
  const start = h.toOffscreen.find((m) => m.type === "start");
  assert.equal(start.streamId, "stream-1");
  assert.equal(start.audio, false, "audio off by default");
  assert.equal(start.chunkBytes, CHUNK_BYTES);
  const ev = events(h, "rec_started")[0];
  assert.equal(ev.recId, RID);
  assert.equal(ev.origin, "http://127.0.0.1:8765");
});

test("Deny or timeout in the confirm: declined, nothing captured", async () => {
  const h = await loadBackground();
  await h.share(1, "act");
  const id = h.start("record.start", { tab: 1, recId: RID });
  await h.answer(await h.confirmWindow(), "deny");
  assert.equal((await h.reply(id)).error.code, "declined");
  assert.equal(h.streamIds.length + h.offscreenCreated, 0);
  const st = await h.call("record.status", {});
  assert.deepEqual(st.result.recordings, []);
});

test("refused: unshared, bot-opened, denied origin, paused, inherited share", async () => {
  const h = await loadBackground();
  let r = await h.call("record.start", { tab: 2, recId: RID });
  assert.equal(r.error.code, "not_shared", "unshared");
  await h.chrome.storage.session.set({ botTabs: { 2: { at: 1 } }, shares: { 2: { origin: "https://example.com", level: "act", at: 1, botOpened: true } } });
  r = await h.call("record.start", { tab: 2, recId: RID });
  assert.equal(r.error.code, "not_shared", "bot-opened");
  assert.equal(r.error.reason, "bot_opened");
  await h.chrome.storage.session.set({ botTabs: {}, shares: { 2: { origin: "https://example.com", level: "act", at: 1, inheritedFrom: 1 } } });
  r = await h.call("record.start", { tab: 2, recId: RID });
  assert.equal(r.error.reason, "inherited_share");
  await h.chrome.storage.session.set({ shares: { 3: { origin: "https://bank.chase.com", level: "read", at: 1 } } });
  r = await h.call("record.start", { tab: 3, recId: RID });
  assert.equal(r.ok, false, "denied origin");
  assert.notEqual(r.error.code, "bad_request");
  await h.share(1, "read");
  await h.message({ type: "pause", paused: true });
  r = await h.call("record.start", { tab: 1, recId: RID });
  assert.equal(r.error.code, "paused");
  assert.equal(h.streamIds.length + h.windowsCreated.filter((w) => w.type === "popup").length, 0, "refused before any confirm");
});

test("bad options and bad recIds are refused before the confirm", async () => {
  const h = await loadBackground();
  await h.share(1, "read");
  for (const args of [{ recId: "nope" }, { recId: RID, fps: 0 }, { recId: RID, fps: 120 }, { recId: RID, maxSec: 99999 }]) {
    const r = await h.call("record.start", { tab: 1, ...args });
    assert.equal(r.ok, false, JSON.stringify(args));
  }
  assert.equal(h.windowsCreated.length, 0);
});

test("one recording per tab; a second record.start is refused", async () => {
  const h = await loadBackground();
  await h.share(1, "read");
  assert.equal((await startRecording(h)).ok, true);
  const r = await h.call("record.start", { tab: 1, recId: RID2 });
  assert.equal(r.error.reason, "already_recording");
});

test("no activeTab grant: awaiting_popup, notification, then Kevin's Record in the popup starts it", async () => {
  const h = await loadBackground();
  await h.share(1, "read");
  h.captureError = "Extension has not been invoked for the current page (see activeTab permission). Chrome pages cannot be captured.";
  const r = await startRecording(h, 1, RID, { waitSec: 30 });
  assert.equal(r.ok, true);
  assert.equal(r.result.state, "awaiting_popup");
  assert.match(r.result.hint, /click the chromecli icon/);
  assert.equal(events(h, "rec_waiting").length, 1);
  assert.equal(h.notes.length, 1);
  assert.equal(recBadge({ state: "awaiting_popup" }).text, "REC?");
  const state = await h.message({ type: "state", tabId: 1 });
  const row = state.shared.find((s) => s.tabId === 1);
  assert.equal(row.rec.button.kind, "record");
  // Still no grant: the popup says so, keeps waiting.
  let res = await h.message({ type: "recordStart", recId: RID });
  assert.equal(res.ok, false);
  assert.match(res.error, /click the chromecli icon/);
  h.captureError = null; // Kevin opened the popup on that tab: Chrome grants it
  res = await h.message({ type: "recordStart", recId: RID });
  assert.equal(res.ok, true, JSON.stringify(res));
  const st = await h.call("record.status", { recId: RID });
  assert.equal(st.result.recordings[0].state, "recording");
  assert.equal(st.result.recordings[0].via, "popup");
  assert.equal(events(h, "rec_started").length, 1);
});

test("awaiting_popup expires after waitSec; other tabCapture errors fail at once (capture_failed)", async () => {
  const h = await loadBackground();
  await h.share(1, "read");
  h.captureError = "Extension has not been invoked for the current page (see activeTab permission).";
  const r = await startRecording(h, 1, RID, { waitSec: 5 });
  assert.equal(r.result.state, "awaiting_popup");
  h.captureError = "Cannot capture a tab with an active stream.";
  await h.share(2, "read");
  const r2 = await startRecording(h, 2, RID2);
  assert.equal(r2.error.code, "capture_failed");
  assert.equal(events(h, "rec_stopped").find((e) => e.recId === RID2).state, "failed");
});

test("chunks go to the host in order; unknown recordings and oversized chunks are refused", async () => {
  const h = await loadBackground();
  await h.share(1, "read");
  await startRecording(h);
  const a = toBase64(new Uint8Array([1, 2, 3, 4, 5]));
  assert.deepEqual(await h.chunk(RID, 0, a), { ok: true });
  assert.deepEqual(await h.chunk(RID, 1, "BgcI"), { ok: true });
  const sent = events(h, "rec_chunk");
  assert.deepEqual(sent.map((m) => [m.seq, m.data]), [[0, a], [1, "BgcI"]]);
  assert.equal((await h.chunk(RID2, 0)).ok, false, "unknown recId");
  const big = "A".repeat(Math.ceil(CHUNK_BYTES / 3) * 4 + 8);
  assert.equal((await h.chunk(RID, 2, big)).ok, false, "over the chunk size");
  const st = await h.call("record.status", { tab: 1 });
  assert.equal(st.result.recordings[0].bytes, 8);
  assert.equal(st.result.recordings[0].chunks, 2);
  // Every forwarded frame stays far under Chrome's 1 MB host->Chrome / 64 MiB Chrome->host limits.
  assert.ok(Math.ceil(CHUNK_BYTES / 3) * 4 + 200 < 1024 * 1024);
});

test("a chunk the host can't take (port gone) stops the recording", async () => {
  const h = await loadBackground();
  await h.share(1, "read");
  await startRecording(h);
  h.port.postMessage = () => { throw new Error("Attempting to use a disconnected port object"); };
  const r = await h.chunk(RID, 0);
  assert.equal(r.ok, false);
  await tick(30);
  const rec = (await h.message({ type: "state", tabId: 1 })).shared.find((s) => s.tabId === 1).rec;
  assert.equal(rec, undefined, "no longer active");
});

test("record.stop finalizes: rec_stopped with bytes, duration, reason; offscreen closes when idle", async () => {
  const h = await loadBackground();
  await h.share(1, "read");
  await startRecording(h);
  await h.chunk(RID, 0, "AAAA");
  const r = await h.call("record.stop", { tab: 1 });
  assert.equal(r.ok, true);
  assert.equal(r.result.stopped[0].state, "stopped");
  assert.equal(r.result.stopped[0].bytes, 3);
  const ev = events(h, "rec_stopped").at(-1);
  assert.equal(ev.recId, RID);
  assert.equal(ev.state, "stopped");
  assert.match(ev.reason, /record stop/);
  assert.equal(h.offscreenOpen, false);
  const again = await h.call("record.stop", { tab: 1 });
  assert.deepEqual(again.result.stopped, []);
  assert.equal(again.result.finished[0].recId, RID, "finished ones are listed (for the CLI's mp4 conversion)");
});

test("auto-stop: Pause stops every recording", async () => {
  const h = await loadBackground();
  await h.share(1, "read");
  await h.share(2, "act");
  await startRecording(h, 1, RID);
  await startRecording(h, 2, RID2);
  await h.message({ type: "pause", paused: true });
  await tick(30);
  const stops = events(h, "rec_stopped");
  assert.deepEqual(stops.map((e) => e.recId).sort(), [RID, RID2].sort());
  assert.ok(stops.every((e) => e.reason === "paused" && e.state === "stopped"));
});

test("auto-stop: Unshare stops that tab's recording only", async () => {
  const h = await loadBackground();
  await h.share(1, "read");
  await h.share(2, "act");
  await startRecording(h, 1, RID);
  await startRecording(h, 2, RID2);
  await h.message({ type: "unshare", tabId: 1 });
  await tick(30);
  const stops = events(h, "rec_stopped");
  assert.deepEqual(stops.map((e) => e.recId), [RID]);
  assert.match(stops[0].reason, /unshared/);
  assert.equal((await h.call("record.status", { tab: 2 })).result.recordings[0].state, "recording");
});

test("auto-stop: navigating to another origin stops it; same-origin navigation doesn't", async () => {
  const h = await loadBackground();
  await h.share(1, "read");
  await startRecording(h);
  await h.chrome.webNavigation.onBeforeNavigate.fire({ tabId: 1, frameId: 0, url: "http://127.0.0.1:8765/other.html" });
  await h.chrome.webNavigation.onBeforeNavigate.fire({ tabId: 1, frameId: 3, url: "https://ads.example/frame" });
  await tick(20);
  assert.equal(events(h, "rec_stopped").length, 0);
  await h.chrome.webNavigation.onBeforeNavigate.fire({ tabId: 1, frameId: 0, url: "https://evil.example/" });
  await tick(30);
  const ev = events(h, "rec_stopped");
  assert.equal(ev.length, 1);
  assert.equal(ev[0].reason, "origin changed");
});

test("pause while the stream ID is pending: the recording never starts", async () => {
  const h = await loadBackground();
  await h.share(1, "read");
  let release;
  h.chrome.tabCapture.getMediaStreamId = () => new Promise((r) => { release = () => r("stream-1"); });
  const id = h.start("record.start", { tab: 1, recId: RID });
  await h.answer(await h.confirmWindow(), "allow");
  await tick(20);
  await h.message({ type: "pause", paused: true });
  release();
  const r = await h.reply(id);
  assert.equal(r.ok, false);
  assert.equal(r.error.code, "capture_failed");
  assert.equal(h.toOffscreen.filter((m) => m.type === "start").length, 0);
  assert.equal(events(h, "rec_started").length, 0);
});

test("screenshot: a covered tab is not_visible (with the record hint) unless it's recorded; then the stream frame", async () => {
  const h = await loadBackground();
  await h.share(2, "read"); // tab 2 is a background tab
  let r = await h.call("tabs.screenshot", { tab: 2 });
  assert.equal(r.error.code, "not_visible");
  assert.match(r.error.hint, /record start/);
  r = await h.call("tabs.screenshot", { tab: 2, covered: true });
  assert.equal(r.error.code, "not_visible");
  await startRecording(h, 2, RID);
  r = await h.call("tabs.screenshot", { tab: 2 });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.result.source, "stream");
  assert.equal(r.result.data, "iVBORw0KGgo=");
  assert.equal(r.result.recId, RID);
  assert.equal(r.result.width, 2880);
});

test("screenshot: a visible tab uses captureVisibleTab unless --covered", async () => {
  const h = await loadBackground();
  await h.share(1, "read");
  let r = await h.call("tabs.screenshot", { tab: 1 });
  assert.equal(r.result.source, "visible");
  await startRecording(h, 1, RID);
  r = await h.call("tabs.screenshot", { tab: 1, covered: true });
  assert.equal(r.result.source, "stream");
});

test("status reports recording support and the active ones", async () => {
  const h = await loadBackground();
  await h.share(1, "read");
  await startRecording(h);
  const st = await h.call("status", {});
  assert.equal(st.result.recording.canRecord, true);
  assert.equal(st.result.recording.active.length, 1);
});

test("recorder.js helpers", () => {
  assert.equal(refusal({ share: { level: "read" }, bot: false }), null);
  assert.equal(refusal({ share: { level: "act" }, bot: true }).reason, "bot_opened");
  assert.equal(refusal({ share: null, bot: false }).reason, "not_shared");
  assert.deepEqual(recordOptions({}), { fps: 30, audio: false, maxSec: 600, waitSec: 120 });
  assert.throws(() => recordOptions({ fps: 61 }));
  assert.equal(recBadge({ state: "recording" }).text, "REC");
  assert.equal(recButton({ state: "recording" }).kind, "stop");
  assert.equal(recButton({ state: "stopped" }), null);
  assert.equal(recordHeadline({ title: "T", origin: "https://a.b" }), "Start recording this tab: T (https://a.b)");
  assert.equal(pickMime((t) => t === "video/webm;codecs=vp8", false), "video/webm;codecs=vp8");
  const bytes = new Uint8Array(1000).map((_, i) => i % 251);
  const pieces = [...base64Pieces(bytes, 300)];
  assert.equal(pieces.length, 4);
  assert.equal(Buffer.concat(pieces.map((p) => Buffer.from(p, "base64"))).equals(Buffer.from(bytes)), true);
  const rows = detailRows({ op: "record", origin: "https://a.b", title: "T", record: { fps: 30, audio: false, maxSec: 600 } });
  assert.ok(rows.some((r) => /Captures/.test(r[0] ?? r.label ?? "")));
});
