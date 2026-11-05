import Foundation

struct AgendaTypingInvalidationGate {
    private(set) var activeSessionIDs = Set<String>()
    private(set) var hasDeferredInvalidation = false

    var isTyping: Bool {
        !activeSessionIDs.isEmpty
    }

    mutating func begin(sessionID: String) {
        activeSessionIDs.insert(sessionID)
    }

    mutating func receiveInvalidation() -> Bool {
        guard isTyping else { return true }
        hasDeferredInvalidation = true
        return false
    }

    mutating func end(sessionID: String) -> Bool {
        activeSessionIDs.remove(sessionID)
        guard !isTyping, hasDeferredInvalidation else { return false }
        hasDeferredInvalidation = false
        return true
    }
}

final class AgendaTodoTextBuffer {
    var text: String

    init(text: String) {
        self.text = text
    }
}
