import AppKit
import SwiftUI
import WebKit

// MARK: - WebViewState

/// Observable state published by the WKWebView navigation delegate.
/// ContentView reads this to decide whether to show the live page or a
/// native "can't reach Agenda" fallback. This is the cheap hook Phase 2
/// will later replace with real offline-mode switching -- not throwaway.
final class WebViewState: ObservableObject {
    @Published var isLoading: Bool = true
    @Published var loadError: String? = nil

    /// Direct reference to the WKWebView -- NOT @Published so it doesn't
    /// trigger re-renders. Used for the Reload menu command and any future
    /// Swift->JS calls.
    weak var webView: WKWebView? = nil

    func reload() {
        loadError = nil
        webView?.reload()
    }
}

// MARK: - Clipboard bridge

/// Fallback for `navigator.clipboard.writeText` when WKWebView's built-in
/// implementation is unavailable or rejects (a known WebKit restriction
/// tied to user-gesture attribution -- can be stricter for some synthetic
/// or indirectly-attributed clicks than in a full browser). Agenda's own
/// code (agenda/static/index.html) is untouched -- this patches the API at
/// the WebView layer via an injected user script, so the web app keeps
/// calling navigator.clipboard.writeText exactly as it does today.
///
/// The JS shim always tries the native API first and only falls back to
/// this bridge on rejection, so this is pure insurance, not a replacement.
private let clipboardBridgeMessageHandlerName = "agendaClipboard"

private let clipboardBridgeScriptSource = """
(function () {
  if (window.__agendaMacClipboardPatched) return;
  window.__agendaMacClipboardPatched = true;

  var nativeWriteText = (navigator.clipboard && navigator.clipboard.writeText)
    ? navigator.clipboard.writeText.bind(navigator.clipboard)
    : null;

  function bridgeWriteText(text) {
    try {
      window.webkit.messageHandlers.\(clipboardBridgeMessageHandlerName).postMessage(String(text));
      return Promise.resolve();
    } catch (e) {
      return Promise.reject(e);
    }
  }

  if (!navigator.clipboard) {
    Object.defineProperty(navigator, 'clipboard', { value: {}, configurable: true });
  }

  navigator.clipboard.writeText = function (text) {
    if (nativeWriteText) {
      return nativeWriteText(text).catch(function () { return bridgeWriteText(text); });
    }
    return bridgeWriteText(text);
  };
})();
"""

final class ClipboardBridge: NSObject, WKScriptMessageHandler {
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == clipboardBridgeMessageHandlerName, let text = message.body as? String else { return }
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(text, forType: .string)
    }
}

// MARK: - External links

/// Note markdown (and any other `<a href>`) must not navigate the Agenda
/// WKWebView: there is no back button, so an in-app load of GitHub/Docs/etc.
/// traps the session until restart. http(s) opens in Google Chrome when that
/// app is installed; other schemes use the system default handler.
enum AgendaLinkPolicy {
    static func shouldOpenExternally(_ url: URL, appOrigin: URL?) -> Bool {
        guard let scheme = url.scheme?.lowercased(), !scheme.isEmpty else { return false }
        if scheme == "about" || scheme == "javascript" || scheme == "data" {
            return false
        }
        if scheme == "http" || scheme == "https" {
            guard let appOrigin else { return true }
            return !isSameOrigin(url, appOrigin)
        }
        return true
    }

    static func isSameOrigin(_ a: URL, _ b: URL) -> Bool {
        a.scheme?.lowercased() == b.scheme?.lowercased()
            && a.host?.lowercased() == b.host?.lowercased()
            && defaultPort(a) == defaultPort(b)
    }

    static func defaultPort(_ url: URL) -> Int {
        if let port = url.port { return port }
        switch url.scheme?.lowercased() {
        case "https": return 443
        case "http": return 80
        default: return -1
        }
    }

    static func isCancellation(_ error: Error) -> Bool {
        let nsError = error as NSError
        return nsError.domain == NSURLErrorDomain && nsError.code == NSURLErrorCancelled
    }

    static func open(_ url: URL) {
        let scheme = url.scheme?.lowercased()
        if scheme == "http" || scheme == "https",
           let chrome = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.google.Chrome") {
            NSWorkspace.shared.open(
                [url],
                withApplicationAt: chrome,
                configuration: NSWorkspace.OpenConfiguration()
            ) { _, error in
                if error != nil { NSWorkspace.shared.open(url) }
            }
            return
        }
        NSWorkspace.shared.open(url)
    }
}

// MARK: - WebView

/// NSViewRepresentable wrapping WKWebView for the local Agenda web app.
/// Uses the default persistent WKWebsiteDataStore so cookies/localStorage
/// survive across launches.
struct WebView: NSViewRepresentable {
    let url: URL
    @ObservedObject var state: WebViewState

    func makeNSView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        // The embedded server is available only on loopback.

