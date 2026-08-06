// Pure helpers for the Key Light panel. No QML types, no state, no side effects —
// mirrors the Model.js convention every first-party Omarchy panel follows, so
// the logic here stays unit-testable and the Panel stays presentation.
.pragma library

// Hardware limits, duplicated from scripts/keylight so the slider bounds match
// what the helper will accept. The helper remains the authority: it clamps
// again before anything reaches the wire.
var BRIGHTNESS_MIN = 0
var BRIGHTNESS_MAX = 100
var KELVIN_MIN = 2900
var KELVIN_MAX = 7000
var KELVIN_STEP = 50

function clamp(value, low, high) {
  var n = Number(value)
  if (!isFinite(n)) return low
  return Math.max(low, Math.min(high, n))
}

function clampBrightness(value) {
  return Math.round(clamp(value, BRIGHTNESS_MIN, BRIGHTNESS_MAX))
}

// Kelvin snaps to 50K stops: the wire format (mired) is coarser than kelvin at
// the warm end, so unsnapped values would display jitter after a round trip.
function clampKelvin(value) {
  var k = clamp(value, KELVIN_MIN, KELVIN_MAX)
  return Math.round(k / KELVIN_STEP) * KELVIN_STEP
}

// ---------------------------------------------------------------- parsing

// Parse a helper reply. Anything unexpected yields a shape the panel can
// render without null checks, because a blank widget is worse than an
// honest "no lights" state.
function parseState(raw) {
  var text = String(raw || "").trim()
    if (!text) return { ok: false, lights: [], error: "no output" }
  try {
    var data = JSON.parse(text)
    if (!data || typeof data !== "object") throw new Error("not an object")
    var lights = Array.isArray(data.lights) ? data.lights.map(normalizeLight) : []
    return {
      ok: data.ok !== false,
      lights: lights,
      error: data.error ? String(data.error) : "",
      discovered: !!data.discovered
    }
  } catch (e) {
    return { ok: false, lights: [], error: "invalid helper output" }
  }
}

function normalizeLight(light) {
  var entry = (light && typeof light === "object") ? light : {}
  var host = String(entry.host || "")
  return {
    host: host,
    port: Number(entry.port) || 9123,
    name: String(entry.name || host || "Key Light"),
    reachable: entry.reachable !== false,
    on: !!entry.on,
    brightness: clampBrightness(entry.brightness),
    kelvin: clampKelvin(entry.kelvin),
    error: entry.error ? String(entry.error) : ""
  }
}

// ---------------------------------------------------------------- aggregates

function reachableLights(lights) {
  return (lights || []).filter(function(l) { return l.reachable })
}

function anyOn(lights) {
  return reachableLights(lights).some(function(l) { return l.on })
}

// Group sliders show the mean of the lights they command. Averaging (rather
// than showing the first light's value) keeps the handle where the user left
// it when the lights differ, so a group drag doesn't jump on open.
function averageBrightness(lights) {
  var list = reachableLights(lights)
  if (!list.length) return 0
  var total = list.reduce(function(sum, l) { return sum + l.brightness }, 0)
  return Math.round(total / list.length)
}

function averageKelvin(lights) {
  var list = reachableLights(lights)
  if (!list.length) return clampKelvin(4000)
  var total = list.reduce(function(sum, l) { return sum + l.kelvin }, 0)
  return clampKelvin(total / list.length)
}

// True when every reachable light already agrees, which is what lets the
// group view claim it is showing a real value rather than an average.
function valuesMatch(lights, field) {
  var list = reachableLights(lights)
  if (list.length < 2) return true
  var first = list[0][field]
  return list.every(function(l) { return l[field] === first })
}

// ---------------------------------------------------------------- labels

// Playful mood-name ladder for brightness, matching the bands the audio and
// brightness panels use so it reads like the rest of the shell.
function brightnessName(percent, on) {
  if (!on) return "Off"
  var p = clampBrightness(percent)
  if (p <= 0) return "Dark"
  if (p < 10) return "Candlelit"
  if (p < 25) return "Dim"
  if (p < 45) return "Soft"
  if (p < 65) return "Lit"
  if (p < 85) return "Bright"
  if (p < 100) return "Brilliant"
  return "Blinding"
}

// Photographic names for color temperature. Warm at the low end because
// kelvin runs backwards from how the light actually looks.
function kelvinName(kelvin) {
  var k = clampKelvin(kelvin)
  if (k < 3200) return "Candle"
  if (k < 3800) return "Tungsten"
  if (k < 4600) return "Warm white"
  if (k < 5200) return "Neutral"
  if (k < 5800) return "Daylight"
  if (k < 6400) return "Overcast"
  return "Shade"
}

function kelvinLabel(kelvin) {
  return clampKelvin(kelvin) + "K"
}

// Bar glyph. A lamp that fills in as it gets brighter, so the bar reads at a
// glance; the crossed-out lamp means every light is off.
function barIcon(lights) {
  var list = reachableLights(lights)
  if (!list.length) return "󰌵"
  if (!anyOn(list)) return "󰛑"
  var level = averageBrightness(list.filter(function(l) { return l.on }))
  if (level >= 66) return "󱩏"
  if (level >= 33) return "󱩑"
  return "󱩓"
}

