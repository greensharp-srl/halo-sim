# Halo SIM

> Unofficial community project, not affiliated with or endorsed by Brilliant Labs.

A simulator for [Brilliant Labs Halo](https://brilliant.xyz/) glasses that uses your **phone as the glasses' hardware**.
You build Python apps with the official [`brilliant-sdk`](https://github.com/brilliantlabsAR/brilliant_sdk) now, and run the same files on the real glasses when they arrive.

```
 your app.py (brilliant-sdk)          Mac: halo-sim serve                         Phone (browser)
 ───────────────────────────          ─────────────────────────────────           ─────────────────────
 BrilliantMsg / BrilliantBle  ──ws──▶ virtual Halo                        ◀─wss─▶ camera  → frame.camera
   (only the radio is swapped)        · official halo-emulator Lua 5.4 VM          mic     → frame.microphone
                                      · firmware REPL, break/restart/sleep         motion  → frame.imu
                                      · camera, live mic, speaker, sfx             speaker ← frame.speaker / sound
                                                                                   round display (see-through)
                                                                                   BUTTON + TAP pads
```

## How it relates to the real hardware

A Halo app is two programs: a **host app** (Python here) and a **Lua app** that runs on the glasses. They talk over Bluetooth with single-byte message codes.

- **Host app.** `halo-sim run app.py` replaces only the Bluetooth transport inside the SDK. Everything above it is the SDK's own code: sending Lua, chunked messages with acknowledgements, file uploads, and break and reset. On real glasses you run `python app.py` with no changes.
- **Lua app.** It runs in the SDK's official firmware-faithful emulator (`halo-emulator`), extended with a camera, a live microphone, a speaker, sound effects, and the firmware's break, restart and sleep behavior.
- **Hardware.** The phone supplies what the emulator can't: real camera frames, real audio in and out, real motion, and a touch button and tap pad.

## Quick start

```bash
# once: install uv (Python manager used by the SDK), then the project
curl -LsSf https://astral.sh/uv/install.sh | sh
./halo-sim setup

# start the virtual glasses
./halo-sim serve
```

Open `http://localhost:8080/` in a browser on the Mac to use its webcam and mic. Or open the phone address printed by `serve` on your phone. The glasses start on the **app menu**: tap once to move to the next app, tap twice to open it. **⌂ home** on the phone (or `H` on the Mac) leaves the app and returns to the menu. The menu also comes back when an app exits and every time `serve` restarts.

The built-in **Halo Demo** maps the hardware like this:

| On the phone | What happens |
|---|---|
| **BUTTON** click | photo, saved on the Mac and shown back on the glasses |
| **BUTTON** hold for 1 to 2 s | sound effect. The pad turns blue at 1 s; release then. At 2 s it turns red: that is the power-off zone on real Halo, and releasing sends nothing |
| **TAP** twice | live mic level meter |
| **TAP** three times | the Mac streams a chime to the glasses' speaker |
| **TAP** once | back to the HUD |
| tilting the phone | the horizon line follows the motion sensors |

The Python environment lives in `~/.venvs/halo-sim` and the glasses' filesystem in `~/.halo-sim/sandbox`, outside the repo, so cloud-synced folders stay light.

## Phone setup (once per phone)

Browsers allow the camera and mic only on secure pages. `serve` creates a small local certificate authority in `~/.halo-sim/certs` and an HTTPS certificate for this Mac's addresses.

1. Put the phone on the same Wi-Fi as the Mac.
2. Open `http://<mac-ip>:8080/ca.crt` on the phone. `serve` prints the exact address.
3. **iPhone:** go to Settings → General → VPN & Device Management and install the profile. Then go to Settings → General → About → Certificate Trust Settings and enable **Halo SIM Local CA**.
   **Android:** go to Settings → Security → Encryption & credentials → Install a certificate → CA certificate.
4. Open `https://<mac-ip>:8443/` and tap **Start**. Allow camera and microphone (one combined prompt), then motion access.

### Full screen on iPhone

Safari can't make a web page full screen on iPhone. Install the sim as a home-screen web app instead, which opens without Safari's address bar and toolbars:

1. In Safari, on `https://<mac-ip>:8443/`, tap the **≡** icon on the left of the address bar, then **Share**.
2. Scroll down the actions and tap **Add to Home Screen** (under **View More** if it isn't listed). Keep **Open as Web App** on and tap **Add**.
3. Open **Halo SIM** from the home screen. Swipe up to leave it.

The iOS status bar stays visible in portrait; the page draws under it. The icon is tied to the Mac's IP address, so give the Mac a DHCP reservation on the router to keep it working.

### Stop the permission prompts

- **Camera and microphone:** in Safari, on the sim page, open **≡ → Website Settings** and set **Camera** and **Microphone** to **Allow**. Or allow them for every site in **Settings → Apps → Safari → Camera / Microphone**.
- **Motion:** iOS has no "always allow" for motion sensors. Expect one prompt when you tap **Start**.

Hold the phone upright with the back camera facing forward, like a lens. The round display is drawn additively over the camera image: black is transparent, as on Halo's waveguide. Tap **see-through** to switch to a black background. **console** shows Lua `print()` output and has a Lua prompt for when no host app is connected.

Desktop keyboard shortcuts:

| Key | Action |
|---|---|
| Space | button click |
| D | button double click |
| L | button long press |
| T | single tap |
| 2 | double tap |
| 3 | triple tap |

## Running the official SDK examples

```bash
SIM="$PWD/halo-sim"
git clone https://github.com/brilliantlabsAR/brilliant_sdk.git ../brilliant_sdk
cd ../brilliant_sdk/python/packages/brilliant_msg/examples
"$SIM" run camera.py
"$SIM" run audio_clip.py
"$SIM" run multi_tap.py
```

Run them from their own folder, because they load `lua/...` with relative paths. Examples marked "Frame-only" fail here as they would on Halo, because they call Frame camera functions.

## Apps

The simulator is the glasses; apps are installed into it, the way a host app deploys its Lua to real Halo. An app is a folder with:

- a **host program** (Python with `brilliant-sdk`), for example `my_app.py`: connect, `upload_stdlua_libs([...])`, `upload_frame_app(...)`, `start_frame_app(...)`, then exchange messages
- its **Lua side**, for example `lua/my_app.lua`: register callbacks, run a `pcall` main loop, and print once when ready
- a **`halo-app.toml`** manifest:

```toml
id = "my_app"                      # optional, defaults to the folder name
name = "My App"                    # shown in the menu
host = "my_app.py"                 # host program, relative to the folder
args = ["--lang", "it"]            # optional
dependencies = ["opencv-python-headless>=4.8"]   # optional pip requirements
[env]                              # optional environment for the host
MY_SERVICE_URL = "https://..."
```

```bash
./halo-sim install ~/projects/my-app          # copy the folder into ~/.halo-sim/apps/my_app
./halo-sim install ~/projects/my-app --link   # symlink it instead: edits apply on the next launch
./halo-sim install https://github.com/org/my-app.git
./halo-sim apps                               # what the menu shows
./halo-sim uninstall my_app
```

Each installed app gets its own Python environment in `~/.halo-sim/venvs/<id>`, with halo-sim and the app's `dependencies`, so apps can't break each other or the simulator. Built-in apps in this repo's `apps/` (just the demo) use the simulator's own environment. Press **⌂ home** or restart `serve` to see a newly installed app in the menu.

When you open an app, the simulator starts its host program the same way `halo-sim run` does, with the app folder as the working directory. The host connects, stops the menu (a break, as on real glasses) and deploys its Lua. On real Halo the same folder runs with `python my_app.py`; nothing in it is simulator-specific. Real glasses run one app at a time, and the menu is a simulator convenience: there, the phone side decides which app connects.

For quick iterations without installing, run a host program directly while `serve` is running:

```bash
./halo-sim run apps/halo_demo/halo_demo.py
```

The menu returns when that program disconnects.

To start your own app, copy `apps/halo_demo/` (it already has a manifest) and change both halves.

Rules from the firmware that the simulator enforces:

- **The camera starts asleep.** Call `frame.camera.power_save(false)` first. Only `resolution = 640` is accepted.
- **Pass breaks on.** A break raises the string `"...interrupted"` once. Every `pcall` that can be running must re-raise it, or the app never stops.
- **Respect the packet size.** `frame.bluetooth.send()` fails above `frame.bluetooth.max_length()`, which is 243 bytes at the default MTU.
- **Halo has no double buffer.** Draw calls show immediately. Redraw only what changed, or the display flickers on real glasses.
- **Wake the display.** Call `frame.display.power_save(false)` at app start. The phone dims a display left in power save, where real Halo shows nothing.

Other commands:

```bash
./halo-sim serve --no-launcher  # bare glasses, no app menu
./halo-sim repl                 # type Lua straight into the glasses
./halo-sim test                 # simulator test suite
curl localhost:8080/api/display.png -o display.png   # snapshot of the glasses' display
```

## How faithful is it?

| Area | Simulated as |
|---|---|
| Lua VM, `frame.display`, fonts, palette, files, compression | The official `halo-emulator`: Lua 5.4, the firmware's Dogica fonts, pixel-accurate |
| REPL and Bluetooth protocol | The firmware control bytes: break `0x03`, restart `0x04`, remove `main.lua` `0x05`. `main.lua` runs at boot. Uploads use the SDK's real code path |
| Camera | Phone frame cropped to 4:3 at 640×480 JPEG, with the firmware's quality levels and errors. `read_raw` returns a Bayer mosaic. `mpix` crop, resize, grey, kernels, denoise, JPEG and QOI are applied. Colour-correction controls are accepted and ignored |
| Microphone | Live phone audio converted to the requested PCM 8/16 kHz, 8/16-bit, mono/stereo, or LC3. Sound-activated wake (`aad_callback`) uses an estimated sound level |
| Speaker | PCM and LC3 from `frame.speaker.play()` or the host's `send_audio()`, played on the phone |
| `frame.sound` | The seven presets are synthesized approximately, not with the firmware's exact generator |
| Motion | Pitch and roll from phone orientation. Raw accelerometer from the phone. Compass values are made up from the phone heading. `heading` is always 0, as on the firmware |
| Sleep | `standby`, `light_sleep` and deep sleep behave as on the firmware. A button press wakes from deep sleep |

These are not simulated:

- **Memory.** The real Lua VM has about 30 KB free. Check `collectgarbage('count')` in your app.
- **Bluetooth timing and throughput.** The simulator is much faster than real Bluetooth.
- **Other firmware services.** iOS notifications (`frame.ancs`), LE Audio, and firmware updates are absent.
- **Real image quality and optics.** Brightness and pan are shown approximately.
- **Untested example.** The SDK's `openai_realtime.py` voice-assistant example uses LC3 both ways and should run, but has not been tested here.

## Requirements and status

- macOS or Linux with Python 3.10+ ([uv](https://docs.astral.sh/uv/) installs it for you). Tested on macOS with an iPhone (iOS 26 Safari, browser and home-screen web app). Android Chrome should work but is less tested; reports welcome.
- Phone and computer on the same Wi-Fi network.
- Tracks `brilliant-sdk` 1.0 and `halo-emulator` 2.2. The firmware rules above come from `halo-firmware` 0.8.19.

## Contributing

Issues and pull requests are welcome, especially fidelity fixes checked against real Halo hardware. Run `./halo-sim test` before opening a PR.

## License

MIT, see [LICENSE](LICENSE). The simulator depends on, but does not include, Brilliant Labs' BSD-licensed `brilliant-sdk` and `halo-emulator` packages. "Halo" and "Brilliant Labs" are trademarks of their owner.

## Project layout

```
halo-sim             command wrapper (setup / serve / apps / install / run / repl / test)
halo_sim/
  device.py          virtual Halo: Lua VM thread, REPL, break/restart/sleep
  camera.py          frame.camera + mpix, backed by the phone camera
  stubs.py           live mic, speaker, sfx, firmware-faithful system calls
  server.py          HTTP/HTTPS + WebSockets for the phone and host apps
  client.py          SimBrilliantBle: the SDK's BrilliantBle with a WebSocket radio
  patch.py, cli.py   `halo-sim run` patching and the command line
  apps.py            app manifests, discovery, install/uninstall
  launcher.py        app menu on the glasses + app process supervisor
  certs.py           local CA + HTTPS certificate for phones
  web/               phone app (display, button, taps, camera, mic, speaker, IMU)
apps/halo_demo/      built-in starter app (host + Lua + halo-app.toml) that uses every peripheral
tests/               firmware-behaviour and host round-trip tests
```
