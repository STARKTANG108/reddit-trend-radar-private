// 配图工作台：用 canvas 生成可直接复制/下载的推文配图。
(function () {
  const SIZES = { "16:9": [1600, 900], "1:1": [1080, 1080], "4:5": [1080, 1350] };
  const TEMPLATES = { news: "资讯卡", overlay: "原图 + 大标题", reddit: "帖子截图风" };
  const THEMES = { dark: "深邃黑", amber: "黑金琥珀", light: "极简白" };
  const FONT = '"PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif';
  const proxied = (u) => {
    if (u.startsWith("/") || u.startsWith("blob:") || u.startsWith("data:")) return u;
    if (window.RADAR_STATIC || location.hostname.endsWith(".github.io")) return "https://wsrv.nl/?url=" + encodeURIComponent(u);
    return "/api/image-proxy?url=" + encodeURIComponent(u);
  };
  const cache = new Map();

  function loadImage(src) {
    if (!cache.has(src)) {
      cache.set(src, new Promise((resolve, reject) => {
        const img = new Image();
        img.crossOrigin = "anonymous";
        img.onload = () => resolve(img);
        img.onerror = () => { cache.delete(src); reject(new Error("图片加载失败")); };
        img.src = src;
      }));
    }
    return cache.get(src);
  }

  function cover(ctx, img, x, y, w, h, r = 0) {
    const s = Math.max(w / img.width, h / img.height);
    const sw = w / s, sh = h / s;
    ctx.save();
    roundRect(ctx, x, y, w, h, r);
    ctx.clip();
    ctx.drawImage(img, (img.width - sw) / 2, (img.height - sh) / 2, sw, sh, x, y, w, h);
    ctx.restore();
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  // CJK breaks per character, latin per word.
  function wrap(ctx, text, maxW) {
    const tokens = text.match(/[\u2e80-\u9fff\u3000-\u303f\uff00-\uffef]|[^\s\u2e80-\u9fff\u3000-\u303f\uff00-\uffef]+|\s+/g) || [];
    const lines = [];
    let line = "";
    for (const t of tokens) {
      if (t.includes("\n")) { lines.push(line.trim()); line = ""; continue; }
      const test = line + t;
      if (ctx.measureText(test).width > maxW && line.trim()) { lines.push(line.trim()); line = t.trimStart(); }
      else line = test;
    }
    if (line.trim()) lines.push(line.trim());
    return lines;
  }

  // Largest font size in [min, max] whose wrapped text fits both maxLines and maxH.
  function fitText(ctx, text, maxW, maxLines, max, min, weight = 800, maxH = Infinity, lh = 1.28) {
    for (let size = max; size >= min; size -= 2) {
      ctx.font = `${weight} ${size}px ${FONT}`;
      const lines = wrap(ctx, text, maxW);
      if (lines.length <= maxLines && lines.length * size * lh <= maxH) return { size, lines };
    }
    ctx.font = `${weight} ${min}px ${FONT}`;
    const fitLines = Math.max(1, Math.min(maxLines, Math.floor(maxH / (min * lh))));
    const all = wrap(ctx, text, maxW);
    const lines = all.slice(0, fitLines);
    if (all.length > fitLines) lines[fitLines - 1] = lines[fitLines - 1].replace(/.{0,1}$/, "…");
    return { size: min, lines };
  }

  function drawLines(ctx, lines, x, y, size, lh = 1.28) {
    lines.forEach((l, i) => ctx.fillText(l, x, y + i * size * lh));
    return y + lines.length * size * lh;
  }

  function pill(ctx, text, x, y, size, bg, fg) {
    ctx.font = `700 ${size}px ${FONT}`;
    const w = ctx.measureText(text).width + size * 1.2;
    ctx.fillStyle = bg;
    roundRect(ctx, x, y, w, size * 1.8, size * 0.9);
    ctx.fill();
    ctx.fillStyle = fg;
    ctx.textBaseline = "middle";
    ctx.fillText(text, x + size * 0.6, y + size * 0.92);
    ctx.textBaseline = "alphabetic";
    return w;
  }

  async function draw(canvas, o) {
    const [W, H] = SIZES[o.size];
    canvas.width = W;
    canvas.height = H;
    const ctx = canvas.getContext("2d");
    const u = W / 1080;
    let img = null;
    if (o.image) { try { img = await loadImage(proxied(o.image)); } catch { img = null; } }
    const meta = `${o.comments.toLocaleString()} 条评论 · ${o.time}`;
    const theme = o.theme || "dark";

    if (o.template === "overlay") {
      const g = ctx.createLinearGradient(0, 0, W, H);
      g.addColorStop(0, "#1f2937"); g.addColorStop(1, "#111827");
      ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
      if (img) cover(ctx, img, 0, 0, W, H);
      const sh = ctx.createLinearGradient(0, H * 0.25, 0, H);
      sh.addColorStop(0, "rgba(0,0,0,0)"); sh.addColorStop(1, "rgba(0,0,0,0.88)");
      ctx.fillStyle = sh; ctx.fillRect(0, 0, W, H);
      pill(ctx, `r/${o.subreddit}`, 56 * u, 56 * u, 30 * u, theme === "amber" ? "#d97706" : "#ff4500", "#fff");
      const pad = 64 * u;
      const { size, lines } = fitText(ctx, o.headline, W - pad * 2, 3, 96 * u, 40 * u, 800, H * 0.5);
      ctx.fillStyle = "#fff";
      const top = H - pad - 60 * u - lines.length * size * 1.28;
      drawLines(ctx, lines, pad, top + size, size);
      ctx.font = `500 ${30 * u}px ${FONT}`;
      ctx.fillStyle = "rgba(255,255,255,.78)";
      ctx.fillText(o.subtitle || meta, pad, H - pad);
    } else if (o.template === "reddit") {
      const isDark = theme !== "light";
      ctx.fillStyle = isDark ? "#0b0f14" : "#dae0e6"; ctx.fillRect(0, 0, W, H);
      const m = 56 * u, cw = W - m * 2, ch = H - m * 2;
      ctx.fillStyle = isDark ? "#161b22" : "#fff"; roundRect(ctx, m, m, cw, ch, 24 * u); ctx.fill();
      const p = 44 * u;
      ctx.fillStyle = "#ff4500"; ctx.beginPath(); ctx.arc(m + p + 22 * u, m + p + 22 * u, 22 * u, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = "#fff"; ctx.font = `800 ${24 * u}px ${FONT}`; ctx.textAlign = "center"; ctx.fillText("r/", m + p + 22 * u, m + p + 31 * u); ctx.textAlign = "left";
      ctx.fillStyle = isDark ? "#f0f6fc" : "#1c1c1c"; ctx.font = `700 ${28 * u}px ${FONT}`; ctx.fillText(`r/${o.subreddit}`, m + p + 58 * u, m + p + 20 * u);
      ctx.fillStyle = isDark ? "#8b949e" : "#7c7c7c"; ctx.font = `400 ${22 * u}px ${FONT}`; ctx.fillText(`u/${o.author} · ${o.time}`, m + p + 58 * u, m + p + 48 * u);
      const tx = m + p, footer = 64 * u;
      const titleTop = m + p + 92 * u, contentBottom = m + ch - p - footer;
      const side = !!img && W / H > 1.3; // wide canvas: image beside the title
      const tw = side ? (cw - p * 2) * 0.55 : cw - p * 2;
      const imgMin = img && !side ? ch * 0.4 : 0;
      const titleMaxH = contentBottom - titleTop - imgMin - (img && !side ? 20 * u : 0);
      const { size, lines } = fitText(ctx, o.headline, tw, img && !side ? 4 : 7, (img ? 58 : 72) * u, 30 * u, 700, titleMaxH, 1.3);
      ctx.fillStyle = isDark ? "#f0f6fc" : "#1c1c1c";
      const yEnd = drawLines(ctx, lines, tx, titleTop + size, size, 1.3);
      if (side) {
        const ix = tx + tw + p * 0.8;
        cover(ctx, img, ix, titleTop - 10 * u, m + cw - p - ix, contentBottom - titleTop + 10 * u, 14 * u);
      } else if (img) {
        const iy = Math.max(yEnd - size * 0.6 + 16 * u, contentBottom - ch * 0.62);
        cover(ctx, img, tx, iy, tw, contentBottom - iy, 14 * u);
      }
      ctx.fillStyle = isDark ? "#8b949e" : "#7c7c7c"; ctx.font = `700 ${26 * u}px ${FONT}`;
      ctx.fillText(`▲ ${o.score.toLocaleString()}     ${o.comments.toLocaleString()} 条评论`, tx, m + ch - p + 4 * u);
    } else {
      const palette = {
        dark: { bg0: "#0b1220", bg1: "#1c2333", bar: "#ff4500", pillBg: "rgba(255,69,0,.16)", pillFg: "#ff8a4c", text: "#f5f7fa", sub: "#9aa7b8" },
        amber: { bg0: "#14110b", bg1: "#272015", bar: "#f59e0b", pillBg: "rgba(245,158,11,.18)", pillFg: "#fbbf24", text: "#fef3c7", sub: "#d6c5a3" },
        light: { bg0: "#f8fafc", bg1: "#eef2f6", bar: "#ff4500", pillBg: "rgba(255,69,0,.12)", pillFg: "#ea580c", text: "#0f172a", sub: "#475569" },
      }[theme] || { bg0: "#0b1220", bg1: "#1c2333", bar: "#ff4500", pillBg: "rgba(255,69,0,.16)", pillFg: "#ff8a4c", text: "#f5f7fa", sub: "#9aa7b8" };
      const g = ctx.createLinearGradient(0, 0, W, H);
      g.addColorStop(0, palette.bg0); g.addColorStop(1, palette.bg1);
      ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
      ctx.fillStyle = palette.bar; ctx.fillRect(0, 0, 14 * u, H);
      const pad = 72 * u;
      const wide = W / H > 1.3;
      const imgBox = img ? (wide ? { x: W * 0.56, y: pad, w: W * 0.44 - pad, h: H - pad * 2 } : { x: pad, y: H * 0.52, w: W - pad * 2, h: H * 0.48 - pad - 70 * u }) : null;
      const textW = img && wide ? W * 0.56 - pad * 1.6 : W - pad * 2;
      pill(ctx, `r/${o.subreddit}`, pad, pad, 30 * u, palette.pillBg, palette.pillFg);
      const top = pad + 100 * u;
      const bottom = (imgBox && !wide ? imgBox.y - 24 * u : H - pad - 50 * u) - (o.subtitle ? 110 * u : 0);
      const { size, lines } = fitText(ctx, o.headline, textW, img && !wide ? 3 : 5, (img ? 84 : 104) * u, 36 * u, 800, bottom - top);
      ctx.fillStyle = palette.text;
      const y = drawLines(ctx, lines, pad, top + size * 0.95, size);
      if (o.subtitle) {
        ctx.font = `500 ${32 * u}px ${FONT}`; ctx.fillStyle = palette.sub;
        drawLines(ctx, wrap(ctx, o.subtitle, textW).slice(0, 2), pad, y + 28 * u, 32 * u, 1.4);
      }
      if (imgBox) cover(ctx, img, imgBox.x, imgBox.y, imgBox.w, imgBox.h, 22 * u);
      ctx.font = `600 ${28 * u}px ${FONT}`; ctx.fillStyle = palette.pillFg;
      ctx.fillText(meta, pad, H - pad + 10 * u);
    }
    if (o.watermark) {
      ctx.font = `600 ${24 * u}px ${FONT}`;
      ctx.textAlign = "right";
      const lightBg = (o.template === "reddit" && theme === "light") || (o.template === "news" && theme === "light");
      ctx.fillStyle = lightBg ? "rgba(0,0,0,.38)" : "rgba(255,255,255,.55)";
      ctx.fillText(o.watermark, W - 40 * u, H - 30 * u);
      ctx.textAlign = "left";
    }
  }

  function mount(root, { post, tweets, assets, imageConfigured, fmtTime, toast, api, onAssets }) {
    const imagesList = [...(post.images || []), ...assets.map((a) => "/media/" + a.path)];
    const firstTweet = tweets.find((t) => t.status === "draft") || tweets[0] || {};
    const firstTitle = firstTweet.card_title || post.title;
    const st = {
      template: localStorage.getItem("studio.template") || "news",
      theme: localStorage.getItem("studio.theme") || "dark",
      size: localStorage.getItem("studio.size") || "16:9",
      image: imagesList[0] || "",
      headline: firstTitle,
      subtitle: "",
      watermark: localStorage.getItem("studio.watermark") || "",
    };
    const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    root.innerHTML = `
      <div class="studio">
        <div class="studio-side">
          <div class="field"><label>模板</label><div class="seg" data-k="template">${Object.entries(TEMPLATES).map(([k, v]) => `<button data-v="${k}">${v}</button>`).join("")}</div></div>
          <div class="field"><label>配色</label><div class="seg" data-k="theme">${Object.entries(THEMES).map(([k, v]) => `<button data-v="${k}">${v}</button>`).join("")}</div></div>
          <div class="field"><label>尺寸</label><div class="seg" data-k="size">${Object.keys(SIZES).map((k) => `<button data-v="${k}">${k}</button>`).join("")}</div></div>
          <div class="field"><label>图片 <span class="muted">（原帖 ${post.images.length} 张，支持上传或粘贴）</span></label>
            <div class="thumbs" id="studio-thumbs">
              <button class="thumb none" data-img="">无图</button>
              <button class="thumb upload" type="button" id="studio-upload-btn" title="上传本地图片或直接按 Ctrl/Cmd+V 粘贴截图">+ 本地/粘贴</button>
              ${imagesList.map((u) => `<button class="thumb" data-img="${esc(u)}"><img loading="lazy" src="${esc(proxied(u))}" onerror="this.parentNode.classList.add('broken')"/></button>`).join("")}
            </div>
            <input type="file" accept="image/*" id="studio-file" hidden /></div>
          <div class="field"><label>配图大标题</label><textarea rows="2" data-f="headline" style="min-height:64px">${esc(st.headline)}</textarea>
            ${tweets.length ? `<div class="chips">${[...new Set(tweets.map((t) => t.card_title).filter(Boolean))].map((t) => `<button class="chip" data-title="${esc(t)}">${esc(t)}</button>`).join("")}<button class="chip" data-title="${esc(post.title)}">原标题</button></div>` : ""}</div>
          <div class="field"><label>副标题（可选）</label><input type="text" data-f="subtitle" placeholder="留空则显示评论数和发帖时间" /></div>
          <div class="field"><label>署名 / 水印</label><input type="text" data-f="watermark" placeholder="@你的账号" value="${esc(st.watermark)}" /></div>
          ${imageConfigured ? `<div class="field"><label>AI 生成配图</label><textarea rows="3" data-f="prompt" style="min-height:70px">${esc(post.image_prompt || "")}</textarea>
            <button class="btn sm" data-a="ai">AI 生成</button></div>` : `<div class="help muted">配置 <code>IMAGE_MODEL</code> 后可用 AI 生成配图。</div>`}
        </div>
        <div class="studio-main">
          <canvas></canvas>
          <div class="row" style="justify-content:flex-end;margin-top:10px">
            <span class="muted" style="font-size:12px">原帖图片可能有版权，商用前请确认</span><span class="spacer"></span>
            <button class="btn" data-a="download">下载 PNG</button>
            <button class="btn" data-a="copy">复制图片</button>
            <button class="btn primary" data-a="copy-post-x" title="复制当前配图到剪贴板，并打开预填好文案的 X 发帖框（在 X 中直接按 Cmd/Ctrl+V 粘贴图片）">复制配图 + 去 X 发帖 ↗</button>
          </div>
        </div>
      </div>`;
    const canvas = root.querySelector("canvas");
    const meta = { subreddit: post.subreddit, author: post.author, comments: post.num_comments, score: post.score, time: fmtTime(post.created_utc) };
    let pending = 0;
    const render = async () => {
      const id = ++pending;
      root.querySelectorAll(".seg").forEach((s) => s.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.v === st[s.dataset.k])));
      root.querySelectorAll(".thumb[data-img]").forEach((b) => b.classList.toggle("active", b.dataset.img === st.image));
      const tmp = document.createElement("canvas");
      await draw(tmp, { ...st, ...meta });
      if (id !== pending) return;
      canvas.width = tmp.width; canvas.height = tmp.height;
      canvas.getContext("2d").drawImage(tmp, 0, 0);
    };
    const bindThumb = (b) => b.addEventListener("click", () => { st.image = b.dataset.img; render(); });
    root.querySelectorAll(".thumb[data-img]").forEach(bindThumb);

    const addLocalFile = (file) => {
      if (!file || !file.type.startsWith("image/")) return;
      const url = URL.createObjectURL(file);
      const btn = document.createElement("button");
      btn.className = "thumb";
      btn.dataset.img = url;
      btn.innerHTML = `<img src="${url}" />`;
      bindThumb(btn);
      root.querySelector("#studio-thumbs").appendChild(btn);
      st.image = url;
      render();
      toast("已载入本地图片");
    };
    const fileInp = root.querySelector("#studio-file");
    root.querySelector("#studio-upload-btn").addEventListener("click", () => fileInp.click());
    fileInp.addEventListener("change", (e) => addLocalFile(e.target.files[0]));
    root.addEventListener("paste", (e) => {
      const item = [...(e.clipboardData?.items || [])].find((i) => i.type.startsWith("image/"));
      if (item) { e.preventDefault(); addLocalFile(item.getAsFile()); }
    });

    root.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => {
      const k = b.parentNode.dataset.k;
      st[k] = b.dataset.v;
      localStorage.setItem("studio." + k, st[k]);
      render();
    }));
    root.querySelectorAll(".chip").forEach((b) => b.addEventListener("click", () => {
      st.headline = b.dataset.title;
      root.querySelector('[data-f="headline"]').value = st.headline;
      render();
    }));
    root.querySelectorAll("[data-f]").forEach((inp) => inp.addEventListener("input", () => {
      if (inp.dataset.f === "prompt") return;
      st[inp.dataset.f] = inp.value;
      if (inp.dataset.f === "watermark") localStorage.setItem("studio.watermark", inp.value);
      render();
    }));
    const blob = () => new Promise((r) => canvas.toBlob(r, "image/png"));
    root.querySelector('[data-a="download"]').addEventListener("click", async () => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(await blob());
      a.download = `${post.subreddit}_${post.id}.png`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    });
    root.querySelector('[data-a="copy"]').addEventListener("click", async () => {
      try {
        await navigator.clipboard.write([new ClipboardItem({ "image/png": blob() })]);
        toast("图片已复制，可直接粘贴到 X");
      } catch (e) {
        toast("浏览器不允许复制图片，请用“下载 PNG”", "error");
      }
    });
    root.querySelector('[data-a="copy-post-x"]').addEventListener("click", async () => {
      const activeTa = document.querySelector("#dr-tweets textarea");
      const copyBody = activeTa ? activeTa.value : (firstTweet.content || post.title);
      window.open("https://x.com/intent/post?text=" + encodeURIComponent(copyBody), "_blank", "noopener");
      try {
        await navigator.clipboard.write([new ClipboardItem({ "image/png": blob() })]);
        toast("配图已复制并打开 X，在发帖框按 Cmd/Ctrl+V 即可粘贴配图！");
      } catch (e) {
        toast("已打开 X 发帖框，请点击“下载 PNG”上传配图", "error");
      }
    });
    const ai = root.querySelector('[data-a="ai"]');
    if (ai) ai.addEventListener("click", async () => {
      ai.disabled = true;
      ai.textContent = "生成中…（约 10-60 秒）";
      try {
        const a = await api(`/posts/${post.id}/ai-image`, { method: "POST", body: { prompt: root.querySelector('[data-f="prompt"]').value } });
        toast("AI 配图已生成");
        onAssets(a);
      } catch (e) {
        toast(e.message, "error");
      } finally {
        ai.disabled = false;
        ai.textContent = "AI 生成";
      }
    });
    render();
  }

  window.Studio = { mount };
})();
