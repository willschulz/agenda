import SwiftUI
import Sparkle

final class AppLifecycleDelegate: NSObject, NSApplicationDelegate {
    weak var supervisor: PythonSupervisor?

    func applicationWillTerminate(_ notification: Notification) {
        supervisor?.forceStop()
    }
}

@main
struct AgendaApp: App {
    @StateObject private var webState = WebViewState()
    @StateObject private var serverController = LocalServerController()
    @StateObject private var loginItemController = LoginItemController()
    @NSApplicationDelegateAdaptor(AppLifecycleDelegate.self) private var appDelegate
    private let updaterController: SPUStandardUpdaterController

    init() {
        updaterController = SPUStandardUpdaterController(
            startingUpdater: true,
            updaterDelegate: nil,
            userDriverDelegate: nil
        )
        // Single-instance guard: if another copy of this app is already
        // running (same bundle ID), activate its window and quit self
        // immediately instead of opening a second window.
        let bundleID = Bundle.main.bundleIdentifier ?? "com.willschulz.Agenda"
        let others = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID)
            .filter { $0 != NSRunningApplication.current }
        if let existing = others.first {
            existing.activate(options: .activateIgnoringOtherApps)
            exit(0)
        }
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(webState)
                .environmentObject(serverController)
                .frame(minWidth: 480, minHeight: 400)
                .task {
                    appDelegate.supervisor = serverController.supervisor
                    await LegacyCredentialCleaner.runIfNeeded()
                    await serverController.start()
                }
        }
        .windowToolbarStyle(.unified)
        .commands {
            CommandGroup(after: .appInfo) {
                Button("Check for Updates…") {
                    updaterController.updater.checkForUpdates()
                }
                .disabled(!updaterController.updater.canCheckForUpdates)
            }
            CommandGroup(after: .toolbar) {
                Button("Reload") {
                    webState.reload()
                }
                .keyboardShortcut("r", modifiers: .command)
            }
        }

        Settings {
            SettingsView(updater: updaterController.updater)
                .environmentObject(serverController)
                .environmentObject(loginItemController)
        }
    }
}

struct SettingsView: View {
    let updater: SPUUpdater
    @EnvironmentObject var serverController: LocalServerController
    @EnvironmentObject var loginItemController: LoginItemController

    private var versionLabel: String {
        let info = Bundle.main.infoDictionary ?? [:]
        let version = info["CFBundleShortVersionString"] as? String ?? "unknown"
        let build = info["CFBundleVersion"] as? String ?? "unknown"
        return "Version \(version) (\(build))"
    }

    private func revealBackups() {
        guard let support = try? FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        ) else { return }
        let backups = support
            .appendingPathComponent("Agenda", isDirectory: true)
            .appendingPathComponent("backups", isDirectory: true)
        try? FileManager.default.createDirectory(
            at: backups,
            withIntermediateDirectories: true
        )
        NSWorkspace.shared.open(backups)
    }

    private func revealAgentIntegrationPack() {
        guard let resources = Bundle.main.resourceURL else { return }
        let pack = resources.appendingPathComponent(
            "agent-integration",
            isDirectory: true
        )
        NSWorkspace.shared.open(pack)
    }

    private func reportProblem() {
        let os = ProcessInfo.processInfo.operatingSystemVersionString
        let body = """
        ## What happened?


        ## What did you expect?


        ## Diagnostics
        - Agenda \(versionLabel)
        - macOS \(os)
        - Server status: \(serverController.supervisor.status.message)

        This issue is public. Do not include private project names, notes, file
        paths, API keys, or unreviewed logs.
        """
        var components = URLComponents(
            string: "https://github.com/willschulz/agenda-releases/issues/new"
        )
        components?.queryItems = [
            URLQueryItem(name: "labels", value: "bug"),
            URLQueryItem(name: "title", value: "Bug: "),
            URLQueryItem(name: "body", value: body),
        ]
        if let url = components?.url {
            NSWorkspace.shared.open(url)
        }
    }

    var body: some View {
        Form {
            Text("Agenda")
                .font(.headline)
            Text("Local data: \(serverController.activeURL?.absoluteString ?? "Starting…")")
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(versionLabel)
                .font(.caption)
                .foregroundStyle(.secondary)

            Section("Data and support") {
                Button("Check for Updates…") {
                    updater.checkForUpdates()
                }
                .disabled(!updater.canCheckForUpdates)
                Button("Reveal Backups in Finder", action: revealBackups)
                Button(
                    "Reveal Agent Integration Pack…",
                    action: revealAgentIntegrationPack
                )
                Button("Report a Problem…", action: reportProblem)
                Text("Agenda keeps automatic local snapshots. Problem reports open the public support tracker with version and basic diagnostics prefilled; local logs are never attached automatically.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Section("On-device intelligence") {
                Text("Agenda uses Apple Intelligence locally for concise todo titles and Pulse insights when available. Board evidence and deterministic fallbacks keep these features useful without a model or network connection.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Section {
                Toggle(
                    "Launch Agenda at login",
                    isOn: Binding(
                        get: { loginItemController.enabled },
                        set: { loginItemController.setEnabled($0) }
                    )
                )
                if let message = loginItemController.statusMessage {
                    Text(message)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if loginItemController.statusMessage?.contains("Approval required") == true {
                    Button("Open Login Items Settings") {
                        loginItemController.openSystemSettings()
                    }
                }
            }
        }
        .padding(24)
        .frame(width: 470)
    }
}
