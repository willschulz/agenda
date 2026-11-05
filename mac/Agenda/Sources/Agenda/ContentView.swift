import SwiftUI

struct ContentView: View {
    @EnvironmentObject var webState: WebViewState
    @EnvironmentObject var serverController: LocalServerController

    var body: some View {
        ZStack {
            if let url = serverController.activeURL {
                WebView(url: url, state: webState)
            } else if case .error(let message) = serverController.supervisor.status {
                StartupFailureView(
                    message: message,
                    logLines: serverController.supervisor.logLines,
                    retry: {
                        Task { await serverController.retry() }
                    },
                    killStale: message.contains("already in use") ? {
                        Task {
                            await serverController.supervisor.killStaleServer()
                            await serverController.retry()
                        }
                    } : nil
                )
            } else {
                ProgressView(serverController.supervisor.status.message)
            }

            if let error = webState.loadError, serverController.activeURL != nil {
                OfflineFallbackView(
                    error: error,
                    status: serverController.supervisor.status,
                    retry: {
                        Task { await serverController.retry() }
                    },
                    killStale: {
                        Task {
                            await serverController.supervisor.killStaleServer()
                            await serverController.retry()
                        }
                    }
                )
                .task(id: error) {
                    serverController.navigationFailed()
                }
            }
        }
    }
}

/// Actionable failure state for errors that happen before WKWebView can load.
struct StartupFailureView: View {
    let message: String
    let logLines: [String]
    let retry: () -> Void
    let killStale: (() -> Void)?

    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 44))
                .foregroundStyle(.secondary)
            Text("Agenda couldn't start")
                .font(.title2)
                .fontWeight(.semibold)
            Text(message)
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 440)
            if !logLines.isEmpty {
                ScrollView {
                    Text(logLines.suffix(12).joined(separator: "\n"))
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(10)
                }
                .frame(maxWidth: 560, maxHeight: 180)
                .background(.quaternary.opacity(0.4))
                .clipShape(RoundedRectangle(cornerRadius: 8))
            }
            HStack {
                Button("Retry", action: retry)
                    .keyboardShortcut("r", modifiers: .command)
                if let killStale {
                    Button("Stop stale server and retry", action: killStale)
                }
            }
        }
        .padding(28)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(.background)
    }
}

/// Native SwiftUI placeholder shown when the local server navigation fails.
struct OfflineFallbackView: View {
    let error: String
    let status: PythonStatus
    let retry: () -> Void
    let killStale: () -> Void

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "wifi.exclamationmark")
                .font(.system(size: 48))
                .foregroundStyle(.secondary)
            Text("Can't reach Agenda")
                .font(.title2)
                .fontWeight(.semibold)
            Text(error)
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 360)
            Button("Retry", action: retry)
                .keyboardShortcut("r", modifiers: .command)
            if case .error(let message) = status, message.contains("already in use") {
                Button("Stop stale local server and retry", action: killStale)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(.background)
    }
}
