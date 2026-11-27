// H.264 for the 远程桌面 viewer, in place of noVNC's own decoder (vendor/novnc/core/decoders/h264.js).
//
// noVNC hands every decoded picture to its render queue as a promise, and the queue waits for each
// one in order. When the browser's decoder failed (a hardware decoder that gives up, a GPU reset,
// a stream it does not like), that promise never settled: the queue -- and the picture -- stood
// still until the page reconnected, while the connection itself looked fine. Here:
//   * a picture the decoder drops or fails on is still settled (as a blank, so the last picture stays);
//   * after a failure the decoder is rebuilt and the server is asked for a key frame (a full update);
//   * a decoder that keeps failing in hardware moves to software; one that keeps failing there gives up
//     (the page then reconnects with JPEG);
//   * a picture the decoder holds for too long counts as a failure (some decoders keep frames);
//   * an update goes to the decoder whole, not cut at every slice (see Context.decode).

const RESET_CONTEXT = 1;
const RESET_ALL = 2;
/** a decoded picture that has not come out after this long: the decoder is stuck */
export const STALL_MS = 2000;
/** failures within this window that move to software, and that give up */
export const FAILURE_WINDOW_MS = 30000;
export const SOFTWARE_AFTER = 2;
export const GIVE_UP_AFTER = 6;

/** What the render queue draws for a picture that never came: one transparent pixel, which leaves
 *  the last picture as it was. */
function blankPicture() {
  let picture;
  if (typeof OffscreenCanvas !== "undefined") picture = new OffscreenCanvas(1, 1);
  else if (typeof document !== "undefined") { picture = document.createElement("canvas"); picture.width = picture.height = 1; }
  else picture = { width: 1, height: 1 };
  picture.close = () => {};
  return picture;
}

class Context {
  constructor(owner, width, height) {
    this.owner = owner;
    this.width = width;
    this.height = height;
    this.decoder = null;
    this.pending = new Map();  // timestamp -> pending picture
    this.clock = 0;
    this.profile = null;
    this.lastUsed = 0;
  }

  configured() { return this.decoder !== null && this.decoder.state === "configured"; }

  configure() {
    const { VideoDecoder } = this.owner.api;
    const [profileIdc, constraintSet, levelIdc] = this.profile;
    const codec = "avc1." + [profileIdc, constraintSet, levelIdc].map((n) => n.toString(16).padStart(2, "0")).join("");
    const decoder = new VideoDecoder({
      output: (frame) => this.output(decoder, frame),
      error: (error) => this.fail(decoder, error),
    });
    decoder.configure({ codec, codedWidth: this.width, codedHeight: this.height, optimizeForLatency: true,
      hardwareAcceleration: this.owner.software ? "prefer-software" : "no-preference" });
    this.decoder = decoder;
  }

  output(decoder, frame) {
    const pending = decoder === this.decoder ? this.pending.get(frame.timestamp) : undefined;
    if (!pending) { frame.close(); return; }
    // pictures before this one that never came out (dropped by the decoder): settle them blank
    for (const [timestamp, older] of this.pending) {
      if (timestamp >= frame.timestamp) break;
      this.settle(older, null);
    }
    this.pending.delete(frame.timestamp);
    this.owner.decoded++;
    this.settle(pending, frame);
  }

  settle(pending, frame) {
    this.pending.delete(pending.timestamp);
    if (frame && !pending.keep) { frame.close(); frame = null; }
    pending.frame = frame || blankPicture();
    pending.ready = true;
    pending.resolve();
  }

  /** The decoder failed (or stalled): settle what it owes, drop it, and ask for a key frame. */
  fail(decoder, error) {
    if (decoder !== this.decoder) return;
    this.release();
    this.owner.failed(error);
  }

  release() {
    const decoder = this.decoder;
    this.decoder = null;
    for (const pending of [...this.pending.values()]) this.settle(pending, null);
    if (decoder && decoder.state !== "closed") { try { decoder.close(); } catch { /* closed */ } }
  }

  stalled(now) {
    for (const pending of this.pending.values()) return now - pending.queued > STALL_MS;
    return false;
  }

