// Slack icon with the number of unread mentions and DMs; a dot when only channels are unread.
// Click opens the client. The numbers come from `slack unread --cached --json` (kept fresh
// by the sync daemon and the client), so reading them never touches the network.

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "petrzpav.slack"

  readonly property string script: Qt.resolvedUrl("bin/slack").toString().replace(/^file:\/\//, "")
  readonly property bool hideWhenRead: setting("hideWhenRead", false) === true
  property int mentions: 0
  property int unread: 0

  visible: !(hideWhenRead && mentions === 0 && unread === 0)
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function refresh() {
    if (!countProc.running) countProc.running = true
  }

  function open() {
    if (root.bar) root.bar.run(script + "-window")
  }

  IpcHandler {
    target: "petrzpav.slack"
    function refresh(): void { root.broadcast("refresh") }
  }

  Process {
    id: countProc
    command: [root.script, "unread", "--cached", "--json"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try {
          var d = JSON.parse(text.trim())
          root.mentions = d.mentions || 0
          root.unread = d.unread || 0
        } catch (e) {}
      }
    }
  }

  Timer {
    interval: 5000
    running: true
    repeat: true
    onTriggered: root.refresh()
  }

  Component.onCompleted: refresh()

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.mentions > 0 ? " " + root.mentions : (root.unread > 0 ? " •" : "")
    active: root.mentions > 0
    fontSize: Style.font.caption
    horizontalMargin: 6
    tooltipText: root.mentions > 0 ? root.mentions + " unread mentions and DMs"
               : root.unread > 0 ? root.unread + " channels with new messages" : "Slack"
    onPressed: root.open()
  }
}
