import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// chttr in the bar: whether chttr is running, loading its speech model, listening,
// transcribing or recording a meeting (with a running timer), and a small dot when
// cleanup is off. Left click opens chttr's menu (or starts chttr when it's off);
// right click starts or stops a meeting recording, or hands-free dictation if the
// widget's "Right click" setting says so. Before setup, a click runs bin/setup.
BarWidget {
  id: root
  moduleName: "robjobbed.chttr"

  readonly property string pluginDir: {
    var url = Qt.resolvedUrl(".").toString()
    if (url.indexOf("file://") === 0) url = url.substring(7)
    if (url.length > 1 && url.charAt(url.length - 1) === "/") url = url.substring(0, url.length - 1)
    return decodeURIComponent(url)
  }
  readonly property string exe: Quickshell.env("HOME") + "/.local/bin/chttr"

  // From bin/chttr-status, one JSON line per change.
  property string chttrState: "idle"
  property string feedTooltip: ""
  property string cleanup: ""
  // The meeting's running time: the feed's count plus the time since that line, so
  // the timer keeps ticking between lines.
  property int meetingBase: -1
  property real meetingAt: 0
  property real now: Date.now()

  readonly property bool showMeetingTimer: String(setting("showMeetingTimer", true)) !== "false"
  readonly property bool rightClickHandsFree: String(setting("rightClick", "meeting")) === "handsfree"
  readonly property bool inMeeting: meetingBase >= 0
  readonly property int meetingSeconds: inMeeting ? meetingBase + Math.max(0, Math.floor((now - meetingAt) / 1000)) : 0
  readonly property bool busy: chttrState === "recording" || chttrState === "transcribing" || chttrState === "meeting"
  readonly property bool running: chttrState !== "off" && chttrState !== "setup"

  function clock(secs) {
    var h = Math.floor(secs / 3600), m = Math.floor(secs % 3600 / 60), s = secs % 60
    var mm = h > 0 && m < 10 ? "0" + m : String(m)
    return (h > 0 ? h + ":" : "") + mm + ":" + (s < 10 ? "0" : "") + s
  }

  readonly property string tooltip: {
    var text = feedTooltip
    if (chttrState === "setup") return "chttr: click to set up"
    if (text === "") text = chttrState === "off" ? "chttr is off · click to start" : "chttr"
    if (rightClickHandsFree) {
      text = text.replace("right click: record a meeting", "right click: hands-free dictation")
      text = text.replace("right click to stop", "stop it from the menu")
    }
    return text
  }

  function press(button) {
    if (chttrState === "setup") {
      Quickshell.execDetached(["omarchy-launch-floating-terminal-with-presentation", pluginDir + "/bin/setup"])
    } else if (chttrState === "off") {
      // Any click starts the service (a meeting can't start without it); if that fails,
      // show chttr doctor so the click never does nothing.
      Quickshell.execDetached(["sh", "-c", "systemctl --user start chttr.service || " +
                               "exec omarchy-launch-floating-terminal-with-presentation \"$0\" doctor", exe])
    } else if (button === Qt.RightButton) {
      Quickshell.execDetached(rightClickHandsFree ? [exe, "toggle"] : [exe, "meeting", "toggle"])
    } else {
      Quickshell.execDetached([exe, "menu"])
    }
  }

  implicitWidth: row.implicitWidth
  implicitHeight: row.implicitHeight

  Process {
    id: feed
    command: [root.pluginDir + "/bin/chttr-status"]
    running: true
    stdout: SplitParser {
      onRead: function (line) {
        try {
          const data = JSON.parse(line)
          root.chttrState = String(data.alt || data["class"] || "idle")
          root.feedTooltip = String(data.tooltip || "")
          root.cleanup = String(data.cleanup || "")
          const secs = data.meeting_seconds
          root.now = Date.now()
          root.meetingAt = root.now
          root.meetingBase = typeof secs === "number" && secs >= 0 ? Math.floor(secs) : -1
        } catch (e) {}
      }
    }
    onExited: restart.start()
  }

  Timer {
    id: restart
    interval: 3000
    onTriggered: feed.running = true
  }

  Timer {
    interval: 1000
    repeat: true
    running: root.inMeeting && root.showMeetingTimer
    onTriggered: root.now = Date.now()
  }

  Row {
    id: row
    anchors.centerIn: parent

    Item {
      id: glyphSlot
      implicitWidth: glyph.implicitWidth
      implicitHeight: glyph.implicitHeight

      BarIconButton {
        id: glyph
        anchors.fill: parent
        bar: root.bar
        text: {
          if (root.chttrState === "recording") return "\u{F036C}"      // microphone
          if (root.chttrState === "transcribing") return "\u{F051F}"   // timer sand
          if (root.chttrState === "loading") return "\u{F051F}"
          if (root.chttrState === "meeting") return "\u{F044A}"        // record
          if (!root.running) return "\u{F036D}"                        // microphone off
          return "\u{F036C}"
        }
        active: root.chttrState === "recording" || root.chttrState === "meeting"
        dimmed: !root.busy
        tooltipText: root.tooltip
        onPressed: function (button) { root.press(button) }
      }

      // Transcribing: a slow pulse. On the slot, so it doesn't fight the button's
      // own opacity (dimmed/active).
      SequentialAnimation on opacity {
        running: root.chttrState === "transcribing"
        loops: Animation.Infinite
        NumberAnimation { to: 0.35; duration: 520; easing.type: Easing.InOutQuad }
        NumberAnimation { to: 1.0; duration: 520; easing.type: Easing.InOutQuad }
        onRunningChanged: if (!running) glyphSlot.opacity = 1
      }

      // Cleanup is off (no Claude key, no Ollama): a small dot at the glyph's corner.
      Rectangle {
        readonly property real size: Math.max(3, Math.round(Style.spaceReal(4)))
        visible: root.running && root.cleanup === "none"
        width: size
        height: size
        radius: size / 2
        color: Commons.Color.muted
        x: Math.round(parent.width / 2 + glyph.opticalSize / 2 - size / 2)
        y: Math.round(parent.height / 2 - glyph.opticalSize / 2 - size / 4)
      }
    }

    WidgetButton {
      id: timer
      visible: root.showMeetingTimer && root.inMeeting && root.running && !root.vertical
      bar: root.bar
      text: root.clock(root.meetingSeconds)
      horizontalMargin: 3
      active: root.chttrState === "meeting"
      tooltipText: root.tooltip
      onPressed: function (button) { root.press(button) }
    }
  }
}
