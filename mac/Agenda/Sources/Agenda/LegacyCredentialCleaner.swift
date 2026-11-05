import Foundation
import Security

enum LegacyCredentialCleaner {
    private static let migrationKey = "AgendaDidRetireFrontierCredential"
    private static let keychainService = "com.willschulz.Agenda.openai"
    private static let keychainAccount = "api-key"

    static func runIfNeeded() async {
        guard !UserDefaults.standard.bool(forKey: migrationKey) else { return }
        let status = await Task.detached(priority: .utility) {
            SecItemDelete([
                kSecClass as String: kSecClassGenericPassword,
                kSecAttrService as String: keychainService,
                kSecAttrAccount as String: keychainAccount,
            ] as CFDictionary)
        }.value
        if status == errSecSuccess || status == errSecItemNotFound {
            UserDefaults.standard.set(true, forKey: migrationKey)
        }
    }
}
