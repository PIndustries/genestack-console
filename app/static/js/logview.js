// Live log boxes. Open on the newest line. Stay there while output arrives.
// Scrolling up stops that. Latest jumps back and resumes.

const NEAR = 24;
const boxes = new Map();

function keyOf(el) {
  return el && el.id ? el.id : "";
}

function gap(el) {
  return el.scrollHeight - el.scrollTop - el.clientHeight;
}

export function resetLiveLog(id) {
  boxes.set(id, { follow: true, top: 0, pinning: false });
}

function pin(el, rec) {
  rec.pinning = true;
  el.scrollTop = el.scrollHeight;
  requestAnimationFrame(() => {
    if (el.isConnected) el.scrollTop = el.scrollHeight;
    rec.pinning = false;
  });
}

// Attach once per element. A replaced element with the same id keeps the
// follow flag from the previous one.
export function bindLiveLog(el, button) {
  const id = keyOf(el);
  if (!el || !id) return;
  if (!boxes.has(id)) resetLiveLog(id);
  if (el.dataset.liveBound !== "1") {
    el.dataset.liveBound = "1";
    el.addEventListener("scroll", () => {
      const rec = boxes.get(id);
      if (!rec || rec.pinning) return;
      rec.follow = gap(el) <= NEAR;
      rec.top = el.scrollTop;
    });
  }
  if (button && button.dataset.liveBound !== "1") {
    button.dataset.liveBound = "1";
    button.addEventListener("click", () => {
      const rec = boxes.get(id) || { follow: true, top: 0, pinning: false };
      rec.follow = true;
      boxes.set(id, rec);
      pin(el, rec);
    });
  }
  stickLiveLog(el);
}

export function stickLiveLog(el) {
  const id = keyOf(el);
  const rec = id && boxes.get(id);
  if (!el || !rec) return;
  if (rec.follow) pin(el, rec);
  else {
    rec.pinning = true;
    el.scrollTop = rec.top || 0;
    requestAnimationFrame(() => {
      rec.pinning = false;
    });
  }
}
