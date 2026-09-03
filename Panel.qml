import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Elgato Key Light control. Layout follows Elgato's Control Center — a master
// power switch, then one row per light with its own switch, temperature
// slider, and brightness slider — but every control is a qs.Ui component, so
// color, typography, spacing, corner rounding, and border weights all come
// from the active Omarchy theme rather than from anything hardcoded here.
Panel {
  id: root
  moduleName: "nille.elgato-keylight"
  ipcTarget: "nille.elgato-keylight"
  manageIpc: false

  // manageIpc: false so this panel owns the single IpcHandler its target
  // allows, and can expose light control to keybindings and scripts.

  // KEYLIGHT_DIR points the helper at a working tree instead of the installed
  // plugin, so tests/harness/run drives the code you are editing.
  readonly property string pluginDir: Quickshell.env("KEYLIGHT_DIR")
    || (Quickshell.env("HOME") + "/.config/omarchy/plugins/nille.elgato-keylight")
  readonly property string helper: pluginDir + "/scripts/keylight"

  // Separate from KEYLIGHT_DIR on purpose. That one is a path a live shell could
  // plausibly inherit from the session; this is a flag nothing but the harness
  // sets, and `quit()` calling Qt.quit() on a stray variable would take down
  // omarchy-shell rather than one widget.
  readonly property bool harness: Quickshell.env("KEYLIGHT_HARNESS") === "1"

  // ---------------------------------------------------------------- theme
  //
  // Same shape every first-party panel uses: take the host bar's resolved
  // colors and font when we have a bar, fall back to the qs.Commons singletons
  // otherwise. Color.* and Style.* are already theme-reactive — Color rebuilds
  // its surface roles when a theme swaps, Style re-reads its spacing and
  // typography tokens — so binding through them is what makes this widget
  // follow `omarchy theme set` live, with no per-theme code.
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color accent: Color.accent
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color hoverFill: bar ? Style.hoverFillFor(bar.foreground, Color.accent) : "transparent"
  readonly property color selectedFill: bar ? Style.selectedFillFor(bar.foreground, Color.accent) : "transparent"

  // Bar glyph color. Lights-on is signalled by full-strength foreground and
  // lights-off by a dimmed one, which is the convention the other stateful bar
  // widgets follow. Deliberately not BarIconButton's `active`: that resolves to
  // bar.urgent, so it would paint the lamp in the theme's alert color and make a
  // working light look like a problem.
  readonly property color barIconColor: {
    var base = bar ? bar.barForeground : Color.foreground
    return anyOn ? base : Qt.darker(base, 1.55)
  }

  // ---------------------------------------------------------------- state

  property var lights: []
  property string lastError: ""
  property bool loading: false
  property bool refreshQueued: false
  property bool everLoaded: false

  // Group mode drives every light from one pair of sliders. It starts from the
  // configured default and is then a session toggle, because which mode you
  // want depends on what you are doing, not on how you set the widget up.
  property bool groupMode: setting("groupMode", true) !== false

  readonly property var hosts: Model.configuredHosts(setting("hosts", ""))
  readonly property int refreshIntervalSec: Math.max(5, Math.min(3600, Number(setting("refreshIntervalSec", 30)) || 30))
  readonly property bool hideWhenAbsent: setting("hideWhenAbsent", false) === true

  readonly property var reachableLights: Model.reachableLights(lights)
  readonly property bool anyOn: Model.anyOn(lights)
  readonly property bool hasLights: lights.length > 0
  readonly property bool multiple: lights.length > 1

  // Optimistic local values so a drag feels instant. While a set is in flight
  // the slider shows what the user asked for, not the last value read back —
  // otherwise the handle snaps backwards on every refresh mid-drag.
  property int pendingBrightness: -1
  property int pendingKelvin: -1
  property var pendingPerLight: ({})

  readonly property int shownGroupBrightness: pendingBrightness >= 0
    ? pendingBrightness : Model.averageBrightness(lights)
  readonly property int shownGroupKelvin: pendingKelvin >= 0
    ? pendingKelvin : Model.averageKelvin(lights)

  // True when the lights disagree, so the group view can say "Mixed" instead
  // of presenting an average as if it were a real setting.
  readonly property bool groupMixed: multiple
    && (!Model.valuesMatch(lights, "kelvin") || !Model.valuesMatch(lights, "brightness"))

  // Carry sub-notch touchpad deltas between wheel events, as the bar's other
  // scrollable widgets do.
  property real wheelAccumulator: 0

  // Inline rename state. A non-empty host means an editor is open, which also
  // blocks the key catcher so the field receives typing.
  property string renamingHost: ""
  property string renameDraft: ""

  // host -> { version, build, product, serial }. Read once per open rather than
  // on every refresh: firmware cannot change without the light rebooting, and
  // it costs an extra round trip per light.
  property var firmware: ({})

  // Host whose identify blink is in flight, so the row can acknowledge the
  // click — the light itself is the real feedback, but it is across the room.
  property string identifyingHost: ""

  function firmwareFor(host) { return firmware[host] || null }

  // ---------------------------------------------------------------- cursor
  //
  // Sections, top to bottom:
  //   "header"  - master power switch (no rows; selectedIndex -1)
  //   "group"   - group temperature (0) and brightness (1) sliders
  //   "lights"  - per light: the row itself, then in individual mode its own
  //               temperature and brightness sliders
  property string focusSection: "group"
  property int selectedIndex: 0
  property bool cursorActive: false

  readonly property bool headerHasCursor: cursorActive && focusSection === "header"

  readonly property var visibleSections: {
    var list = ["header"]
    if (hasLights && groupMode) list.push("group")
    if (hasLights) list.push("lights")
    return list
  }

  function sectionCount(section) {
    if (section === "header") return 0
    if (section === "group") return 2
    if (section === "lights") return lights.length * (groupMode ? 1 : 3)
    return 0
  }

  // Per-light rows in individual mode are three cursor stops each: the row
  // (power / rename), then its temperature and brightness sliders.
  function lightIndexFor(cursor) { return groupMode ? cursor : Math.floor(cursor / 3) }
  function lightSlotFor(cursor) { return groupMode ? 0 : (cursor % 3) }

  function setHeaderCursor() {
    cursorActive = true
    focusSection = "header"
    selectedIndex = -1
  }

  function setCursor(section, index) {
    cursorActive = true
    focusSection = section
    selectedIndex = index
  }

  function moveCursor(delta) {
    var sections = visibleSections
    if (!sections.length) return
    var sIdx = sections.indexOf(focusSection)
    if (sIdx < 0) {
      focusSection = sections[0]
      selectedIndex = sectionCount(focusSection) ? 0 : -1
      return
    }
    var max = sectionCount(focusSection) - 1

    if (delta > 0) {
      if (selectedIndex < max) { selectedIndex = selectedIndex + 1; return }
      if (sIdx < sections.length - 1) {
        focusSection = sections[sIdx + 1]
        selectedIndex = sectionCount(focusSection) ? 0 : -1
      }
    } else {
      if (selectedIndex > 0) { selectedIndex = selectedIndex - 1; return }
      if (sIdx > 0) {
        var prev = sections[sIdx - 1]
        focusSection = prev
        var prevMax = sectionCount(prev) - 1
        selectedIndex = prevMax >= 0 ? prevMax : -1
      }
    }
  }

  // h/l adjusts whichever slider the cursor sits on. On a light's own row
  // there is no slider, so horizontal motion is deliberately inert rather
  // than silently moving a value the cursor is not pointing at.
  function adjustHorizontal(direction) {
    if (focusSection === "group") {
      if (selectedIndex === 0) setGroupKelvin(shownGroupKelvin + direction * 100)
      else setGroupBrightness(shownGroupBrightness + direction * 5)
      return
    }
    if (focusSection !== "lights" || groupMode) return
    var light = lights[lightIndexFor(selectedIndex)]
    if (!light) return
    var slot = lightSlotFor(selectedIndex)
    if (slot === 1) setLightKelvin(light, shownKelvinFor(light) + direction * 100)
    else if (slot === 2) setLightBrightness(light, shownBrightnessFor(light) + direction * 5)
  }

  function activateCursor() {
    if (focusSection === "header") { toggleAll(); return }
    if (focusSection === "lights") {
      var light = lights[lightIndexFor(selectedIndex)]
      if (light && lightSlotFor(selectedIndex) === 0) toggleLight(light)
    }
    // Slider stops have no discrete action; their value is the action.
  }

  function clampCursor() {
    var sections = visibleSections
    if (!sections.length) return
    // "header" has no rows, so let it through rather than knocking the cursor
    // off the master switch every time a refresh republishes the light list.
    if (focusSection === "header") return
    if (sections.indexOf(focusSection) < 0) {
      focusSection = sections[0]
      selectedIndex = sectionCount(focusSection) ? 0 : -1
      return
    }
    var count = sectionCount(focusSection)
    if (count === 0) { selectedIndex = -1; return }
    if (selectedIndex > count - 1) selectedIndex = count - 1
    if (selectedIndex < 0) selectedIndex = 0
  }

  // Keep the focused row inside the ScrollView's viewport; without this, j/k
  // can walk the cursor off-screen.
  function ensureCursorVisible(item) {
    if (!item || !scrollArea) return
    var flick = scrollArea.contentItem
    if (!flick || flick.contentY === undefined) return
    var margin = Style.space(6)
    var maxY = Math.max(0, (flick.contentHeight || 0) - flick.height)
    if (maxY <= 0) { flick.contentY = 0; return }
    var pt = item.mapToItem(flick.contentItem || flick, 0, 0)
    var top = pt.y
    var bottom = top + (item.height || 0)
    if (top < flick.contentY + margin)
      flick.contentY = Math.max(0, Math.min(maxY, top - margin))
    else if (bottom > flick.contentY + flick.height - margin)
      flick.contentY = Math.max(0, Math.min(maxY, bottom + margin - flick.height))
  }

  // ---------------------------------------------------------------- per-light

  function shownBrightnessFor(light) {
    var pending = pendingPerLight[light.host]
    return (pending && pending.brightness !== undefined) ? pending.brightness : light.brightness
  }

  function shownKelvinFor(light) {
    var pending = pendingPerLight[light.host]
    return (pending && pending.kelvin !== undefined) ? pending.kelvin : light.kelvin
  }

  // Replace the map rather than mutating it: QML only re-evaluates bindings on
  // a var property when it is reassigned.
  function setPending(host, field, value) {
    var next = {}
    for (var k in pendingPerLight) next[k] = pendingPerLight[k]
    var entry = {}
    var existing = next[host]
    if (existing) for (var f in existing) entry[f] = existing[f]
    entry[field] = value
    next[host] = entry
    pendingPerLight = next
  }

  function lightFor(host) {
    for (var i = 0; i < lights.length; i++)
      if (lights[i].host === host) return lights[i]
    return null
  }

  // ---------------------------------------------------------------- actions

  function hostList() {
    // Fall back to the configured hosts when nothing has loaded yet, so a
    // keybinding fired before the first refresh still reaches the lights.
    if (reachableLights.length)
      return reachableLights.map(function(l) { return l.host })
    return hosts
  }

  function toggleAll() {
    // One --toggle would flip each light independently and desynchronize a
    // mixed group. Driving every light to the same explicit state is what
    // makes the master switch behave like a master switch.
    apply(hostList(), { power: anyOn ? "off" : "on" })
  }

  function toggleLight(light) {
    if (!light) return
    apply([light.host], { power: light.on ? "off" : "on" })
  }

  function setGroupBrightness(value) {
    pendingBrightness = Model.clampBrightness(value)
    groupDebounce.restart()
  }

  function setGroupKelvin(value) {
    pendingKelvin = Model.clampKelvin(value)
    groupDebounce.restart()
  }

  function setLightBrightness(light, value) {
    if (!light) return
    setPending(light.host, "brightness", Model.clampBrightness(value))
    perLightDebounce.restart()
  }

  function setLightKelvin(light, value) {
    if (!light) return
    setPending(light.host, "kelvin", Model.clampKelvin(value))
    perLightDebounce.restart()
  }

  function flushGroup() {
    var changes = {}
    if (pendingBrightness >= 0) changes.brightness = pendingBrightness
    if (pendingKelvin >= 0) changes.kelvin = pendingKelvin
    if (!Object.keys(changes).length) return
    // Setting a value implies turning on: dragging brightness up on an off
    // light and seeing nothing happen reads as a broken slider.
    if (!anyOn) changes.power = "on"
    apply(hostList(), changes)
  }

  function flushPerLight() {
    for (var host in pendingPerLight) {
      var entry = pendingPerLight[host]
      var changes = {}
      if (entry.brightness !== undefined) changes.brightness = entry.brightness
      if (entry.kelvin !== undefined) changes.kelvin = entry.kelvin
      if (!Object.keys(changes).length) continue
      var light = lightFor(host)
      if (light && !light.on) changes.power = "on"
      apply([host], changes)
    }
  }

  // Every mutation goes through one detached process. Fire-and-forget plus a
  // follow-up read is deliberate: a slider drag emits far more sets than round
  // trips we would want to await, and the refresh reconciles anyway.
  function apply(targets, changes) {
    if (!targets || !targets.length) return
    Quickshell.execDetached(Model.setArgs(helper, targets, changes))
    settleTimer.restart()
  }

  function refresh(force) {
    if (stateProc.running) { refreshQueued = true; return }
    loading = true
    stateProc.command = Model.stateArgs(helper, hosts, force === true)
    stateProc.running = true
  }

  function publish(raw) {
    var parsed = Model.parseState(raw)
    lights = parsed.lights
    lastError = parsed.ok ? (parsed.error || "") : (parsed.error || "helper failed")
    everLoaded = true
    // Drop optimistic values now that real state has arrived, so the sliders
    // follow the hardware again — including changes made from another app.
    pendingBrightness = -1
    pendingKelvin = -1
    pendingPerLight = ({})
    clampCursor()
    // Pick up firmware for any light we have not read yet — a light that was
    // asleep at open, or one that only just appeared on the network.
    if (opened && reachableLights.some(function(l) { return !firmware[l.host] }))
      readFirmware()
  }

  function beginRename(light) {
    if (!light || !light.reachable) return
    renamingHost = light.host
    renameDraft = light.name
  }

  function commitRename() {
    if (!renamingHost) return
    var host = renamingHost
    var name = renameDraft.trim()
    renamingHost = ""
    renameDraft = ""
    // Names live on the device, so they survive reboots, follow the light to
    // another machine, and stay in sync with Elgato's own software.
    Quickshell.execDetached(Model.renameArgs(helper, host, name))
    settleTimer.restart()
  }

  function cancelRename() {
    renamingHost = ""
    renameDraft = ""
  }

  // Blink one light. With several lights on a desk, matching a row to a fixture
  // is otherwise guesswork — this is the same affordance Control Center has.
  function identify(light) {
    if (!light || !light.reachable) return
    identifyingHost = light.host
    Quickshell.execDetached(Model.identifyArgs(helper, light.host))
    identifyTimer.restart()
  }

  function readFirmware() {
    if (firmwareProc.running || !reachableLights.length) return
    firmwareProc.command = Model.firmwareArgs(
      helper, reachableLights.map(function(l) { return l.host }))
    firmwareProc.running = true
  }

  function setGroupMode(enabled) {
    if (groupMode === enabled) return
    groupMode = enabled
    // Section membership changes with the mode, so re-seat the cursor.
    focusSection = groupMode ? "group" : "lights"
    selectedIndex = 0
    clampCursor()
  }

  // Stop the widget from the widget itself, so nobody has to go hunting for a
  // terminal. In the harness that means ending the process; in a real shell it
  // means disabling the plugin, which the shell applies live over IPC — the
  // bar icon goes away without a shell restart.
  function quit() {
    root.close()
    if (harness) {
      Qt.quit()
      return
    }
    Quickshell.execDetached(["omarchy", "plugin", "disable", root.moduleName])
  }

  // ---------------------------------------------------------------- IPC

  IpcHandler {
    target: "nille.elgato-keylight"

    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }

    // Light control without opening the panel, so a Hyprland keybinding can
    // work the lights directly.
    function lightsOn(): void { root.apply(root.hostList(), { power: "on" }) }
    function lightsOff(): void { root.apply(root.hostList(), { power: "off" }) }
    function lightsToggle(): void { root.toggleAll() }
    function brightness(percent: string): void { root.setGroupBrightness(Number(percent)) }
    function kelvin(value: string): void { root.setGroupKelvin(Number(value)) }
    function rescan(): void { root.refresh(true) }
    // Quickshell IPC requires every declared argument, so pass "" to mean
    // "the first light" rather than omitting it.
    function identify(host: string): void {
      root.identify(host ? root.lightFor(host) : root.reachableLights[0])
    }
    function state(): string {
      return JSON.stringify({
        lights: root.lights,
        anyOn: root.anyOn,
        groupMode: root.groupMode,
        error: root.lastError
      })
    }
  }

  // ---------------------------------------------------------------- processes

  Process {
    id: stateProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.publish(text)
    }
    onExited: function(exitCode) {
      // The helper always prints JSON and exits 0, even for unreachable
      // lights. A nonzero exit therefore means it could not run at all, and
      // publish() never fired — so say so instead of showing a blank panel.
      if (exitCode !== 0 && !root.everLoaded) {
        root.lights = []
        root.lastError = "helper not runnable — chmod +x scripts/keylight"
        root.everLoaded = true
      }
    }
    onRunningChanged: {
      if (running) return
      root.loading = false
      if (root.refreshQueued) {
        root.refreshQueued = false
        Qt.callLater(function() { root.refresh(false) })
      }
    }
  }

  Process {
    id: firmwareProc
    stdout: StdioCollector {
      waitForEnd: true
      // Merge rather than replace: a read scoped to the reachable lights must
      // not drop what we already know about a light that is briefly asleep.
      onStreamFinished: {
        var next = {}
        for (var k in root.firmware) next[k] = root.firmware[k]
        var found = Model.firmwareMap(text)
        for (var h in found) next[h] = found[h]
        root.firmware = next
      }
    }
  }

  // Roughly how long the light blinks, so the row's acknowledgement matches
  // what the user sees across the room.
  Timer {
    id: identifyTimer
    interval: 1500
    onTriggered: root.identifyingHost = ""
  }

  // Debounce drags. Short enough that the light tracks the handle, long enough
  // to collapse the burst of events one sweep produces into a few requests.
  Timer {
    id: groupDebounce
    interval: 90
    onTriggered: root.flushGroup()
  }

  Timer {
    id: perLightDebounce
    interval: 90
    onTriggered: root.flushPerLight()
  }

  // Read back shortly after a mutation so the panel reflects what the firmware
  // actually accepted, rather than trusting the optimistic value forever.
  Timer {
    id: settleTimer
    interval: 400
    onTriggered: root.refresh(false)
  }

  Timer {
    interval: root.refreshIntervalSec * 1000
    running: root.opened
    repeat: true
    onTriggered: root.refresh(false)
  }

  // One read at startup so the bar icon means something before the first open.
  Component.onCompleted: refresh(false)

  onOpenedChanged: {
    if (opened) {
      refresh(!hasLights)   // full rescan only when there is nothing to show
      focusSection = hasLights ? (groupMode ? "group" : "lights") : "header"
      selectedIndex = hasLights ? 0 : -1
      cursorActive = false
      cancelRename()
      readFirmware()
      Qt.callLater(function() {
        if (scrollArea && scrollArea.contentItem) scrollArea.contentItem.contentY = 0
      })
    } else {
      cancelRename()
    }
  }

  // ---------------------------------------------------------------- bar button

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight
  visible: !(hideWhenAbsent && everLoaded && !hasLights)

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // foreground comes from the bar, so the glyph picks up the theme's bar text
    // color — including the transparent-bar variant the bar cross-fades to over
    // a maximized window.
    text: Model.barIcon(root.lights)
    foreground: root.barIconColor
    tooltipText: "Key Lights — " + Model.summary(root.lights, root.lastError)

    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.toggleAll()
      else if (buttonCode === Qt.MiddleButton) root.refresh(true)
      else root.toggle()
    }

    onWheelMoved: function(delta) {
      if (!root.reachableLights.length) return
      var wheel = Util.wheelSteps(root.wheelAccumulator, delta)
      root.wheelAccumulator = wheel.remainder
      if (wheel.steps === 0) return
      root.setGroupBrightness(root.shownGroupBrightness + wheel.steps * 5)
    }
  }

  // ---------------------------------------------------------------- panel

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(380))
    contentHeight: panel.fittedContentHeight(panelColumn.implicitHeight, Style.space(560))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      // While renaming, the TextField owns every key until Enter or Escape.
      blocked: root.renamingHost !== ""

      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        if (dy !== 0) root.moveCursor(dy)
        else if (dx !== 0) root.adjustHorizontal(dx)
      }
      onActivateRequested: if (root.cursorActive) root.activateCursor()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        var key = t.toLowerCase()
        if (key === "g") root.setGroupMode(!root.groupMode)
        else if (key === "r") root.refresh(true)
        else if (key === "a") root.toggleAll()
        else if (key === "n") {
          // Rename whichever light the cursor is on.
          if (root.focusSection === "lights")
            root.beginRename(root.lights[root.lightIndexFor(root.selectedIndex)])
        }
        else if (key === "i") {
          if (root.focusSection === "lights")
            root.identify(root.lights[root.lightIndexFor(root.selectedIndex)])
        }
      }

      ScrollView {
        id: scrollArea
        anchors.fill: parent
        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: panelColumn.implicitHeight > height ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff
        Binding {
          target: scrollArea.contentItem
          property: "interactive"
          value: panelColumn.implicitHeight > scrollArea.height
        }

        Column {
          id: panelColumn
          width: scrollArea.availableWidth
          spacing: Style.spacing.panelGap

          // ---------- Hero: lamp · title/status · master switch ----------
          PanelHero {
            id: hero
            width: parent.width
            title: "Key Lights"
            meta: root.loading && !root.everLoaded
              ? "Searching for lights"
              : Model.summary(root.lights, root.lastError)
            detail: root.multiple ? (root.groupMode ? "GROUP" : "EACH") : ""
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconOpacity: root.anyOn ? 1.0 : 0.5

            // Status only — the switch owns power, mouse and keyboard alike.
            iconComponent: Component {
              Text {
                text: Model.barIcon(root.lights)
                color: root.anyOn ? root.foreground : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }

            // The quick on/off: one switch for every light at once, and the
            // header's only cursor target.
            trailingControl: Component {
              ToggleSwitch {
                id: masterSwitch
                checked: root.anyOn
                hasCursor: root.headerHasCursor
                foreground: root.foreground
                accent: root.accent
                enabled: root.reachableLights.length > 0
                opacity: enabled ? 1.0 : 0.4
                onHovered: function(on) { if (on) root.setHeaderCursor() }
                onToggled: root.toggleAll()

                PanelToolTip {
                  visible: masterSwitch.containsMouse
                  text: root.anyOn ? "All lights off" : "All lights on"
                  fontFamily: root.fontFamily
                }
              }
            }
          }

          // ---------- Nothing found ----------
          Column {
            width: parent.width
            spacing: Style.spacing.lg
            visible: root.everLoaded && !root.hasLights

            PanelSeparator { foreground: root.foreground }

            Text {
              width: parent.width
              wrapMode: Text.WordWrap
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              text: root.lastError && root.lastError !== "no-devices"
                ? root.lastError
                : "No Key Lights on the network. They need to be powered on and on the same network as this machine. If mDNS is blocked, set their addresses in the widget settings."
            }

            Button {
              text: "Scan again"
              bordered: true
              foreground: root.foreground
              accent: root.accent
              fontFamily: root.fontFamily
              onClicked: root.refresh(true)
            }
          }

          // ---------- All together vs individually ----------
          Column {
            width: parent.width
            spacing: Style.spacing.md
            visible: root.multiple

            PanelSeparator { foreground: root.foreground }

            ButtonGroup {
              width: parent.width
              options: [
                { value: "group", label: "All together" },
                { value: "each", label: "Individually" }
              ]
              value: root.groupMode ? "group" : "each"
              foreground: root.foreground
              accent: root.accent
              background: root.bar ? root.bar.background : Color.background
              fontFamily: root.fontFamily
              // 'g' drives the mode from the keyboard; the chips are a mouse
              // affordance and stay out of the j/k cursor walk.
              focusable: false
              cursorIndex: -1
              onChanged: function(value) { root.setGroupMode(value === "group") }
            }
          }

          // ---------- Group sliders ----------
          Column {
            width: parent.width
            spacing: Style.spacing.md
            visible: root.hasLights && root.groupMode

            PanelSeparator { foreground: root.foreground }

            LightControls {
              width: parent.width
              kelvin: root.shownGroupKelvin
              brightness: root.shownGroupBrightness
              on: root.anyOn
              mixed: root.groupMixed
              section: "group"
              baseIndex: 0
              onKelvinRequested: function(v) { root.setGroupKelvin(v) }
              onBrightnessRequested: function(v) { root.setGroupBrightness(v) }
            }
          }

          // ---------- One row per light ----------
          Column {
            width: parent.width
            spacing: Style.spacing.sm
            visible: root.hasLights

            PanelSeparator { foreground: root.foreground }

            PanelSectionHeader {
              text: root.multiple ? "LIGHTS" : "LIGHT"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Repeater {
              model: root.lights

              LightRow {
                required property var modelData
                required property int index
                width: panelColumn.width
                light: modelData
                rowIndex: index
              }
            }
          }

          // ---------- Keyboard hints ----------
          Column {
            width: parent.width
            spacing: Style.spacing.md
            visible: root.hasLights

            PanelSeparator { foreground: root.foreground }

            Text {
              width: parent.width
              wrapMode: Text.WordWrap
              color: Qt.darker(root.foreground, 1.9)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              text: "j/k move · h/l adjust · space toggle · a all · g group · n rename · i blink · r rescan"
            }
          }

          // ---------- Quit ----------
          Item {
            width: parent.width
            implicitHeight: quitButton.implicitHeight

            Button {
              id: quitButton
              anchors.right: parent.right
              text: "Quit"
              bordered: true
              foreground: root.foreground
              accent: root.accent
              fontFamily: root.fontFamily
              tooltipText: root.harness
                ? "Exit the harness"
                : "Disable the Key Light widget"
              onClicked: root.quit()
            }
          }
        }
      }
    }
  }

  // ---------------------------------------------------------------- components

  // Temperature + brightness slider pair, used both for the group and inside
  // each light's row, so the two paths render one control rather than two
  // copies that drift apart.
  //
  // `compact` collapses each slider onto a single glyph + track + value line.
  // The group pair gets the panel's usual header-above-slider treatment; the
  // per-light pairs go compact so a three-light setup stays a readable panel
  // instead of six full-height sections.
  component LightControls: Column {
    id: controls

    property int kelvin: 4000
    property int brightness: 0
    property bool on: false
    property bool mixed: false
    property bool compact: false
    property string section: "group"
    property int baseIndex: 0

    signal kelvinRequested(int value)
    signal brightnessRequested(int value)

    readonly property int liveKelvin: Model.clampKelvin(tempSlider.dragging ? tempSlider.liveValue : controls.kelvin)
    readonly property int liveBrightness: Math.round(briSlider.dragging ? briSlider.liveValue : controls.brightness)
    readonly property real dimmed: controls.on ? 1.0 : 0.5
    readonly property color temperatureColor: {
      var rgb = Model.kelvinToRgb(controls.liveKelvin)
      return Qt.rgba(rgb.r, rgb.g, rgb.b, 1)
    }

    spacing: Style.spacing.md

    // ---- Temperature ----
    Item {
      width: parent.width
      visible: !controls.compact
      height: visible ? implicitHeight : 0
      implicitHeight: Math.max(tempHeader.implicitHeight, tempValue.implicitHeight)

      PanelSectionHeader {
        id: tempHeader
        text: "TEMPERATURE"
        foreground: root.foreground
        fontFamily: root.fontFamily
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
      }

      Row {
        id: tempValue
        anchors.right: parent.right
        anchors.rightMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.spacing.md

        // The color the current temperature actually produces — the fastest
        // way to read warm vs cool without parsing a number.
        Rectangle {
          width: Style.space(10)
          height: width
          radius: Style.cornerRadius > 0 ? width / 2 : 0
          anchors.verticalCenter: parent.verticalCenter
          opacity: controls.dimmed
          color: controls.temperatureColor
        }

        Text {
          text: controls.mixed && !tempSlider.dragging
            ? "Mixed"
            : Model.kelvinName(controls.liveKelvin) + " · " + Model.kelvinLabel(controls.liveKelvin)
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
          anchors.verticalCenter: parent.verticalCenter
          opacity: controls.dimmed
        }
      }
    }

    CursorSurface {
      id: tempRow
      width: parent.width
      height: Math.max(tempSlider.implicitHeight, tempGlyph.implicitHeight) + Style.spacing.controlGap
      hasCursor: root.cursorActive && root.focusSection === controls.section
        && root.selectedIndex === controls.baseIndex
      onHasCursorChanged: if (hasCursor) root.ensureCursorVisible(tempRow)
      foreground: root.foreground
      accent: root.accent
      outline: true

      // Thermometer glyph, shown only in compact rows where the section header
      // is gone and the slider still needs to name itself.
      Text {
        id: tempGlyph
        visible: controls.compact
        text: "󰔏"
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.icon
        anchors.left: parent.left
        anchors.leftMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
        opacity: controls.dimmed
      }

      Text {
        id: tempCompactValue
        visible: controls.compact
        text: controls.mixed && !tempSlider.dragging ? "Mixed" : Model.kelvinLabel(controls.liveKelvin)
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        font.bold: true
        anchors.right: parent.right
        anchors.rightMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
        opacity: controls.dimmed
      }

      PanelSlider {
        id: tempSlider
        bar: root.bar
        anchors.left: controls.compact ? tempGlyph.right : parent.left
        anchors.right: controls.compact ? tempCompactValue.left : parent.right
        anchors.leftMargin: Style.space(6)
        anchors.rightMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
        minimum: Model.KELVIN_MIN
        maximum: Model.KELVIN_MAX
        step: Model.KELVIN_STEP
        integer: true
        value: controls.kelvin
        opacity: controls.dimmed
        // Warm-to-cool fill, as on Elgato's own control: the track shows the
        // light's color rather than needing a legend. The knob keeps the
        // theme's foreground so it still reads as an Omarchy slider.
        fillColor: controls.temperatureColor
        onMoved: function(v) { controls.kelvinRequested(v) }
      }

      HoverHandler {
        onHoveredChanged: if (hovered) root.setCursor(controls.section, controls.baseIndex)
      }
    }

    // ---- Brightness ----
    Item {
      width: parent.width
      visible: !controls.compact
      height: visible ? implicitHeight : 0
      implicitHeight: Math.max(briHeader.implicitHeight, briValue.implicitHeight)

      PanelSectionHeader {
        id: briHeader
        text: "BRIGHTNESS"
        foreground: root.foreground
        fontFamily: root.fontFamily
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
      }

      Text {
        id: briValue
        text: controls.mixed && !briSlider.dragging
          ? "Mixed"
          : Model.brightnessName(controls.liveBrightness, controls.on) + " · " + controls.liveBrightness + "%"
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        font.bold: true
        anchors.right: parent.right
        anchors.rightMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
        opacity: controls.dimmed
      }
    }

    CursorSurface {
      id: briRow
      width: parent.width
      height: Math.max(briSlider.implicitHeight, briGlyph.implicitHeight) + Style.spacing.controlGap
      hasCursor: root.cursorActive && root.focusSection === controls.section
        && root.selectedIndex === controls.baseIndex + 1
      onHasCursorChanged: if (hasCursor) root.ensureCursorVisible(briRow)
      foreground: root.foreground
      accent: root.accent
      outline: true

      Text {
        id: briGlyph
        visible: controls.compact
        text: "󰃟"
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.icon
        anchors.left: parent.left
        anchors.leftMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
        opacity: controls.dimmed
      }

      Text {
        id: briCompactValue
        visible: controls.compact
        text: controls.mixed && !briSlider.dragging ? "Mixed" : controls.liveBrightness + "%"
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        font.bold: true
        anchors.right: parent.right
        anchors.rightMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
        opacity: controls.dimmed
      }

      PanelSlider {
        id: briSlider
        bar: root.bar
        anchors.left: controls.compact ? briGlyph.right : parent.left
        anchors.right: controls.compact ? briCompactValue.left : parent.right
        anchors.leftMargin: Style.space(6)
        anchors.rightMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
        minimum: Model.BRIGHTNESS_MIN
        maximum: Model.BRIGHTNESS_MAX
        step: 5
        integer: true
        value: controls.brightness
        opacity: controls.dimmed
        onMoved: function(v) { controls.brightnessRequested(v) }
      }

      HoverHandler {
        onHoveredChanged: if (hovered) root.setCursor(controls.section, controls.baseIndex + 1)
      }
    }
  }

  // One light: power switch, name (editable in place), and — in individual
  // mode — its own temperature and brightness sliders.
  component LightRow: Column {
    id: lightRow

    required property var light
    required property int rowIndex

    // Cursor index of this row's first stop. Group mode gives each light one
    // stop; individual mode gives it three.
    readonly property int cursorBase: root.groupMode ? rowIndex : rowIndex * 3
    readonly property bool rowHasCursor: root.cursorActive
      && root.focusSection === "lights" && root.selectedIndex === cursorBase
    readonly property bool renaming: root.renamingHost === light.host

    spacing: Style.spacing.sm

    CursorSurface {
      id: rowSurface
      width: parent.width
      implicitHeight: rowInner.implicitHeight + Style.spacing.xl
      hasCursor: lightRow.rowHasCursor
      onHasCursorChanged: if (hasCursor) root.ensureCursorVisible(rowSurface)
      current: lightRow.light.on
      foreground: root.foreground
      accent: root.accent
      fill: root.hoverFill
      currentFill: root.selectedFill

      // Declared before rowInner so the switch and the rename field sit above
      // it and receive their own clicks; this only catches the rest of the row.
      MouseArea {
        id: rowMouse
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        acceptedButtons: Qt.LeftButton | Qt.RightButton
        onContainsMouseChanged: if (containsMouse) root.setCursor("lights", lightRow.cursorBase)
        onClicked: function(mouse) {
          if (lightRow.renaming) return
          // Right-click renames. Left-click toggles, so the whole row is the
          // hit target for power and the switch is just its indicator.
          if (mouse.button === Qt.RightButton) root.beginRename(lightRow.light)
          else root.toggleLight(lightRow.light)
        }
      }

      Item {
        id: rowInner
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        anchors.leftMargin: Style.space(6)
        anchors.rightMargin: Style.space(6)
        implicitHeight: Math.max(rowSwitch.implicitHeight, nameLoader.implicitHeight)

        ToggleSwitch {
          id: rowSwitch
          checked: lightRow.light.on
          enabled: lightRow.light.reachable
          opacity: enabled ? 1.0 : 0.4
          foreground: root.foreground
          accent: root.accent
          // The row owns the cursor ring, so the switch is a compact indicator
          // rather than a second highlight competing with it.
          trackHeight: Math.max(16, Math.round(Style.spacing.controlHeight * 0.42))
          cursorRing: false
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter
          onHovered: function(on) { if (on) root.setCursor("lights", lightRow.cursorBase) }
          onToggled: root.toggleLight(lightRow.light)
        }

        Loader {
          id: nameLoader
          anchors.left: rowSwitch.right
          anchors.leftMargin: Style.spacing.xl
          anchors.right: identifyButton.visible ? identifyButton.left : parent.right
          anchors.rightMargin: identifyButton.visible ? Style.spacing.md : 0
          anchors.verticalCenter: parent.verticalCenter
          sourceComponent: lightRow.renaming ? renameEditor : nameLabel
        }

        // Blink this light. Elgato's own app puts an equivalent control on each
        // row; with more than one fixture it is the only way to tell which row
        // is which without walking over and toggling them one at a time.
        Button {
          id: identifyButton
          iconText: "󰛨"
          visible: lightRow.light.reachable && !lightRow.renaming
          tooltipText: "Blink this light"
          foreground: root.foreground
          accent: root.accent
          fontFamily: root.fontFamily
          // The row owns the cursor ring; this stays a quiet glyph until hover.
          active: root.identifyingHost === lightRow.light.host
          horizontalPadding: Style.spacing.sm
          verticalPadding: Style.spacing.xxs
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          onClicked: root.identify(lightRow.light)
          onHovered: function(on) { if (on) root.setCursor("lights", lightRow.cursorBase) }
        }

        Component {
          id: nameLabel

          Column {
            spacing: Style.spacing.xxs

            Text {
              width: parent.width
              text: lightRow.light.name
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              font.bold: lightRow.light.on
              elide: Text.ElideRight
              opacity: lightRow.light.reachable ? 1.0 : 0.5
            }

            // In group mode the per-light sliders are hidden, so the row
            // carries the light's own values as text instead.
            Text {
              width: parent.width
              visible: !lightRow.light.reachable || root.groupMode
              text: lightRow.light.reachable
                ? Model.brightnessName(lightRow.light.brightness, lightRow.light.on)
                  + " · " + Model.kelvinLabel(lightRow.light.kelvin)
                : (lightRow.light.error || "unreachable")
              color: lightRow.light.reachable ? root.dim : root.urgent
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }
          }
        }

        Component {
          id: renameEditor

          TextField {
            foreground: root.foreground
            accent: root.accent
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            // Inline in a row, so trim the field's dialog-height padding.
            verticalPadding: Style.spacing.xs
            placeholderText: lightRow.light.host
            onTextChanged: root.renameDraft = text
            onAccepted: root.commitRename()
            Keys.onEscapePressed: root.cancelRename()
            Component.onCompleted: {
              // Seed imperatively: binding `text` to renameDraft while also
              // writing renameDraft from onTextChanged would fight itself.
              text = root.renameDraft
              Qt.callLater(function() { forceActiveFocus(); selectAll() })
            }
          }
        }
      }

      // Mouse-hover only, as every other panel's row tooltips are. Showing it
      // on the keyboard cursor as well made j/k pop a tooltip over the section
      // header above, and the keyboard already has 'n' for rename.
      //
      // Carries the firmware version because there is nowhere better for it:
      // it is worth being able to check, but not worth a permanent line of
      // panel real estate on a value that changes once a year at most.
      PanelToolTip {
        visible: rowMouse.containsMouse && !lightRow.renaming
        text: {
          var parts = [lightRow.light.host]
          var fw = Model.firmwareLabel(root.firmwareFor(lightRow.light.host))
          if (fw) parts.push(fw)
          parts.push("right-click to rename")
          return parts.join(" · ")
        }
        fontFamily: root.fontFamily
      }
    }

    // Individual mode: this light's own sliders, under its row.
    LightControls {
      width: parent.width
      visible: !root.groupMode && lightRow.light.reachable
      compact: true
      kelvin: root.shownKelvinFor(lightRow.light)
      brightness: root.shownBrightnessFor(lightRow.light)
      on: lightRow.light.on
      section: "lights"
      baseIndex: lightRow.cursorBase + 1
      onKelvinRequested: function(v) { root.setLightKelvin(lightRow.light, v) }
      onBrightnessRequested: function(v) { root.setLightBrightness(lightRow.light, v) }
    }
  }
}
