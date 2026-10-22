// chromecli's offscreen recorder (0.12.1): one document (reason USER_MEDIA) for every tab recording. The service
// worker gets a stream ID from chrome.tabCapture.getMediaStreamId (after Kevin's Start) and sends it here; this page
// opens the tab stream, records it with MediaRecorder (WebM, 1 s slices) and sends each slice to the worker in
// base64 pieces of at most CHUNK_BYTES, one at a time (the worker forwards them to chromecli-host, which writes the
// file). It also keeps the newest video frame, so `chromecli screenshot` works while the tab is covered.
// Only chrome.runtime messaging is available in offscreen documents; this page talks to nothing else.
import { CHUNK_BYTES, VIDEO_BPS, base64Pieces, pickMime, toBase64 } from "./recorder.js";

const recs = new Map();

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.target !== "offscreen" || sender.id !== chrome.runtime.id) return false;
  handle(msg).then(sendResponse, (e) => sendResponse({ ok: false, error: String(e?.message || e?.name || e) }));
  return true;
});

async function handle(msg) {
  switch (msg.type) {
    case "start": return start(msg);
    case "stop": return stop(msg.recId, msg.reason);
    case "frame": return frame(msg);
    case "list": return { ok: true, recs: [...recs.values()].map((r) => ({ recId: r.recId, bytes: r.bytes, chunks: r.seq, frames: r.frames })) };
    case "ping": return { ok: true };
  }
  return { ok: false, error: "unknown offscreen message" };
}

async function start({ recId, streamId, fps, audio, maxWidth, maxHeight, chunkBytes, videoBitsPerSecond }) {
  if (recs.has(recId)) return { ok: false, error: "already recording" };
  const mandatory = { chromeMediaSource: "tab", chromeMediaSourceId: streamId };
  const video = { mandatory: { ...mandatory, maxFrameRate: fps, ...(maxWidth ? { maxWidth, maxHeight } : {}) } };
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ video, audio: audio ? { mandatory } : false });
  } catch (e) {
    return { ok: false, error: `getUserMedia: ${e?.name || ""} ${e?.message || e}`.trim() };
  }
  // Capturing a tab's audio mutes it for Kevin unless it's played back: route it to the speakers.
  let ctx = null;
  const hasAudio = stream.getAudioTracks().length > 0;
  if (hasAudio) {
    try { ctx = new AudioContext(); ctx.createMediaStreamSource(stream).connect(ctx.destination); } catch { /* silent */ }
  }
  const mimeType = pickMime((t) => MediaRecorder.isTypeSupported(t), hasAudio);
  let recorder;
  try {
    recorder = new MediaRecorder(stream, { mimeType, videoBitsPerSecond: videoBitsPerSecond || VIDEO_BPS });
  } catch (e) {
    for (const t of stream.getTracks()) t.stop();
    return { ok: false, error: `MediaRecorder: ${e?.message || e}` };
  }
  const r = { recId, stream, ctx, recorder, mimeType, seq: 0, bytes: 0, frames: 0, latest: null, lastFrameAt: 0,
    startedAt: Date.now(), size: Math.min(Number(chunkBytes) || CHUNK_BYTES, CHUNK_BYTES), queue: Promise.resolve(),
    reason: null, error: null, lost: false };
  r.finished = new Promise((res) => { r.finish = res; });
  recs.set(recId, r);
  recorder.ondataavailable = (e) => { if (e.data && e.data.size) r.queue = r.queue.then(() => sendBlob(r, e.data)); };
  recorder.onerror = (e) => { r.error = String(e?.error?.message || e?.error?.name || "recorder error"); };
  recorder.onstop = () => { r.queue = r.queue.then(() => done(r)); };
  const track = stream.getVideoTracks()[0];
  // The tab closed or Chrome ended the capture.
  track.addEventListener("ended", () => { if (recorder.state !== "inactive") { r.reason ||= "capture_ended"; recorder.stop(); } });
  tap(r, track);
  recorder.start(1000);
  const s = track.getSettings ? track.getSettings() : {};
  return { ok: true, mime: mimeType, width: s.width ?? null, height: s.height ?? null, frameRate: s.frameRate ?? null, audio: hasAudio };
}

