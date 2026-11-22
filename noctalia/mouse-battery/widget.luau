-- Mouse Tool: mouse battery in the noctalia bar.
-- Runs `mousetool.py battery` every minute; click opens Mouse Tool.
-- mousetool prints "<percent|-> <charging 0/1> <stale 0/1> <age seconds|->". Stale means the mouse
-- was asleep, so the last good reading is shown dimmed.
--
-- The glyph sits above a small number with no "%", so it fits a side bar's width (and is still short
-- enough for a horizontal bar). barWidget.isVertical() reported false on a right-hand bar in 5.4.1, so
-- the layout doesn't depend on it.

-- Set MOUSETOOL_BIN in your environment, install `mousetool` on PATH, or point TOOL at `/path/to/mousetool/mousetool.py`.
local TOOL = '"${MOUSETOOL_BIN:-mousetool}"'
local busy = false

local function ago(sec)
  if sec == nil then return "unknown" end
  if sec < 90 then return "just now" end
  if sec < 5400 then return math.floor(sec / 60 + 0.5) .. " min ago" end
  return math.floor(sec / 3600 + 0.5) .. " h ago"
end

local function draw(glyph, text, color)
  barWidget.render(ui.column({ gap = 1, align = "center" }, {
    ui.glyph({ name = glyph, size = 13, color = color }),
    ui.label({ text = text, fontSize = 9, color = color }),
  }))
end

local function show(pct, charging, stale, age)
  if pct == nil then
    draw("mouse", "–", "on_surface_variant")
    barWidget.setTooltip({ { key = "Mouse", value = "not found" } })
    return
  end
  local color = nil                                   -- nil keeps the bar's normal widget colours
  if stale then color = "on_surface_variant" elseif pct <= 15 then color = "error" end
  draw(charging and "battery-charging" or "mouse", tostring(pct), color)
  local status = charging and "Charging" or "On battery"
  if stale then status = "Asleep · read " .. ago(age) end
  barWidget.setTooltip({
    { key = "Mouse battery", value = pct .. "%" },
    { key = "Status", value = status },
  })
end

function update()
  if busy then return end
  busy = true
  noctalia.runAsync(TOOL .. " battery", function(r)
    busy = false
    local p, c, s, a = string.match(r.stdout or "", "^(%S+) (%d) (%d) (%S+)")
    if r.exitCode ~= 0 or p == nil or p == "-" then
      show(nil)
      return
    end
    show(tonumber(p), c == "1", s == "1", tonumber(a))
  end)
end

function onClick()
  noctalia.runAsync(TOOL .. " open")
end

noctalia.setUpdateInterval(60000)
update()
