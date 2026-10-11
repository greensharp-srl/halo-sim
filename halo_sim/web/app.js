/* Halo SIM phone client: the phone is the glasses' hardware. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const PHONE_RATE = 16000;
  const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

  const st = {
    ws: null, connected: false, retry: 500,
    micOn: false, micStream: null, micNode: null,
    camStream: null, facing: "environment",
    ctx: null, spkGain: null, spkNext: 0, spkVolume: 50,
    imuOn: false, lastImu: 0, orient: { beta: 90, gamma: 0, heading: 0 }, accel: [0, 0, 1000],
    hostApp: null,
    stats: { spkChunks: 0, spkSeconds: 0, sfx: 0, photos: 0, micChunks: 0, frames: 0 },
  };
  window.haloSim = st; // for debugging from the browser console

  // ------------------------------------------------------------------ UI helpers
  const logEl = $("log");
  function log(line, cls) {
    const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 30;
    const div = document.createElement("div");
    if (cls) div.className = cls;
    div.textContent = line;
    logEl.appendChild(div);
    while (logEl.childNodes.length > 400) logEl.removeChild(logEl.firstChild);
    if (atBottom) logEl.scrollTop = logEl.scrollHeight;
  }
  let toastT = 0;
  function toast(msg) {
    const t = $("toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toastT);
    toastT = setTimeout(() => t.classList.remove("show"), 1100);
  }

  // ------------------------------------------------------------------ websocket
  function send(obj) {
    if (st.ws && st.ws.readyState === 1) st.ws.send(JSON.stringify(obj));
  }
  function sendBin(tag, ...parts) {
    if (!st.ws || st.ws.readyState !== 1) return;
    let len = 1;
    for (const p of parts) len += p.byteLength;
    const out = new Uint8Array(len);
    out[0] = tag.charCodeAt(0);
    let o = 1;
    for (const p of parts) { out.set(new Uint8Array(p.buffer || p, p.byteOffset || 0, p.byteLength), o); o += p.byteLength; }
    st.ws.send(out);
  }

  function connect() {
    const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/phone`;
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    st.ws = ws;
    ws.onopen = () => {
      st.connected = true; st.retry = 500;
      $("conn").className = "dot on";
      send({ t: "caps", camera: !!st.camStream, mic: !!st.micStream, audio: !!st.ctx });
      sendBattery();
    };
    ws.onclose = () => {
      st.connected = false;
      $("conn").className = "dot off";
      $("state").textContent = "reconnecting…";
      setTimeout(connect, st.retry);
      st.retry = Math.min(st.retry * 2, 5000);
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") onJson(JSON.parse(ev.data));
      else onBinary(new Uint8Array(ev.data));
    };
  }

  function onJson(m) {
    switch (m.t) {
      case "hello": log(`connected to ${m.device}`, "sys"); break;
      case "status": {
        $("state").textContent = m.state;
        st.hostApp = m.host;
        $("host").textContent = m.app ? `· ${m.app}` : m.host ? `· ${m.host}` : "· launcher";
        $("batt").textContent = `${m.battery}%`;
        if (st.micOn) $("mic-level").style.setProperty("--lvl", `${Math.max(0, Math.min(100, (m.mic_db + 60) * 1.7))}%`);
        break;
      }
      case "mic": setMic(m.on); break;
      case "speaker":
        $("spk-ind").hidden = !m.on;
        if (m.volume != null) setVolume(m.volume);
        if (!m.on) st.spkNext = 0;
        break;
      case "sfx": st.stats.sfx++; if (st.ctx) { if (m.stop) HaloSfx.stop(); else HaloSfx.play(st.ctx, m); } break;
      case "flash": flash(); break;
      case "photo": takePhoto(m.id); break;
      case "display": applyDisplayState(m); break;
      case "log": log(m.line, m.line.startsWith("[halo-sim]") ? "sys" : /error|interrupted/i.test(m.line) ? "err" : ""); break;
    }
  }

  function onBinary(b) {
    const tag = String.fromCharCode(b[0]);
    if (tag === "F") drawFrame(b.subarray(1));
    else if (tag === "S") playPcm(b);
  }

  // ------------------------------------------------------------------ display
  const canvas = $("display");
  const g = canvas.getContext("2d");
  g.imageSmoothingEnabled = false;
  let drawing = false, pendingFrame = null;
  async function drawFrame(png) {
    if (drawing) { pendingFrame = png; return; }
    drawing = true;
    try {
      const bmp = await createImageBitmap(new Blob([png], { type: "image/png" }));
      g.drawImage(bmp, 0, 0);
      st.stats.frames++;
      bmp.close && bmp.close();
    } catch (e) { /* ignore a bad frame */ }
    drawing = false;
    if (pendingFrame) { const p = pendingFrame; pendingFrame = null; drawFrame(p); }
  }
  function applyDisplayState(m) {
    const note = $("lens-note");
    let opacity = 0.35 + 0.65 * ((m.brightness ?? 50) / 100);
    note.textContent = "";
    if (m.asleep) { opacity = 0.05; note.textContent = "deep sleep · press BUTTON to wake"; }
    else if (m.power_save) { opacity = Math.min(opacity, 0.18); note.textContent = "display power save (dark on real Halo)"; }
    canvas.style.opacity = opacity;
    const [px, py] = m.pan || [0, 0];
    canvas.style.transform = `translate(${(px / 256) * 100}%, ${(py / 256) * 100}%)`;
  }
  function flash() {
    const f = $("flash");
    f.classList.add("on");
    requestAnimationFrame(() => requestAnimationFrame(() => f.classList.remove("on")));
  }

  // ------------------------------------------------------------------ button + taps
  function setupButton() {
    const el = $("btn");
    let downAt = 0, pendingSingle = 0, longTimer = 0, offTimer = 0, longFired = false;
    const LONG_MS = 1000, DOUBLE_MS = 350;
    const down = (e) => {
      e.preventDefault();
      if (downAt) return;
      downAt = performance.now();
      el.classList.add("down");
      longFired = false;
      // iOS has no navigator.vibrate, so the pad itself shows when the hold counts as long
      longTimer = setTimeout(() => { longFired = true; el.classList.add("long"); navigator.vibrate && navigator.vibrate(30); }, LONG_MS);
      offTimer = setTimeout(() => el.classList.add("off"), 2000);
    };
    const up = (e) => {
      e.preventDefault();
      if (!downAt) return;
      const held = performance.now() - downAt;
      downAt = 0;
      el.classList.remove("down", "long", "off");
      clearTimeout(longTimer); clearTimeout(offTimer);
      if (held >= 2000) { toast("2 s hold = power off on real Halo (not simulated)"); return; }
      if (longFired) { fire("button", "long"); return; }
      if (pendingSingle) { clearTimeout(pendingSingle); pendingSingle = 0; fire("button", "double"); return; }
      pendingSingle = setTimeout(() => { pendingSingle = 0; fire("button", "single"); }, DOUBLE_MS);
    };
    el.addEventListener("pointerdown", down);
    el.addEventListener("pointerup", up);
    el.addEventListener("pointercancel", up);
    el.addEventListener("contextmenu", (e) => e.preventDefault());
  }
  function setupTap() {
    const el = $("tap");
    let count = 0, timer = 0;
    el.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      el.classList.add("down");
      setTimeout(() => el.classList.remove("down"), 90);
      count = Math.min(count + 1, 3);
      clearTimeout(timer);
      // like the BMA580 detector: confirm only after the multi-tap window expires
      timer = setTimeout(() => { fire("tap", ["single", "double", "triple"][count - 1]); count = 0; }, 400);
    });
    el.addEventListener("contextmenu", (e) => e.preventDefault());
  }
  function fire(t, kind) {
    send({ t, kind });
    toast(t === "button" ? `button: ${kind}` : `tap: ${kind}`);
    navigator.vibrate && navigator.vibrate(12);
  }
  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT" || e.repeat) return;
    const k = e.key.toLowerCase();
    const map = { " ": ["button", "single"], d: ["button", "double"], l: ["button", "long"],
      t: ["tap", "single"], 2: ["tap", "double"], 3: ["tap", "triple"] };
    if (map[k]) { e.preventDefault(); fire(...map[k]); }
    else if (k === "h") { e.preventDefault(); send({ t: "home" }); toast("home"); }
  });

  // ------------------------------------------------------------------ camera
  const camConstraints = () => ({ facingMode: { ideal: st.facing }, width: { ideal: 1280 }, height: { ideal: 960 } });
  const MIC_CONSTRAINTS = { channelCount: 1, echoCancellation: true, noiseSuppression: false, autoGainControl: false };
  async function startCamera(stream) {
    if (st.camStream) st.camStream.getTracks().forEach((t) => t.stop());
    st.camStream = stream || await navigator.mediaDevices.getUserMedia({ audio: false, video: camConstraints() });
    const v = $("cam");
    v.srcObject = st.camStream;
    await v.play().catch(() => {});
    const settings = st.camStream.getVideoTracks()[0].getSettings();
    document.body.classList.toggle("mirror", settings.facingMode === "user" || (!settings.facingMode && !isMobile()));
  }
  function isMobile() { return /Android|iPhone|iPad|iPod/.test(navigator.userAgent) || isIOS; }
  const shot = document.createElement("canvas");
  shot.width = 640; shot.height = 480;
  function takePhoto(id) {
    const v = $("cam");
    if (!st.camStream || !v.videoWidth) { send({ t: "photo_fail", id }); return; }
    // centre-crop to 4:3 like the PAG7982J1 640x480 sensor
    const vw = v.videoWidth, vh = v.videoHeight, target = 4 / 3;
    let sw = vw, sh = vh;
    if (vw / vh > target) sw = vh * target; else sh = vw / target;
    const sx = (vw - sw) / 2, sy = (vh - sh) / 2;
    const c = shot.getContext("2d");
    // the preview may be mirrored (selfie camera), the glasses' photo never is
    c.drawImage(v, sx, sy, sw, sh, 0, 0, 640, 480);
    shot.toBlob(async (blob) => {
      if (!blob) { send({ t: "photo_fail", id }); return; }
      const idb = new Uint8Array(4);
      new DataView(idb.buffer).setUint32(0, id, true);
      sendBin("C", idb, new Uint8Array(await blob.arrayBuffer()));
      st.stats.photos++;
    }, "image/jpeg", 0.92);
  }

  // ------------------------------------------------------------------ audio out
  function ensureAudio() {
    if (st.ctx) return;
    const AC = window.AudioContext || window.webkitAudioContext;
    st.ctx = new AC({ latencyHint: "interactive" });
    st.spkGain = st.ctx.createGain();
    st.spkGain.connect(st.ctx.destination);
    setVolume(st.spkVolume);
  }
  function setVolume(v) {
    st.spkVolume = v;
    if (st.spkGain) st.spkGain.gain.value = Math.max(0, Math.min(1, v / 100)) * 1.6;
  }
  function playPcm(b) {
    if (!st.ctx) return;
    const dv = new DataView(b.buffer, b.byteOffset, b.byteLength);
    const rate = dv.getUint16(1, true), ch = b[3];
    const nBytes = b.byteLength - 4;
    const frames = Math.floor(nBytes / 2 / ch);
    if (!frames) return;
    const sample = (i, c) => dv.getInt16(4 + (i * ch + c) * 2, true) / 32768;
    let buf;
    try {
      buf = st.ctx.createBuffer(ch, frames, rate);
      for (let c = 0; c < ch; c++) {
        const out = buf.getChannelData(c);
        for (let i = 0; i < frames; i++) out[i] = sample(i, c);
      }
    } catch (e) {
      // older Safari rejects buffers below 22.05 kHz: upsample to the context rate
      const k = st.ctx.sampleRate / rate, n = Math.floor(frames * k);
      buf = st.ctx.createBuffer(ch, n, st.ctx.sampleRate);
      for (let c = 0; c < ch; c++) {
        const out = buf.getChannelData(c);
        for (let j = 0; j < n; j++) {
          const x = j / k, i0 = Math.floor(x), i1 = Math.min(frames - 1, i0 + 1), f = x - i0;
          out[j] = sample(i0, c) * (1 - f) + sample(i1, c) * f;
        }
      }
    }
    const src = st.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(st.spkGain);
    const now = st.ctx.currentTime;
    if (st.spkNext < now + 0.02) st.spkNext = now + 0.08; // small jitter buffer
    src.start(st.spkNext);
    st.stats.spkChunks++; st.stats.spkSeconds += buf.duration;
    st.spkNext += buf.duration;
  }

  // ------------------------------------------------------------------ microphone
  async function startMic(stream) {
    st.micStream = stream || await navigator.mediaDevices.getUserMedia({ video: false, audio: MIC_CONSTRAINTS });
    ensureAudio();
    const ctx = st.ctx;
    const srcNode = ctx.createMediaStreamSource(st.micStream);
    const ratio = ctx.sampleRate / PHONE_RATE;
    let acc = new Float32Array(0), pos = 0;
    const onSamples = (f32) => {
      if (!st.micOn) return;
      // linear-interpolating resampler to 16 kHz with a box pre-filter
      const merged = new Float32Array(acc.length + f32.length);
      merged.set(acc); merged.set(f32, acc.length);
      const out = [];
      while (pos + ratio < merged.length - 1) {
        const i0 = Math.floor(pos);
        let s = 0, n = 0;
        for (let k = Math.max(0, Math.floor(pos - ratio / 2)); k <= Math.min(merged.length - 1, Math.floor(pos + ratio / 2)); k++) { s += merged[k]; n++; }
        out.push(n ? s / n : merged[i0]);
        pos += ratio;
      }
      const keep = Math.floor(pos);
      acc = merged.slice(keep);
      pos -= keep;
      const pcm = new Int16Array(out.length);
      for (let i = 0; i < out.length; i++) pcm[i] = Math.max(-32768, Math.min(32767, Math.round(out[i] * 32767)));
      if (pcm.length) { sendBin("M", pcm); st.stats.micChunks++; }
    };
    try {
      await ctx.audioWorklet.addModule("/static/mic-worklet.js");
      const node = new AudioWorkletNode(ctx, "halo-mic");
      node.port.onmessage = (e) => onSamples(e.data);
      srcNode.connect(node);
      st.micNode = node;
    } catch (e) {
      const sp = ctx.createScriptProcessor(2048, 1, 1);
      sp.onaudioprocess = (ev) => onSamples(new Float32Array(ev.inputBuffer.getChannelData(0)));
      srcNode.connect(sp);
      const mute = ctx.createGain(); mute.gain.value = 0;
      sp.connect(mute); mute.connect(ctx.destination);
      st.micNode = sp;
    }
  }
  function setMic(on) {
    st.micOn = on && !!st.micStream;
    $("mic-ind").hidden = !on;
    if (on && !st.micStream) log("Lua started the microphone but this device has no mic enabled", "err");
  }

  // ------------------------------------------------------------------ IMU
  async function startImu() {
    try {
      if (window.DeviceOrientationEvent && typeof DeviceOrientationEvent.requestPermission === "function") {
        if ((await DeviceOrientationEvent.requestPermission()) !== "granted") return;
      }
      if (window.DeviceMotionEvent && typeof DeviceMotionEvent.requestPermission === "function") {
        await DeviceMotionEvent.requestPermission().catch(() => {});
      }
    } catch (e) { return; }
    st.imuOn = true;
    window.addEventListener("deviceorientation", (e) => {
      if (e.beta == null) return;
      st.orient.beta = e.beta; st.orient.gamma = e.gamma;
      if (e.webkitCompassHeading != null) st.orient.heading = e.webkitCompassHeading;
      else if (e.absolute && e.alpha != null) st.orient.heading = 360 - e.alpha;
      sendImu();
    });
    window.addEventListener("devicemotion", (e) => {
      const a = e.accelerationIncludingGravity;
      if (!a || a.x == null) return;
      const s = isIOS ? -1 : 1; // iOS reports the opposite sign convention
      const mg = (v) => Math.round((s * v / 9.80665) * 1000);
      // phone held upright like a lens: gravity along the phone's Y axis,
      // which is Halo's X axis when worn ("device gravity sits on X")
      st.accel = [mg(a.y), mg(a.x), mg(a.z)];
    });
  }
  function sendImu() {
    const now = performance.now();
    if (now - st.lastImu < 50) return; // 20 Hz
    st.lastImu = now;
    const pitch = st.orient.beta - 90;   // phone upright = level head; tilt back = look up
    const roll = st.orient.gamma;
    const h = (st.orient.heading * Math.PI) / 180;
    const compass = [Math.round(20 * Math.cos(h) * 10) / 10, Math.round(-20 * Math.sin(h) * 10) / 10, -40];
    send({ t: "imu", pitch, roll, accel: st.accel, compass });
  }

  // ------------------------------------------------------------------ battery
  async function sendBattery() {
    if (!navigator.getBattery) return;
    try {
      const b = await navigator.getBattery();
      const push = () => send({ t: "battery", level: Math.round(b.level * 100), charging: b.charging });
      push();
      b.onlevelchange = b.onchargingchange = push;
    } catch (e) {}
  }

  // ------------------------------------------------------------------ console + chips
  $("home").onclick = () => send({ t: "home" });
  $("con").onclick = () => { const c = $("console"); c.hidden = !c.hidden; $("con").classList.toggle("on", !c.hidden); logEl.scrollTop = logEl.scrollHeight; };
  $("see").onclick = () => { document.body.classList.toggle("opaque"); $("see").classList.toggle("on", !document.body.classList.contains("opaque")); };
  $("flip").onclick = async () => {
    st.facing = st.facing === "environment" ? "user" : "environment";
    try { await startCamera(); } catch (e) { toast("camera switch failed"); }
  };
  $("repl").onsubmit = (e) => {
    e.preventDefault();
    const code = $("lua").value.trim();
    if (!code) return;
    if (st.hostApp) { toast("a host app owns the link"); return; }
    log(`> ${code}`, "sys");
    send({ t: "lua", code });
    $("lua").value = "";
  };
  $("brk").onclick = () => send({ t: "control", kind: "break" });
  $("rst").onclick = () => send({ t: "control", kind: "restart" });

  // ------------------------------------------------------------------ start
  async function start() {
    $("go").disabled = true;
    ensureAudio();
    try { await st.ctx.resume(); } catch (e) {}
    const errs = [];
    if ($("opt-imu").checked) await startImu();
    // one combined request = one iOS permission prompt instead of two; fall back to separate requests
    let cam = null, mic = null;
    if ($("opt-cam").checked && $("opt-mic").checked) {
      try {
        const both = await navigator.mediaDevices.getUserMedia({ video: camConstraints(), audio: MIC_CONSTRAINTS });
        cam = new MediaStream(both.getVideoTracks());
        mic = new MediaStream(both.getAudioTracks());
      } catch (e) {}
    }
    if ($("opt-cam").checked) { try { await startCamera(cam); } catch (e) { errs.push(`camera: ${e.message || e}`); } }
    if ($("opt-mic").checked) { try { await startMic(mic); } catch (e) { errs.push(`mic: ${e.message || e}`); } }
    if (!st.camStream) document.body.classList.add("opaque");
    $("see").classList.toggle("on", !document.body.classList.contains("opaque"));
    $("start").hidden = true;
    errs.forEach((e) => log(e, "err"));
    if (st.ws && st.ws.readyState === 1) send({ t: "caps", camera: !!st.camStream, mic: !!st.micStream, audio: true });
    setupButton(); setupTap();
  }

  async function init() {
    const secure = window.isSecureContext && navigator.mediaDevices;
    if (!secure) {
      $("insecure").hidden = false;
      try {
        const info = await (await fetch("/api/info")).json();
        $("ca-link").href = `http://${location.hostname}:${info.http_port}/ca.crt`;
        $("https-link").href = `https://${location.hostname}:${info.https_port}/`;
        $("https-link").textContent = $("https-link").href;
      } catch (e) {}
      $("opt-cam").checked = $("opt-mic").checked = false;
      $("opt-cam").disabled = $("opt-mic").disabled = true;
    }
    $("go").onclick = start;
    connect();
    if (new URLSearchParams(location.search).has("autostart")) start();
  }
  init();
})();
