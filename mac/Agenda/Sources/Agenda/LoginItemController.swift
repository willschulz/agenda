import Foundation
import ServiceManagement

@MainActor
final class LoginItemController: ObservableObject {
    private static let preferenceKey = "AgendaLaunchAtLogin"

    @Published private(set) var enabled: Bool
    @Published private(set) var statusMessage: String?

    init() {
        if AgendaRuntime.isSandbox {
            // A throwaway copy must never register itself to launch at login.
            enabled = false
            statusMessage = "Not available in the sandbox copy."
            return
        }
        let defaults = UserDefaults.standard
        if defaults.object(forKey: Self.preferenceKey) == nil {
            let configured = Bundle.main.object(
                forInfoDictionaryKey: "AgendaDefaultLaunchAtLogin"
            ) as? Bool ?? false
            defaults.set(configured, forKey: Self.preferenceKey)
        }
        enabled = defaults.bool(forKey: Self.preferenceKey)
        apply(enabled)
    }

    func setEnabled(_ newValue: Bool) {
        guard !AgendaRuntime.isSandbox else { return }
        enabled = newValue
        UserDefaults.standard.set(newValue, forKey: Self.preferenceKey)
        apply(newValue)
    }

    func openSystemSettings() {
        SMAppService.openSystemSettingsLoginItems()
    }

    private func apply(_ shouldEnable: Bool) {
        let statusBefore = SMAppService.mainApp.status
        do {
            if shouldEnable {
                if statusBefore == .notRegistered || statusBefore == .notFound {
                    try SMAppService.mainApp.register()
                }
            } else if statusBefore == .enabled || statusBefore == .requiresApproval {
                try SMAppService.mainApp.unregister()
            }
            refreshStatus()
        } catch {
            statusMessage = error.localizedDescription
        }
    }

    private func refreshStatus() {
        switch SMAppService.mainApp.status {
        case .enabled:
            statusMessage = "Enabled"
        case .requiresApproval:
            statusMessage = "Approval required in System Settings"
        case .notFound:
            statusMessage = "Installed app not found"
        case .notRegistered:
            statusMessage = enabled ? "Not registered" : "Disabled"
        @unknown default:
            statusMessage = "Unknown status"
        }
    }
}
