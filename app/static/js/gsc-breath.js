/* Quiet field behind the console, the same slow sine the P.Industries
   meadow uses. Stays behind the UI. Stops when the user asks for less motion. */
(function () {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const GREEN = [74, 222, 128];

  function meadow(canvas, opts) {
    const ctx = canvas.getContext("2d", { alpha: true });
    if (!ctx) return;
    let w = 0;
    let h = 0;
    let dpr = 1;

    function resize() {
      const rect = canvas.getBoundingClientRect();
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = Math.max(1, rect.width);
      h = Math.max(1, rect.height);
      canvas.width = Math.floor(w * dpr);
      canvas.height = Math.floor(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    resize();
    if (typeof ResizeObserver === "function") {
      new ResizeObserver(resize).observe(canvas);
    } else if (opts.full) {
      window.addEventListener("resize", resize);
    }

    const t0 = performance.now();
    function frame(now) {
      const t = (now - t0) / 1000;
      if (w < 2 || h < 2) resize();
      ctx.clearRect(0, 0, w, h);
      const cols = opts.cols;
      const rows = opts.rows;
      for (let r = 0; r < rows; r++) {
        for (let c = 0; c < cols; c++) {
          const bx = (c - cols / 2) * opts.sp;
          const bz = (r - rows / 2) * opts.sp;
          const d = Math.hypot(bx, bz);
          const wave = Math.sin(d * 0.32 - t * 1.4);
          const y = wave * opts.amp * Math.exp(-d * 0.012)
            + Math.sin(bx * 0.18 + t * 0.9) * 0.5
            + Math.cos(bz * 0.2 - t * 0.7) * 0.5;
          const x = (c + 0.5) * (w / cols);
          const py = (r + 0.5) * (h / rows) - y * opts.lift;
          const a = opts.alpha * (0.4 + 0.6 * (0.5 + 0.5 * wave));
          ctx.fillStyle = "rgba(" + GREEN[0] + "," + GREEN[1] + "," + GREEN[2] + "," + a.toFixed(3) + ")";
          ctx.beginPath();
          ctx.arc(x, py, opts.dot, 0, Math.PI * 2);
          ctx.fill();
        }
      }
      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  const page = document.getElementById("gsc-breath");
  if (page) {
    meadow(page, { cols: 36, rows: 20, sp: 1.05, amp: 1.7, lift: 2.8, dot: 1.05, alpha: 0.14, full: true });
  }
  document.querySelectorAll(".brand-breath canvas").forEach(function (canvas) {
    meadow(canvas, { cols: 16, rows: 7, sp: 1.15, amp: 1.6, lift: 2.2, dot: 1.35, alpha: 0.5, full: false });
  });
})();