// One-line summary for the panel hero and the bar tooltip.
function summary(lights, error) {
  var list = lights || []
  if (error === "no-devices" || !list.length) return "No lights found"
  var reachable = reachableLights(list)
  if (!reachable.length) return "Unreachable"
  var on = reachable.filter(function(l) { return l.on }).length
  if (list.length === 1) {
    return on ? brightnessName(reachable[0].brightness, true) : "Off"
  }
  if (on === 0) return "All off"
  // "All on" has to mean every light, not every *reachable* light: a group
  // with one light unplugged is not all on, and saying so hides the problem.
  if (on === list.length) return "All on"
  return on + " of " + list.length + " on"
}

// ---------------------------------------------------------------- color

// Approximate the visible color of a given temperature, for the swatch on the
// temperature slider. Piecewise-linear between anchor points sampled from a
// blackbody curve — close enough to read as "warm" vs "cool" at swatch size,
// and far cheaper than evaluating Planck's law per frame.
function kelvinToRgb(kelvin) {
  var stops = [
    { k: 2900, r: 1.00, g: 0.71, b: 0.42 },
    { k: 3500, r: 1.00, g: 0.79, b: 0.57 },
    { k: 4000, r: 1.00, g: 0.84, b: 0.67 },
    { k: 4500, r: 1.00, g: 0.88, b: 0.76 },
    { k: 5000, r: 1.00, g: 0.91, b: 0.84 },
    { k: 5500, r: 1.00, g: 0.94, b: 0.91 },
    { k: 6000, r: 1.00, g: 0.97, b: 0.97 },
    { k: 6500, r: 0.97, g: 0.97, b: 1.00 },
    { k: 7000, r: 0.92, g: 0.95, b: 1.00 }
  ]
  var k = clamp(kelvin, KELVIN_MIN, KELVIN_MAX)
  for (var i = 0; i < stops.length - 1; i++) {
    var lo = stops[i], hi = stops[i + 1]
    if (k <= hi.k) {
      var t = (k - lo.k) / (hi.k - lo.k)
      return {
        r: lo.r + (hi.r - lo.r) * t,
        g: lo.g + (hi.g - lo.g) * t,
        b: lo.b + (hi.b - lo.b) * t
      }
    }
  }
  var last = stops[stops.length - 1]
  return { r: last.r, g: last.g, b: last.b }
}

// ---------------------------------------------------------------- helper argv

// Build the helper's argument list. Centralized so the Panel never assembles
// argv inline and every call site inherits the same --host handling.
function setArgs(helper, hosts, changes) {
  var argv = [helper, "set"]
  var list = hosts || []
  for (var i = 0; i < list.length; i++) argv.push("--host", String(list[i]))
  if (changes.power === "on") argv.push("--on")
  else if (changes.power === "off") argv.push("--off")
  else if (changes.power === "toggle") argv.push("--toggle")
  if (changes.brightness !== undefined && changes.brightness !== null)
    argv.push("--brightness", String(clampBrightness(changes.brightness)))
  if (changes.kelvin !== undefined && changes.kelvin !== null)
    argv.push("--kelvin", String(clampKelvin(changes.kelvin)))
  return argv
}

function stateArgs(helper, hosts, refresh) {
  var argv = [helper, "state"]
  var list = hosts || []
  for (var i = 0; i < list.length; i++) argv.push("--host", String(list[i]))
  if (refresh) argv.push("--refresh")
  return argv
}

function renameArgs(helper, host, name) {
  return [helper, "rename", "--host", String(host), String(name)]
}

function identifyArgs(helper, host) {
  return [helper, "identify", "--host", String(host)]
}

function firmwareArgs(helper, hosts) {
  var argv = [helper, "firmware"]
  var list = hosts || []
  for (var i = 0; i < list.length; i++) argv.push("--host", String(list[i]))
  return argv
}

// Merge a `firmware` reply into a map of host -> firmware info, so the panel can
// look up a row's version without re-reading it on every refresh. Firmware only
// changes across a reboot, which makes it worth caching separately from state.
function firmwareMap(raw) {
  var parsed
  try {
    parsed = JSON.parse(String(raw || "").trim())
  } catch (e) {
    return {}
  }
  var list = (parsed && Array.isArray(parsed.lights)) ? parsed.lights : []
  var map = {}
  for (var i = 0; i < list.length; i++) {
    var entry = list[i]
    if (!entry || !entry.host || !entry.reachable) continue
    map[String(entry.host)] = {
      version: String(entry.firmwareVersion || ""),
      build: entry.firmwareBuildNumber,
      product: String(entry.productName || ""),
      serial: String(entry.serialNumber || "")
    }
  }
  return map
}

// One-line firmware label for a row. Empty when unknown, so the caller can hide
// the field rather than render "unknown" noise.
function firmwareLabel(info) {
  if (!info || !info.version) return ""
  var text = "Firmware " + info.version
  if (info.build !== undefined && info.build !== null) text += " (" + info.build + ")"
  return text
}

// Hosts configured in shell.json. Accepts an array or a comma/space separated
// string so hand-edited config is forgiving.
function configuredHosts(setting) {
  if (Array.isArray(setting))
    return setting.map(function(h) { return String(h).trim() }).filter(Boolean)
  return String(setting || "")
    .split(/[,\s]+/)
    .map(function(h) { return h.trim() })
    .filter(Boolean)
}