        let userScript = WKUserScript(
            source: clipboardBridgeScriptSource,
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        )
        config.userContentController.addUserScript(userScript)
        config.userContentController.add(context.coordinator.clipboardBridge, name: clipboardBridgeMessageHandlerName)
        // Board pinch-zoom (see PinchSupport.swift). With the native strategy
        // the page ignores WebKit's own pinch events so nothing double-applies.
        let strategy = PinchStrategy.current
        if strategy == .native {
            config.userContentController.addUserScript(WKUserScript(
                source: "window.agendaNativePinch = true;",
                injectionTime: .atDocumentStart,
                forMainFrameOnly: true
            ))
        }
        if PinchLog.isEnabled {
            config.userContentController.addUserScript(WKUserScript(
                source: "window.agendaPinchDebug = true;",
                injectionTime: .atDocumentStart,
                forMainFrameOnly: true
            ))
            config.userContentController.add(
                context.coordinator.pinchLog, name: PinchLog.messageHandlerName)
        }

        let webView = AgendaWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true
        // Whole-page magnification stays off: a pinch zooms only the board,
        // re-laying it out (see agendaPinch in static/index.html).
        webView.allowsMagnification = false
        if PinchLog.isEnabled {
            webView.pinchLog = context.coordinator.pinchLog
            context.coordinator.pinchLog.write("---- launch, strategy=\(strategy.rawValue)")
        }
        if strategy == .native {
            webView.addGestureRecognizer(NSMagnificationGestureRecognizer(
                target: context.coordinator,
                action: #selector(Coordinator.handlePinch(_:))
            ))
        }
        state.webView = webView
        return webView
    }

    func updateNSView(_ webView: WKWebView, context: Context) {
        guard context.coordinator.loadedURL != url else { return }
        context.coordinator.loadedURL = url
        webView.load(URLRequest(url: url))
    }

    func makeCoordinator() -> Coordinator { Coordinator(state: state) }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        let state: WebViewState
        let clipboardBridge = ClipboardBridge()
        let pinchLog = PinchLog()
        var loadedURL: URL? = nil

        init(state: WebViewState) { self.state = state }

        private var lastMagnification: CGFloat = 0

        /// Forwards trackpad pinches to the page as incremental zoom factors,
        /// anchored at the pinch location (page coordinates, top-left origin).
        @objc func handlePinch(_ gesture: NSMagnificationGestureRecognizer) {
            guard let webView = gesture.view as? WKWebView else { return }
            pinchLog.write(String(
                format: "native recognizer state=%ld magnification=%.4f",
                gesture.state.rawValue, gesture.magnification
            ))
            switch gesture.state {
            case .began:
                lastMagnification = 0
            case .changed:
                let delta = gesture.magnification - lastMagnification
                lastMagnification = gesture.magnification
                let point = gesture.location(in: webView)
                let y = webView.isFlipped ? point.y : webView.bounds.height - point.y
                let factor = max(0.5, min(1.5, 1 + delta))
                webView.evaluateJavaScript(
                    "window.agendaPinch && window.agendaPinch(\(factor), \(point.x), \(y))"
                )
            default:
                lastMagnification = 0
                webView.evaluateJavaScript("window.agendaPinchEnd && window.agendaPinchEnd()")
            }
        }

        func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
            DispatchQueue.main.async {
                self.state.isLoading = true
                self.state.loadError = nil
            }
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            DispatchQueue.main.async {
                self.state.isLoading = false
                self.state.loadError = nil
                webView.window?.makeFirstResponder(webView)
            }
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            DispatchQueue.main.async {
                self.state.isLoading = false
                if !AgendaLinkPolicy.isCancellation(error) {
                    self.state.loadError = error.localizedDescription
                }
            }
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            DispatchQueue.main.async {
                self.state.isLoading = false
                if !AgendaLinkPolicy.isCancellation(error) {
                    self.state.loadError = error.localizedDescription
                }
            }
        }

        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
        ) {
            guard let url = navigationAction.request.url else {
                decisionHandler(.allow)
                return
            }
            let origin = loadedURL ?? webView.url
            if AgendaLinkPolicy.shouldOpenExternally(url, appOrigin: origin) {
                AgendaLinkPolicy.open(url)
                decisionHandler(.cancel)
                return
            }
            decisionHandler(.allow)
        }

        func webView(
            _ webView: WKWebView,
            createWebViewWith configuration: WKWebViewConfiguration,
            for navigationAction: WKNavigationAction,
            windowFeatures: WKWindowFeatures
        ) -> WKWebView? {
            if let url = navigationAction.request.url {
                let origin = loadedURL ?? webView.url
                if AgendaLinkPolicy.shouldOpenExternally(url, appOrigin: origin) {
                    AgendaLinkPolicy.open(url)
                } else {
                    webView.load(navigationAction.request)
                }
            }
            return nil
        }

        /// WKWebView does not present JavaScript confirm() dialogs unless its
        /// UI delegate handles them. Agenda uses confirm() to guard destructive
        /// task and project deletion, so silently returning false makes every
        /// trash button appear broken in the native app.
        func webView(
            _ webView: WKWebView,
            runJavaScriptConfirmPanelWithMessage message: String,
            initiatedByFrame frame: WKFrameInfo,
            completionHandler: @escaping (Bool) -> Void
        ) {
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = message
            alert.informativeText = "This action cannot be undone."
            alert.addButton(withTitle: "Delete")
            alert.addButton(withTitle: "Cancel")

            if let window = webView.window {
                alert.beginSheetModal(for: window) { response in
                    completionHandler(response == .alertFirstButtonReturn)
                }
            } else {
                completionHandler(alert.runModal() == .alertFirstButtonReturn)
            }
        }
    }
}
