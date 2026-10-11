/* Approximations of the Halo firmware's sfxr presets (frame.sound.*).
   Not sample-exact: the firmware runs a full sfxr generator; this renders a
   recognisable sound of the right kind, length and volume. */
window.HaloSfx = (() => {
  let active = [];
  function rng(seed) {
    let s = (seed == null ? Math.floor(Math.random() * 2 ** 31) : seed) >>> 0 || 1;
    return () => ((s = (s * 1103515245 + 12345) >>> 0) / 2 ** 32);
  }
  function stop() { active.forEach((n) => { try { n.stop(); } catch (e) {} }); active = []; }
  function play(ctx, o) {
    const r = rng(o.seed);
    const dur = Math.max(0.03, (o.duration_ms || 1000) / 1000);
    const vol = Math.max(0, Math.min(1, (o.volume ?? 20) / 100)) * 0.6;
    const t0 = ctx.currentTime + 0.01;
    const out = ctx.createGain();
    out.connect(ctx.destination);
    const env = (g, attack, decayTo) => {
      g.gain.setValueAtTime(0.0001, t0);
      g.gain.exponentialRampToValueAtTime(vol, t0 + attack);
      g.gain.exponentialRampToValueAtTime(decayTo, t0 + dur);
    };
    const osc = (type, f0, f1, curve = "exp") => {
      const o2 = ctx.createOscillator();
      o2.type = type;
      o2.frequency.setValueAtTime(f0, t0);
      if (curve === "exp") o2.frequency.exponentialRampToValueAtTime(Math.max(20, f1), t0 + dur);
      else o2.frequency.linearRampToValueAtTime(f1, t0 + dur);
      o2.connect(out); o2.start(t0); o2.stop(t0 + dur + 0.02); active.push(o2);
      return o2;
    };
    const noise = (cut0, cut1) => {
      const len = Math.ceil(ctx.sampleRate * dur);
      const b = ctx.createBuffer(1, len, ctx.sampleRate);
      const d = b.getChannelData(0);
      for (let i = 0; i < len; i++) d[i] = r() * 2 - 1;
      const src = ctx.createBufferSource(); src.buffer = b;
      const f = ctx.createBiquadFilter(); f.type = "lowpass";
      f.frequency.setValueAtTime(cut0, t0); f.frequency.exponentialRampToValueAtTime(cut1, t0 + dur);
      src.connect(f); f.connect(out); src.start(t0); active.push(src);
    };
    switch (o.name) {
      case "pickup": {
        env(out, 0.005, 0.0001);
        const f = 600 + r() * 600;
        const o2 = osc("square", f, f, "lin");
        o2.frequency.setValueAtTime(f * (1.3 + r() * 0.5), t0 + dur * 0.25);
        break;
      }
      case "laser": env(out, 0.005, 0.0001); osc(r() < 0.5 ? "sawtooth" : "square", 900 + r() * 900, 120 + r() * 100); break;
      case "explosion": env(out, 0.005, 0.0001); noise(3000 + r() * 2000, 80); break;
      case "powerup": {
        env(out, 0.01, 0.001);
        const o2 = osc("square", 200 + r() * 200, 900 + r() * 500);
        const lfo = ctx.createOscillator(); const lg = ctx.createGain();
        lfo.frequency.value = 12 + r() * 10; lg.gain.value = 40; lfo.connect(lg); lg.connect(o2.frequency);
        lfo.start(t0); lfo.stop(t0 + dur); active.push(lfo);
        break;
      }
      case "hit": env(out, 0.003, 0.0001); noise(2500, 200); osc("square", 300 + r() * 200, 60); break;
      case "jump": env(out, 0.005, 0.0001); osc("square", 250 + r() * 150, 700 + r() * 300); break;
      case "blip": default: env(out, 0.003, 0.0001); osc("square", 500 + r() * 900, 500 + r() * 900, "lin"); break;
    }
  }
  return { play, stop };
})();
