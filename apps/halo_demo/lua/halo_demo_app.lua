-- halo_demo_app.lua: device side of the Halo demo (runs on real Halo and in halo-sim).
--   button click  -> take a photo, host sends it back as a sprite
--   button hold   -> sound effect
--   double tap    -> toggle microphone streaming (host shows a level meter)
--   triple tap    -> ask the host to play a chime on the speaker
--   single tap    -> back to the HUD
-- Halo has no double buffer: redraw only what changed to avoid flicker.

local data = require('data.min')
local camera = require('camera.min')
local code = require('code.min')
local plain_text = require('plain_text.min')
local image_sprite_block = require('image_sprite_block.min')
local audio = require('audio.min')

-- host -> device
local TEXT_MSG = 0x0a
local SPRITE_MSG = 0x20
local LEVEL_MSG = 0x31
local SPEAKER_MSG = 0x32
-- device -> host
local EVENT_MSG = 0x40   -- 1 = mic on, 0 = mic off, 3 = chime request

local W = 256
local mode = 'hud'
local listening = false
local pending = {}
local horizon = nil
local isb, drawn = nil, 0

-- callbacks only set flags; the main loop does the work
frame.button.single(function() pending.photo = true end)
frame.button.long(function() pending.sfx = true end)
frame.imu.tap_callback(function(kind) pending[kind] = true end)

local function is_interrupt(err)
  return type(err) == 'string' and string.find(err, 'interrupted$') ~= nil
end

local function send_event(v)
  pcall(frame.bluetooth.send, string.char(EVENT_MSG, v))
end

local function text_center(s, y, color)
  frame.display.text(s, math.max(1, math.floor((W - #s * 8) / 2) + 1), y, color)
end

local function draw_status(s)
  frame.display.rect(40, 206, 176, 12, 0x000000, true)
  text_center(s, 208, 0xC0C0C0)
end

local function draw_hud()
  frame.display.clear()
  frame.display.circle(128, 128, 126, 0x1E3A4C, false)
  text_center('HALO DEMO', 36, 0x39D0FF)
  text_center('click: photo', 58, 0x808080)
  text_center('hold: sound', 70, 0x808080)
  text_center('2 taps: mic  3: chime', 82, 0x808080)
  horizon = nil
  draw_status(listening and 'listening' or 'ready')
end

local function update_horizon()
  local d = frame.imu.direction()
  local cy = 140 - math.floor(math.max(-30, math.min(30, d.pitch)) * 1.2)
  local dy = math.floor(math.tan(math.rad(math.max(-60, math.min(60, d.roll)))) * 50)
  local new = { 78, cy + dy, 178, cy - dy }
  if horizon and horizon[2] == new[2] and horizon[4] == new[4] then return end
  if horizon then frame.display.line(horizon[1], horizon[2], horizon[3], horizon[4], 0x000000) end
  frame.display.line(new[1], new[2], new[3], new[4], 0x3DDC84)
  frame.display.line(122, 140, 134, 140, 0xFFFFFF)
  horizon = new
end

local function draw_level(v)
  frame.display.rect(68, 186, 120, 10, 0x404040, false)
  frame.display.rect(69, 187, 118, 8, 0x000000, true)
  local w = math.floor(v * 118 / 100)
  if w > 0 then frame.display.rect(69, 187, w, 8, 0xFF3B4E, true) end
end

local function draw_sprite_lines()
  for index = drawn + 1, isb.active_sprites do
    local spr = isb.sprites[index]
    if index == 1 then image_sprite_block.set_palette(spr.num_colors, spr.palette_data) end
    local y = 33 + isb.sprite_line_height * (index - 1)
    frame.display.bitmap(1, y, spr.width, 2 ^ spr.bpp, 0, spr.pixel_data)
    drawn = index
  end
  if isb.active_sprites == isb.total_sprites then isb, drawn = nil, 0 end
end

local function handle_messages()
  for _, item in ipairs(data.process_raw_items()) do
    local flag, raw = item[1], item[2]
    if flag == SPRITE_MSG then
      if isb == nil then frame.display.clear() end
      isb = image_sprite_block.parse_image_sprite_block(raw, isb)
      if isb ~= nil and isb.current_sprite_index > 0 then draw_sprite_lines() end
    elseif flag == LEVEL_MSG then
      if listening and mode == 'hud' then draw_level(code.parse_code(raw).value) end
    elseif flag == SPEAKER_MSG then
      if code.parse_code(raw).value == 1 then
        frame.speaker.start { encoder = 'pcm', sample_rate = 16000, channels = 1, volume = 80 }
      else
        frame.speaker.stop()
      end
    elseif flag == TEXT_MSG then
      if mode == 'hud' then draw_status(plain_text.parse_plain_text(raw).string) end
    end
    collectgarbage('collect')
  end
end

local function handle_inputs()
  if pending.photo then
    pending.photo = nil
    mode = 'photo'
    draw_status('capturing')
    local ok, err = pcall(camera.capture_and_send, { resolution = 640, quality = 'MEDIUM', pan = 0, raw = false })
    if not ok then
      if is_interrupt(err) then error(err, 0) end
      print('capture failed: ' .. tostring(err))
    end
  end
  if pending.sfx then
    pending.sfx = nil
    frame.sound.play_async('powerup', { volume = 60, duration_ms = 600 })
  end
  if pending.single then
    pending.single = nil
    if mode ~= 'hud' then mode = 'hud'; draw_hud() end
  end
  if pending.double then
    pending.double = nil
    listening = not listening
    if listening then
      audio.start { sample_rate = 8000, bit_depth = 16 }
      send_event(1)
    else
      audio.stop()
      while audio.read_and_send_audio() ~= nil do end  -- flush + end-of-stream marker
      send_event(0)
      if mode == 'hud' then draw_hud() end
    end
    if mode == 'hud' then draw_status(listening and 'listening' or 'ready') end
  end
  if pending.triple then
    pending.triple = nil
    send_event(3)
  end
end

local function app_loop()
  frame.display.power_save(false)
  frame.camera.power_save(false)
  draw_hud()
  print('Frame app is running')   -- start_frame_app() waits for this

  while true do
    local ok, err = pcall(function()
      handle_messages()
      handle_inputs()
      if listening then
        for _ = 1, 10 do
          local sent = audio.read_and_send_audio()
          if sent == nil or sent == 0 then break end
        end
      end
      if mode == 'hud' then update_horizon() end
      frame.sleep(listening and 0.02 or 0.05)
    end)
    if not ok then
      print(err)
      if is_interrupt(err) then
        audio.stop()
        frame.display.clear()
        break
      end
    end
  end
end

app_loop()
