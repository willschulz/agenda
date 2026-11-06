import AppKit
import WebKit

/// How a trackpad pinch reaches the board's zoom (window.agendaPinch).
///
/// - `.web`: no native handling. WebKit delivers pinches to the page as
///   gesture events, and the page steps the zoom itself. Default.
/// - `.native`: an NSMagnificationGestureRecognizer on the web view forwards
///   pinches to the page, which then ignores WebKit's own gesture events.
///
/// The sandbox app reads the strategy from its preferences so pinch handling
/// can be compared without rebuilding:
///   defaults write com.willschulz.Agenda.Sandbox AgendaPinchStrategy native
enum PinchStrategy: String {
    case web
    case native

    static var current: PinchStrategy {
        guard AgendaRuntime.isSandbox,
              let raw = UserDefaults.standard.string(forKey: "AgendaPinchStrategy"),
              let strategy = PinchStrategy(rawValue: raw)
        else { return .web }
        return strategy
    }
}

/// Sandbox-only diagnostics: every pinch-related signal the app or the page
/// sees is appended to "Application Support/Agenda Sandbox/pinch-debug.log".
/// Never active in the real app.
final class PinchLog: NSObject, WKScriptMessageHandler {
    static let messageHandlerName = "agendaPinchLog"
    static var isEnabled: Bool { AgendaRuntime.isSandbox }

    private let fileURL: URL? = {
        guard let base = try? FileManager.default.url(
            for: .applicationSupportDirectory, in: .userDomainMask,
            appropriateFor: nil, create: true
        ) else { return nil }
        let dir = base.appendingPathComponent(AgendaRuntime.dataFolderName, isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("pinch-debug.log")
    }()

    private let formatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss.SSS"
        return f
    }()

    func write(_ line: String) {
        guard Self.isEnabled, let fileURL else { return }
        let text = "\(formatter.string(from: Date())) \(line)\n"
        guard let data = text.data(using: .utf8) else { return }
        if let handle = try? FileHandle(forWritingTo: fileURL) {
            handle.seekToEndOfFile()
            handle.write(data)
            try? handle.close()
        } else {
            try? data.write(to: fileURL)
        }
    }

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        write("page   \(message.body)")
    }
}

/// WKWebView that records the raw trackpad events it receives (sandbox only)
/// before handing them to WebKit unchanged.
final class AgendaWebView: WKWebView {
    var pinchLog: PinchLog?

    override func magnify(with event: NSEvent) {
        pinchLog?.write(String(
            format: "native magnify phase=%lu magnification=%.4f",
            event.phase.rawValue, event.magnification
        ))
        super.magnify(with: event)
    }

    override func smartMagnify(with event: NSEvent) {
        pinchLog?.write("native smartMagnify (two-finger double tap)")
        super.smartMagnify(with: event)
    }

    override func mouseDown(with event: NSEvent) {
        pinchLog?.write("native mouseDown clicks=\(event.clickCount)")
        super.mouseDown(with: event)
    }

    override func scrollWheel(with event: NSEvent) {
        if event.modifierFlags.contains(.control) {
            pinchLog?.write(String(format: "native ctrl+scroll dy=%.2f", event.scrollingDeltaY))
        }
        super.scrollWheel(with: event)
    }
}
