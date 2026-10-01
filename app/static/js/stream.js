// stream.js — SSE wrapper for GET /api/v1/stream. One shared EventSource per
// topic set, ref-counted across pages; automatic reconnect with exponential
// backoff (1s → 30s cap). Auth: EventSource cannot set headers, so each
// connect first exchanges the stored credential (via api(), which sets the
// X-API-Key header) for a single-use ticket at POST /api/v1/auth/ticket and
// passes it as `?ticket=` — raw tokens never appear in the URL (access logs).
import { api } from "./api.js";

const RETRY_MIN_MS = 1000;
const RETRY_MAX_MS = 30000;

const channels = new Map(); // sorted topic key -> Channel

async function streamUrl(topics) {
  const qs = new URLSearchParams();
  qs.set("topics", topics.join(","));
  const { ticket } = await api("/api/v1/auth/ticket", { method: "POST" });
  qs.set("ticket", ticket);
  return "/api/v1/stream?" + qs.toString();
}

class Channel {
  constructor(topics) {
    this.topics = topics;
    this.topicHandlers = new Map(); // topic -> Set<fn>
    this.stateFns = new Set();
    this.refs = 0;
    this.es = null;
    this.connecting = false;
    this.generation = 0; // bumped on teardown to invalidate in-flight connects
    this.retryMs = RETRY_MIN_MS;
    this.retryTimer = null;
    this.failures = 0;
    this.state = "closed"; // closed | open | retrying | unsupported
  }

  add(topic, fn) {
    if (topic === "onState") {
      this.stateFns.add(fn);
      return;
    }
    if (!this.topicHandlers.has(topic)) this.topicHandlers.set(topic, new Set());
    this.topicHandlers.get(topic).add(fn);
  }

  remove(topic, fn) {
    if (topic === "onState") {
      this.stateFns.delete(fn);
      return;
    }
    const set = this.topicHandlers.get(topic);
    if (set) set.delete(fn);
  }

  setState(state) {
    this.state = state;
    this.stateFns.forEach((fn) => {
      try {
        fn(state, this.failures);
      } catch { /* handler errors must not kill the stream */ }
    });
  }

  open() {
    if (this.es || this.retryTimer || this.connecting) return; // already connecting/connected
    if (typeof EventSource === "undefined") {
      this.setState("unsupported");
      return;
    }
    this.connecting = true;
    this._openWithTicket(this.generation);
  }

  async _openWithTicket(generation) {
    let url;
    try {
      url = await streamUrl(this.topics);
    } catch {
      // Ticket request failed (e.g. expired credential): back off and retry
      // like a stream error so the UI state stays honest.
      this.connecting = false;
      if (this.generation !== generation) return; // torn down while awaiting
      this.failures += 1;
      this.setState("retrying");
      this.retryTimer = setTimeout(() => {
        this.retryTimer = null;
        this.open();
      }, this.retryMs);
      this.retryMs = Math.min(this.retryMs * 2, RETRY_MAX_MS);
      return;
    }
    this.connecting = false;
    // Torn down or superseded while awaiting the ticket.
    if (this.generation !== generation || this.es || this.retryTimer) return;
    const es = new EventSource(url);
    this.es = es;
    es.onopen = () => {
      this.retryMs = RETRY_MIN_MS;
      this.failures = 0;
      this.setState("open");
    };
    es.onmessage = (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      const set = this.topicHandlers.get(msg && msg.topic);
      if (!set) return;
      set.forEach((fn) => {
        try {
          fn(msg.payload, msg);
        } catch { /* one bad handler must not starve the others */ }
      });
    };
    es.onerror = () => {
      if (this.es !== es) return; // superseded
      es.close();
      this.es = null;
      this.failures += 1;
      this.setState("retrying");
      this.retryTimer = setTimeout(() => {
        this.retryTimer = null;
        this.open();
      }, this.retryMs);
      this.retryMs = Math.min(this.retryMs * 2, RETRY_MAX_MS);
    };
  }

  teardown() {
    this.connecting = false;
    this.generation += 1;
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    if (this.es) {
      this.es.close();
      this.es = null;
    }
    this.setState("closed");
  }
}

/**
 * Subscribe to SSE topics. `topics` is a string or list of topic names
 * ("fleet", "alerts", "jobs", "env:{id}"). `handlers` maps topic -> fn(payload,
 * msg); the reserved key `onState` takes fn(state, failures) where state is
 * "open" | "retrying" | "unsupported" | "closed". Returns a handle whose
 * close() unsubscribes (and tears down the shared EventSource when the last
 * subscriber leaves).
 */
export function connect(topics, handlers = {}) {
  const list = (Array.isArray(topics) ? topics : [topics]).map(String).filter(Boolean);
  if (!list.length) return { close() {} };
  const key = [...list].sort().join(",");
  let ch = channels.get(key);
  if (!ch) {
    ch = new Channel([...list].sort());
    channels.set(key, ch);
  }
  const added = [];
  for (const [topic, fnOrList] of Object.entries(handlers)) {
    const fns = Array.isArray(fnOrList) ? fnOrList : [fnOrList];
    fns.forEach((fn) => {
      if (typeof fn !== "function") return;
      ch.add(topic, fn);
      added.push([topic, fn]);
    });
  }
  ch.refs += 1;
  ch.open();

  let closed = false;
  return {
    get state() {
      return ch.state;
    },
    close() {
      if (closed) return;
      closed = true;
      added.forEach(([topic, fn]) => ch.remove(topic, fn));
      ch.refs -= 1;
      if (ch.refs <= 0) {
        ch.teardown();
        channels.delete(key);
      }
    },
  };
}

/** Convenience: subscribe to the "alerts" topic. Returns the connect() handle. */
export function onAlert(fn) {
  return connect(["alerts"], { alerts: fn });
}

/** Tear down every channel (page teardown / logout). */
export function closeAll() {
  channels.forEach((ch) => ch.teardown());
  channels.clear();
}

window.addEventListener("beforeunload", closeAll);
