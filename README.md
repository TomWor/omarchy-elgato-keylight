# Elgato Key Light for Omarchy

An Elgato Key Light control panel for the Omarchy 4 bar.

Power, brightness, and color temperature for one light or a whole set — as a
group or individually — from a bar widget that looks and behaves like the
first-party ones.

![The bar widget among the other bar icons](docs/bar.png)

| All together | Individually |
| --- | --- |
| ![Group view: one temperature and brightness slider driving every light](docs/panel-group.png) | ![Individual view: compact sliders per light](docs/panel-individual.png) |

## What it does

- **Quick on/off.** Right-click the bar icon, or hit the master switch in the
  panel. Scroll on the icon to change brightness without opening anything. The
  icon dims when every light is off, so the bar reads at a glance without
  claiming your attention.
- **Group or individual.** One pair of sliders for every light at once, or a
  compact pair per light. The bar glyph fills in as the lights get brighter.
- **Persistent names.** Rename a light and the name is written to the light's
  own flash — so it survives reboots, follows the light to another machine, and
  matches what Elgato's Control Center shows.
- **Blink to identify.** With several lights on a desk, the only reliable way
  to tell which row is which fixture.
- **Keyboard driven.** The panel is fully navigable without a mouse.
- **Themed.** Colors, font, corner rounding, and border weights all come from
  the active Omarchy theme, so `omarchy theme set` restyles it live.

## Install

```bash
omarchy plugin add https://github.com/nille/omarchy-elgato-keylight
```

Then add it to the bar via **Omarchy menu → Bar → Widgets**, or by hand in
`~/.config/omarchy/shell.json`:

```json
{ "id": "nille.elgato-keylight" }
```

No build step, no compiled binary, and nothing to install as root. The helper is
a single Python 3 standard-library script, which is what lets `omarchy plugin
add` — which deliberately never runs plugin code or install hooks — set this up
end to end.

### Requirements

- Omarchy 4 (the Quickshell bar)
- Python 3.9+ (already present on Omarchy)
- `avahi-daemon` running, for automatic discovery. Optional — you can pin
  addresses instead.

## Settings

| Key | Default | What it does |
| --- | --- | --- |
| `hosts` | `""` | Comma-separated addresses, e.g. `192.168.1.10, 192.168.1.11`. Skips discovery entirely. Set this if mDNS is blocked on your network or you want a fixed order. |
| `refreshIntervalSec` | `30` | How often to re-read state while the panel is open. |
| `groupMode` | `true` | Start with all lights ganged together. |
| `hideWhenAbsent` | `false` | Remove the bar icon when no lights are found, instead of showing a dimmed one. |

```json
{
  "id": "nille.elgato-keylight",
  "hosts": "192.168.1.10, 192.168.1.11",
  "groupMode": false
}
```

## Using it

### Mouse

| Action | Result |
| --- | --- |
| Left-click icon | Open the panel |
| Right-click icon | Toggle every light |
| Middle-click icon | Rescan the network |
| Scroll icon | Group brightness |
| Left-click a row | Toggle that light |
| Right-click a row | Rename it |
| Blink button on a row | Flash that light so you can find it |

### Keyboard

| Key | Result |
| --- | --- |
| `j` / `k` | Move the cursor |
| `h` / `l` | Adjust the slider under the cursor |
| `space` | Toggle |
| `a` | All lights on/off |
| `g` | Group ⇄ individual |
| `n` | Rename the light under the cursor |
| `i` | Blink the light under the cursor |
| `r` | Rescan |
| `Esc` | Close |

### Hyprland bindings

Everything is reachable over IPC, so you can drive the lights without opening
the panel. Add to `~/.config/hypr/bindings.conf`:

```
bind = SUPER SHIFT, L, exec, quickshell -p $OMARCHY_PATH/shell ipc call nille.elgato-keylight lightsToggle
bind = SUPER ALT,   L, exec, quickshell -p $OMARCHY_PATH/shell ipc call nille.elgato-keylight toggle
```

Available calls: `open`, `close`, `toggle`, `lightsOn`, `lightsOff`,
`lightsToggle`, `brightness <0-100>`, `kelvin <2900-7000>`, `identify <host>`,
`rescan`, `state`. Quickshell requires every argument, so pass `identify ""` to
blink the first light.

## The helper CLI

`scripts/keylight` is usable on its own, and prints JSON on stdout for every
subcommand:

```bash
scripts/keylight discover                                  # find lights
scripts/keylight state                                     # read them
scripts/keylight set --brightness 60 --kelvin 4500         # all of them
scripts/keylight set --host 192.168.1.10 --off             # just one
scripts/keylight rename --host 192.168.1.10 "Left Key"
scripts/keylight identify --host 192.168.1.10
scripts/keylight firmware --probe                          # installed versions
```

Every subcommand exits 0 and reports failure as `{"ok": false, "error": ...}`,
including when a light is unplugged. The panel is a UI, not an error handler: it
should never have to tell "the helper crashed" apart from "the light is asleep",
so both arrive as data.

## Firmware

Each light's installed version shows in its row tooltip, and
`scripts/keylight firmware` prints it. Compare it against [Elgato's release
notes](https://help.elgato.com/hc/en-us/sections/360004115951-Elgato-Control-Center-Software)
to see whether you are current.

The panel never says an update is *available*, and there is no "check for
updates" button. Elgato publishes no machine-readable firmware feed and no
downloadable images, so such a control would behave identically whether you were
current or three versions behind — a notification carrying no information is
worse than no notification.

`scripts/keylight firmware-flash` implements the light's own network update
protocol (`prepare` → chunked `data` → `execute`), so if you have obtained an
image you can write it without Windows or macOS. It is **not** exposed in the
panel UI, and requires `--yes`:

```bash
scripts/keylight firmware-flash --host 192.168.1.10 keylight.bin --yes
```

There is no rollback and no signature you can verify beforehand. An interrupted
flash, or the wrong image, leaves the light needing a successful retry to
recover. Elgato's supported path is Control Center on Windows, macOS, iOS, or
Android; use that unless you have a reason not to.

## How it works

```
Panel.qml    presentation, cursor model, theming — every control is a qs.Ui
             component, so nothing about the look is hardcoded here
Model.js     pure functions: parsing, clamping, labels, argv building. No QML
             types, no state, no side effects — which is what makes it testable
scripts/     the helper CLI: mDNS discovery, HTTP, concurrency
tests/       89 unit tests over the helper
```

Lights speak HTTP on port 9123 and advertise themselves as `_elg._tcp`.
Temperature travels the wire in mired (`mired = 1e6 / kelvin`); the panel works
in Kelvin and snaps to 50K stops, because the round trip is lossy and unsnapped
values make the readout jitter. Discovery is cached, since it costs about a
second of network waiting and is the difference between a panel that opens
instantly and one that hangs.

Values are applied optimistically while you drag and reconciled from the
hardware shortly after, so the slider tracks your finger but still ends up
showing what the firmware actually accepted — including changes made from
another app.

## Testing

```bash
python3 -m unittest discover -s tests -v
```

Nothing in the suite touches a real light.

## A note on the network API

The Key Light's HTTP API is unauthenticated, which is a property of the
hardware rather than of this plugin — see
[CVE-2025-7202](https://www.toreon.com/flashing-your-lights-cve-2025-7202/).
Anything on your network can already control your lights. Worth knowing if you
run one on a network you do not trust.

## License

MIT. See [LICENSE](LICENSE).

Not affiliated with or endorsed by Elgato / Corsair.
