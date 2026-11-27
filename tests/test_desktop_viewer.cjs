"use strict";
// The 远程桌面 viewer's H.264 decoder (agent_nexus/static/local/desktop-h264.js): a picture the browser's
// decoder fails on or never returns must not stop noVNC's render queue, and the viewer must ask for a key
// frame, move to software decoding, and give up in the end.
const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");
const { pathToFileURL } = require("node:url");

const STATIC = path.join(__dirname, "../agent_nexus/static");
globalThis.window = globalThis.window || { console };  // noVNC's logging reads window.console
const load = (rel) => import(pathToFileURL(path.join(STATIC, rel)).href);

const KEY = Uint8Array.from([0, 0, 0, 1, 0x67, 0x42, 0x00, 0x1f, 0, 0, 0, 1, 0x68, 0xce, 0, 0, 0, 1, 0x65, 0x88, 0x84]);
const DELTA = Uint8Array.from([0, 0, 0, 1, 0x41, 0x9a, 0x02]);

class FakeDecoder {
  static made = [];
  constructor({ output, error }) { this.output = output; this.error = error; this.state = "unconfigured"; this.chunks = []; FakeDecoder.made.push(this); }
  configure(config) { this.config = config; this.state = "configured"; }
  decode(chunk) { this.chunks.push(chunk); }
  close() { this.state = "closed"; }
  emit(timestamp) { this.output({ timestamp, close() { this.closed = true; } }); }
  crash() { this.state = "closed"; this.error(new Error("decode failed")); }
}
class Chunk { constructor(init) { Object.assign(this, init); } }

/** a socket that holds one H.264 rect (as noVNC's Websock would) */
function sock(payload, flags = 0) {
  const head = Buffer.alloc(8);
  head.writeUInt32BE(payload.length, 0);
  head.writeUInt32BE(flags, 4);
  const data = Uint8Array.from([...head, ...payload]);
  let at = 0;
  return {
    rQwait: (_what, n) => data.length - at < n,
    rQshift32: () => { const v = Buffer.from(data.subarray(at, at + 4)).readUInt32BE(0); at += 4; return v; },
    rQshiftBytes: (n) => { const v = data.subarray(at, at + n); at += n; return v; },
  };
}

async function setup() {
  FakeDecoder.made = [];
  const { SteadyH264Decoder, SOFTWARE_AFTER, GIVE_UP_AFTER, STALL_MS } = await load("local/desktop-h264.js");
  const { H264Parser } = await load("vendor/novnc/core/decoders/h264.js");
  let clock = 0;
  const events = { keyFrames: 0, software: 0, giveUp: 0 };
  const decoder = new SteadyH264Decoder({ Parser: H264Parser, VideoDecoder: FakeDecoder, EncodedVideoChunk: Chunk }, {
    keyFrame: () => events.keyFrames++, software: () => events.software++, giveUp: () => events.giveUp++,
  }, () => clock);
  const drawn = [];
  const display = { videoFrame: (x, y, w, h, picture) => drawn.push(picture) };
  const rect = (payload, flags = 0) => decoder.decodeRect(0, 0, 64, 48, sock(payload, flags), display);
  return { decoder, drawn, rect, events, tick: (ms) => { clock += ms; }, SOFTWARE_AFTER, GIVE_UP_AFTER, STALL_MS };
}

test("pictures come out in order and the last one of an update is kept", async () => {
  const { drawn, rect } = await setup();
  assert.equal(rect(KEY, 1), true);
  assert.equal(rect(DELTA), true);
  const live = FakeDecoder.made.at(-1);
  assert.equal(live.chunks.length, 2);
  assert.equal(live.chunks[0].type, "key");
  live.emit(live.chunks[1].timestamp);  // the first one was dropped by the decoder
  assert.ok(drawn[0].ready && drawn[1].ready, "a dropped picture is settled, not left pending");
  assert.equal(drawn[1].frame.timestamp, live.chunks[1].timestamp);
  assert.equal(typeof drawn[0].frame.close, "function");
});

test("a decoder error settles what it owes, asks for a key frame and waits for one", async () => {
  const { drawn, rect, events, tick } = await setup();
  rect(KEY, 1);
  FakeDecoder.made.at(-1).crash();
  assert.ok(drawn[0].ready, "the render queue is not stuck on the failed picture");
  assert.equal(events.keyFrames, 1);
  tick(1500);
  rect(DELTA);  // no decoder: a delta cannot start one
  assert.equal(drawn.length, 1);
  assert.equal(events.keyFrames, 2);
  rect(KEY, 1);  // the key frame the server sent
  assert.equal(FakeDecoder.made.at(-1).state, "configured");
  assert.equal(drawn.length, 2);
});

test("repeated failures move to software, then give up", async () => {
  const { decoder, rect, events, tick, SOFTWARE_AFTER, GIVE_UP_AFTER } = await setup();
  for (let i = 0; i < SOFTWARE_AFTER; i++) { rect(KEY, 1); FakeDecoder.made.at(-1).crash(); tick(100); }
  assert.equal(events.software, 1);
  assert.equal(decoder.software, true);
  rect(KEY, 1);
  assert.equal(FakeDecoder.made.at(-1).config.hardwareAcceleration, "prefer-software");
  FakeDecoder.made.at(-1).crash();
  for (let i = 1; i < GIVE_UP_AFTER; i++) { rect(KEY, 1); FakeDecoder.made.at(-1).crash(); tick(100); }
  assert.equal(events.giveUp, 1);
});

test("a decoder that holds pictures too long counts as failed", async () => {
  const { decoder, drawn, rect, events, tick, STALL_MS } = await setup();
  rect(KEY, 1);
  tick(STALL_MS - 100);
  decoder.watch();
  assert.equal(drawn[0].ready, false);
  tick(200);
  decoder.watch();
  assert.equal(drawn[0].ready, true);
  assert.equal(events.keyFrames, 1);
  assert.equal(FakeDecoder.made.at(-1).state, "closed");
});

test("a corrupt payload does not throw into noVNC", async () => {
  const { rect, events } = await setup();
  assert.equal(rect(Uint8Array.from([1, 2, 3, 4, 5])), true);
  assert.equal(events.keyFrames, 1);
});

test("bit rates read like the bar shows them", async () => {
  const { bitrateLabel } = await load("local/desktop.js").catch(() => ({}));
  if (!bitrateLabel) return;  // the page module needs the browser
  assert.equal(bitrateLabel(2_400_000), "2.4 Mbps");
  assert.equal(bitrateLabel(860_000), "860 kbps");
  assert.equal(bitrateLabel(12_000_000), "12 Mbps");
});
