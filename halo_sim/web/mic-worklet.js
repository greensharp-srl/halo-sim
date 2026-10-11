// Collects mic samples and posts ~20 ms Float32 blocks to the main thread.
class HaloMic extends AudioWorkletProcessor {
  constructor() { super(); this.buf = new Float32Array(1024); this.n = 0; this.block = Math.round(sampleRate / 50); }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) {
      for (let i = 0; i < ch.length; i++) {
        if (this.n === this.buf.length) { const b = new Float32Array(this.buf.length * 2); b.set(this.buf); this.buf = b; }
        this.buf[this.n++] = ch[i];
      }
      if (this.n >= this.block) { this.port.postMessage(this.buf.slice(0, this.n)); this.n = 0; }
    }
    return true;
  }
}
registerProcessor("halo-mic", HaloMic);