// Keep the newest frame (and count them) from a clone of the video track, for covered screenshots.
function tap(r, track) {
  const clone = track.clone();
  r.tapTrack = clone;
  if (typeof MediaStreamTrackProcessor === "function") {
    const reader = new MediaStreamTrackProcessor({ track: clone, maxBufferSize: 1 }).readable.getReader();
    r.reader = reader;
    (async () => {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        r.latest?.close();
        r.latest = value;
        r.frames++;
        r.lastFrameAt = Date.now();
      }
    })().catch(() => {});
    return;
  }
  // Fallback: a <video> element playing the clone (drawImage takes its current frame).
  const v = document.createElement("video");
  v.muted = true; v.playsInline = true; v.srcObject = new MediaStream([clone]);
  v.play().catch(() => {});
  r.video = v;
  const count = () => { r.frames++; r.lastFrameAt = Date.now(); v.requestVideoFrameCallback?.(count); };
  v.requestVideoFrameCallback?.(count);
}

async function sendBlob(r, blob) {
  if (r.lost) return;
  const bytes = new Uint8Array(await blob.arrayBuffer());
  for (const data of base64Pieces(bytes, r.size)) {
    let res;
    try { res = await chrome.runtime.sendMessage({ type: "rec_chunk", recId: r.recId, seq: r.seq, data }); } catch (e) { res = { ok: false, error: String(e?.message || e) }; }
    if (!res?.ok) {
      // The worker couldn't hand it to chromecli-host: the file would have a hole, so stop here.
      r.lost = true;
      r.error ||= `chunk ${r.seq} not delivered: ${res?.error || "no answer"}`;
      r.reason ||= "host_disconnected";
      if (r.recorder.state !== "inactive") r.recorder.stop();
      return;
    }
    r.seq++;
  }
  r.bytes += bytes.length;
}

async function done(r) {
  for (const t of r.stream.getTracks()) t.stop();
  r.tapTrack?.stop();
  try { r.latest?.close(); } catch { /* closed */ }
  r.latest = null;
  if (r.video) r.video.srcObject = null;
  try { await r.ctx?.close(); } catch { /* closed */ }
  recs.delete(r.recId);
  const msg = { type: "rec_done", recId: r.recId, bytes: r.bytes, chunks: r.seq, frames: r.frames, mime: r.mimeType,
    durationMs: Date.now() - r.startedAt, reason: r.reason || "stopped", ...(r.error ? { error: r.error } : {}) };
  try { await chrome.runtime.sendMessage(msg); } catch { /* the worker restarts and finds nothing */ }
  r.finish(msg);
}

async function stop(recId, reason) {
  const r = recs.get(recId);
  if (!r) return { ok: true, gone: true };
  r.reason ||= reason || "stopped";
  if (r.recorder.state !== "inactive") {
    try { r.recorder.requestData(); } catch { /* inactive */ }
    r.recorder.stop();
  }
  const out = await r.finished;
  return { ok: true, ...out };
}

const sleep = (ms) => new Promise((res) => setTimeout(res, ms));

async function frame({ recId, format, quality }) {
  const r = recs.get(recId);
  if (!r) return { ok: false, error: "not recording" };
  for (let i = 0; i < 40 && !r.frames; i++) await sleep(50); // the first frame after start
  let w, h, draw;
  if (r.latest) {
    w = r.latest.displayWidth; h = r.latest.displayHeight;
    draw = (c) => c.drawImage(r.latest, 0, 0, w, h);
  } else if (r.video && r.video.videoWidth) {
    w = r.video.videoWidth; h = r.video.videoHeight;
    draw = (c) => c.drawImage(r.video, 0, 0, w, h);
  } else {
    return { ok: false, error: "no frame from the tab yet" };
  }
  const canvas = new OffscreenCanvas(w, h);
  draw(canvas.getContext("2d"));
  const type = format === "jpeg" ? "image/jpeg" : "image/png";
  const blob = await canvas.convertToBlob(type === "image/jpeg" ? { type, quality: Math.min(Math.max(1, Number(quality) || 80), 100) / 100 } : { type });
  const data = toBase64(new Uint8Array(await blob.arrayBuffer()));
  return { ok: true, data, width: w, height: h, frames: r.frames, frameAgeMs: r.lastFrameAt ? Date.now() - r.lastFrameAt : null };
}
