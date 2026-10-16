// Tiny dependency-free SVG charts.
(function () {
  const NS = "http://www.w3.org/2000/svg";
  let tip;
  function tooltip() {
    if (!tip) { tip = document.createElement("div"); tip.className = "chart-tip"; document.body.appendChild(tip); }
    return tip;
  }
  function el(tag, attrs, parent) {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }
  function niceMax(v) {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
    return 10 * p;
  }
  function legend(container, items) {
    const lg = document.createElement("div");
    lg.className = "legend";
    lg.innerHTML = items.map((s) => `<span><i style="background:${s.color}"></i>${s.name}</span>`).join("");
    container.appendChild(lg);
  }

  function lineChart(container, { series, height = 220, xFmt = (x) => x, yFmt = (y) => Math.round(y) }) {
    container.innerHTML = "";
    container.classList.add("chart");
    const W = Math.max(320, container.clientWidth || 600), H = height;
    const pad = { l: 46, r: 12, t: 10, b: 24 };
    const all = series.flatMap((s) => s.points);
    if (!all.length) { container.innerHTML = '<div class="empty">暂无数据</div>'; return; }
    const xmin = Math.min(...all.map((p) => p.x)), xmax = Math.max(...all.map((p) => p.x)) || 1;
    const ymax = niceMax(Math.max(...all.map((p) => p.y)) * 1.05);
    const X = (x) => pad.l + ((x - xmin) / (xmax - xmin || 1)) * (W - pad.l - pad.r);
    const Y = (y) => H - pad.b - (y / ymax) * (H - pad.t - pad.b);
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, height: H });
    const g = el("g", { class: "grid" }, svg), ax = el("g", { class: "axis" }, svg);
    for (let i = 0; i <= 4; i++) {
      const v = (ymax / 4) * i;
      el("line", { x1: pad.l, x2: W - pad.r, y1: Y(v), y2: Y(v) }, g);
      el("text", { x: pad.l - 6, y: Y(v) + 4, "text-anchor": "end" }, ax).textContent = yFmt(v);
    }
    for (let i = 0; i <= 4; i++) {
      const v = xmin + ((xmax - xmin) / 4) * i;
      el("text", { x: X(v), y: H - 6, "text-anchor": i === 0 ? "start" : i === 4 ? "end" : "middle" }, ax).textContent = xFmt(v);
    }
    for (const s of series) {
      if (!s.points.length) continue;
      const d = s.points.map((p, i) => `${i ? "L" : "M"}${X(p.x).toFixed(1)},${Y(p.y).toFixed(1)}`).join("");
      if (s.fill) el("path", { d: `${d}L${X(s.points.at(-1).x)},${Y(0)}L${X(s.points[0].x)},${Y(0)}Z`, fill: s.color, opacity: 0.12 }, svg);
      el("path", { d, fill: "none", stroke: s.color, "stroke-width": s.width || 2, "stroke-dasharray": s.dash || "", "stroke-linejoin": "round" }, svg);
      if (s.dots) s.points.forEach((p) => el("circle", { cx: X(p.x), cy: Y(p.y), r: 2.6, fill: s.color }, svg));
    }
    const cross = el("line", { y1: pad.t, y2: H - pad.b, stroke: "#4b5563", "stroke-dasharray": "3 3", visibility: "hidden" }, svg);
    svg.addEventListener("mousemove", (e) => {
      const rect = svg.getBoundingClientRect();
      const px = ((e.clientX - rect.left) / rect.width) * W;
      const x = xmin + ((px - pad.l) / (W - pad.l - pad.r)) * (xmax - xmin);
      cross.setAttribute("x1", X(Math.min(Math.max(x, xmin), xmax)));
      cross.setAttribute("x2", X(Math.min(Math.max(x, xmin), xmax)));
      cross.setAttribute("visibility", "visible");
      const lines = series.filter((s) => s.points.length).map((s) => {
        const p = s.points.reduce((a, b) => (Math.abs(b.x - x) < Math.abs(a.x - x) ? b : a));
        return `<span style="color:${s.color}">●</span> ${s.name}: <b>${yFmt(p.y)}</b> <span class="muted">@${xFmt(p.x)}</span>`;
      });
      const t = tooltip();
      t.innerHTML = lines.join("<br>");
      t.style.display = "block";
      t.style.left = e.clientX + 14 + "px";
      t.style.top = e.clientY + 10 + "px";
    });
    svg.addEventListener("mouseleave", () => { cross.setAttribute("visibility", "hidden"); tooltip().style.display = "none"; });
    container.appendChild(svg);
    legend(container, series);
  }

  function stackedBar(container, { labels, stacks, line, height = 220, xFmt = (x) => x }) {
    container.innerHTML = "";
    container.classList.add("chart");
    const W = Math.max(320, container.clientWidth || 600), H = height;
    const pad = { l: 46, r: 12, t: 10, b: 24 };
    const totals = labels.map((_, i) => stacks.reduce((a, s) => a + (s.values[i] || 0), 0));
    const ymax = niceMax(Math.max(...totals, line ? line.value : 0) * 1.1);
    const bw = (W - pad.l - pad.r) / labels.length;
    const Y = (y) => H - pad.b - (y / ymax) * (H - pad.t - pad.b);
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, height: H });
    const g = el("g", { class: "grid" }, svg), ax = el("g", { class: "axis" }, svg);
    for (let i = 0; i <= 4; i++) {
      const v = (ymax / 4) * i;
      el("line", { x1: pad.l, x2: W - pad.r, y1: Y(v), y2: Y(v) }, g);
      el("text", { x: pad.l - 6, y: Y(v) + 4, "text-anchor": "end" }, ax).textContent = Math.round(v);
    }
    const step = Math.ceil(labels.length / 8);
    labels.forEach((lb, i) => {
      let acc = 0;
      const x = pad.l + i * bw + bw * 0.15;
      const group = el("g", {}, svg);
      for (const s of stacks) {
        const v = s.values[i] || 0;
        if (!v) continue;
        el("rect", { x, width: bw * 0.7, y: Y(acc + v), height: Y(acc) - Y(acc + v), fill: s.color, rx: 2 }, group);
        acc += v;
      }
      el("rect", { x: pad.l + i * bw, width: bw, y: pad.t, height: H - pad.t - pad.b, fill: "transparent" }, group);
      if (i % step === 0 || i === labels.length - 1) el("text", { x: x + bw * 0.35, y: H - 6, "text-anchor": "middle" }, ax).textContent = xFmt(lb);
      group.addEventListener("mousemove", (e) => {
        const t = tooltip();
        t.innerHTML = `<b>${lb}</b> 合计 ${totals[i]}<br>` + stacks.map((s) => `<span style="color:${s.color}">●</span> ${s.name}: ${s.values[i] || 0}`).join("<br>");
        t.style.display = "block";
        t.style.left = e.clientX + 14 + "px";
        t.style.top = e.clientY + 10 + "px";
      });
      group.addEventListener("mouseleave", () => (tooltip().style.display = "none"));
    });
    if (line) {
      el("line", { x1: pad.l, x2: W - pad.r, y1: Y(line.value), y2: Y(line.value), stroke: line.color, "stroke-dasharray": "5 4", "stroke-width": 1.5 }, svg);
    }
    container.appendChild(svg);
    legend(container, line ? [...stacks, line] : stacks);
  }

  window.Charts = { lineChart, stackedBar };
})();