  /** One update is one access unit (agent_nexus/vnc.py sends a whole picture per rect): it goes to the
   *  decoder as one chunk. noVNC split it at every slice, and encoders that cut a picture into slices
   *  (x264's zero-latency threads, some hardware encoders) then decoded as torn, green pictures. */
  decode(payload) {
    const parser = new this.owner.api.Parser(payload);
    let key = false;
    let slices = 0;
    try {
      for (let unit = parser.parse(); unit !== null; unit = parser.parse()) {
        slices++;
        key = key || unit.key;
        if (parser.profileIdc !== null) this.profile = [parser.profileIdc, parser.constraintSet, parser.levelIdc];
      }
    } catch (error) {
      this.release();  // one failure, reported once
      this.owner.failed(error);
      return null;
    }
    if (!slices) return null;
    if (!this.configured()) {
      if (!key || !this.profile) { this.owner.needKeyFrame(); return null; }
      try { this.configure(); } catch (error) { this.release(); this.owner.failed(error); return null; }
    }
    const timestamp = ++this.clock;
    const pending = { timestamp, promise: null, resolve: null, frame: null, ready: false, keep: true,
      queued: this.owner.now() };
    pending.promise = new Promise((resolve) => { pending.resolve = resolve; });
    this.pending.set(timestamp, pending);
    try {
      this.decoder.decode(new this.owner.api.EncodedVideoChunk({ timestamp, type: key ? "key" : "delta", data: payload }));
    } catch (error) {
      this.fail(this.decoder, error);
    }
    return pending;
  }
}

export class SteadyH264Decoder {
  /**
   * api: {Parser, VideoDecoder, EncodedVideoChunk} -- noVNC's H264Parser and WebCodecs (passed in for tests).
   * events: {keyFrame(), software(), giveUp()} -- ask the server for a key frame; moved to software; no use.
   */
  constructor(api, events = {}, now = () => performance.now()) {
    this.api = api;
    this.events = events;
    this.now = now;
    this.contexts = new Map();
    this.tick = 0;
    this.software = false;
    this.failures = [];
    this.decoded = 0;
    this.received = 0;
    this.bytes = 0;
    this.keyAsked = -1e9;
    this.gaveUp = false;
  }

  needKeyFrame() {
    const now = this.now();
    if (now - this.keyAsked < 1000) return;
    this.keyAsked = now;
    this.events.keyFrame?.();
  }

  failed(error) {
    const now = this.now();
    this.failures = this.failures.filter((at) => now - at < FAILURE_WINDOW_MS);
    this.failures.push(now);
    console.warn("H.264 decoder failed:", error?.message || error);
    if (!this.software && this.failures.length >= SOFTWARE_AFTER) {
      this.software = true;
      this.failures = [];
      this.events.software?.();
    } else if (this.software && this.failures.length >= GIVE_UP_AFTER && !this.gaveUp) {
      this.gaveUp = true;
      this.events.giveUp?.();
    }
    this.needKeyFrame();
  }

  /** Called now and then: a decoder that holds pictures too long is treated as failed. */
  watch() {
    const now = this.now();
    for (const context of this.contexts.values()) {
      if (context.stalled(now)) context.fail(context.decoder, new Error("the decoder stopped returning pictures"));
    }
  }

  context(x, y, width, height, reset) {
    const key = `${x},${y},${width},${height}`;
    let context = this.contexts.get(key);
    if (reset && context) { context.release(); context = undefined; }
    if (!context) {
      if (this.contexts.size >= 16) {
        const oldest = [...this.contexts.entries()].sort((a, b) => a[1].lastUsed - b[1].lastUsed)[0];
        oldest[1].release();
        this.contexts.delete(oldest[0]);
      }
      context = new Context(this, width, height);
      this.contexts.set(key, context);
    }
    context.lastUsed = this.tick++;
    return context;
  }

  /** noVNC's decoder interface. */
  decodeRect(x, y, width, height, sock, display) {
    if (sock.rQwait("h264 header", 8)) return false;
    const length = sock.rQshift32();
    const flags = sock.rQshift32();
    if (sock.rQwait("h264 payload", length, 8)) return false;
    if (flags & RESET_ALL) { for (const context of this.contexts.values()) context.release(); this.contexts.clear(); }
    const context = this.context(x, y, width, height, (flags & RESET_CONTEXT) !== 0);
    if (length !== 0) {
      const payload = sock.rQshiftBytes(length, false);
      this.received++;
      this.bytes += length;
      const picture = context.decode(payload);
      if (picture !== null) display.videoFrame(x, y, width, height, picture);
    }
    return true;
  }

  close() {
    for (const context of this.contexts.values()) context.release();
    this.contexts.clear();
  }
}
