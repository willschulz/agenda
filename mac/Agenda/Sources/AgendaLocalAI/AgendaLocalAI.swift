import Foundation
#if canImport(FoundationModels)
import FoundationModels

@available(macOS 26.0, *)
@Generable
private struct GeneratedTitleWords {
    @Guide(
        description: "One concise title word per item, in display order.",
        .maximumCount(6)
    )
    var words: [String]
}

@available(macOS 26.0, *)
@Generable
private struct GeneratedInsightDraft {
    @Guide(description: "Copy one candidate_id exactly from the supplied JSON.")
    var candidateID: String

    @Guide(description: "Concise grounded headline, at most 80 characters.")
    var headline: String

    @Guide(description: "One useful sentence grounded only in that candidate, at most 240 characters.")
    var message: String
}

@available(macOS 26.0, *)
@Generable
private struct GeneratedInsightSet {
    @Guide(
        description: "The one to three highest-value non-duplicative insights.",
        .maximumCount(3)
    )
    var insights: [GeneratedInsightDraft]
}

#endif

public struct LocalAIRequest: Codable, Equatable {
    public let operation: String
    public let text: String?
    public let prefix: String?
    public let mode: String?
    public let context: String?

    public init(
        operation: String,
        text: String? = nil,
        prefix: String? = nil,
        mode: String? = nil,
        context: String? = nil
    ) {
        self.operation = operation
        self.text = text
        self.prefix = prefix
        self.mode = mode
        self.context = context
    }
}

public struct NativeInsightDraft: Codable, Equatable {
    public let candidateID: String
    public let headline: String
    public let message: String

    public init(candidateID: String, headline: String, message: String) {
        self.candidateID = candidateID
        self.headline = headline
        self.message = message
    }

    enum CodingKeys: String, CodingKey {
        case candidateID = "candidate_id"
        case headline
        case message
    }
}

public struct LocalAIResponse: Codable, Equatable {
    public let status: String
    public let title: String?
    public let insights: [NativeInsightDraft]?
    public let provider: String?
    public let reason: String?

    enum CodingKeys: String, CodingKey {
        case status
        case title
        case insights
        case provider
        case reason
    }

    public init(
        status: String,
        title: String? = nil,
        insights: [NativeInsightDraft]? = nil,
        provider: String? = nil,
        reason: String? = nil
    ) {
        self.status = status
        self.title = title
        self.insights = insights
        self.provider = provider
        self.reason = reason
    }
}

public enum TitleContract {
    public static let maximumCharacters = 60
    public static let maximumWords = 8
    public static let knownPrefixes = ["FR: ", "BUG: ", "NIT: "]

    public static func normalizedPrefix(_ raw: String?) -> String {
        let candidate = (raw ?? "").uppercased()
        return knownPrefixes.first(where: {
            candidate.trimmingCharacters(in: .whitespaces) ==
                $0.trimmingCharacters(in: .whitespaces)
        }) ?? ""
    }

    public static func modelTitle(_ raw: String, prefix rawPrefix: String?) -> String? {
        let prefix = normalizedPrefix(rawPrefix)
        let nonemptyLines = raw
            .split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        guard nonemptyLines.count == 1 else { return nil }

        var body = nonemptyLines[0]
        body = body.replacingOccurrences(
            of: #"^(?:[-*•]|\d+[.)])\s*"#,
            with: "",
            options: .regularExpression
        )
        for wrapper in ["\"", "'", "`"] {
            if body.hasPrefix(wrapper), body.hasSuffix(wrapper), body.count >= 2 {
                body.removeFirst()
                body.removeLast()
            }
        }
        body = body
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: ".!?;"))
        for knownPrefix in knownPrefixes where
            body.uppercased().hasPrefix(knownPrefix) {
            body.removeFirst(knownPrefix.count)
            body = body.trimmingCharacters(in: .whitespacesAndNewlines)
            break
        }

        let full = prefix + body
        guard !body.isEmpty,
              body.split(whereSeparator: \.isWhitespace).count <= maximumWords,
              full.count <= maximumCharacters
        else { return nil }
        return full
    }

    public static func fallback(_ raw: String, prefix rawPrefix: String?) -> String {
        let prefix = normalizedPrefix(rawPrefix)
        var body = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        for knownPrefix in knownPrefixes where
            body.uppercased().hasPrefix(knownPrefix) {
            body.removeFirst(knownPrefix.count)
            body = body.trimmingCharacters(in: .whitespacesAndNewlines)
            break
        }
        let words = body.split(whereSeparator: \.isWhitespace).map(String.init)
        let limit = max(1, maximumCharacters - prefix.count)
        var selected: [String] = []
        var wasTruncated = false

        for word in words {
            if selected.count == maximumWords {
                wasTruncated = true
                break
            }
            let candidate = (selected + [word]).joined(separator: " ")
            if candidate.count > limit {
                wasTruncated = true
                break
            }
            selected.append(word)
        }
        if selected.count < words.count { wasTruncated = true }

        var result = selected.joined(separator: " ")
        if result.isEmpty, let first = words.first {
            result = String(first.prefix(max(1, limit - 1)))
            wasTruncated = result.count < first.count
        }
        if wasTruncated {
            while result.count >= limit, !result.isEmpty {
                result.removeLast()
            }
            result = result.trimmingCharacters(in: .whitespaces) + "…"
        }
        return prefix + result
    }
}

public enum NativeInsightContract {
    public static let modes = ["plan_today", "stale_work"]
    public static let maximumHeadlineCharacters = 80
    public static let maximumMessageCharacters = 240

    public static func normalize(
        _ drafts: [NativeInsightDraft],
        allowedCandidateIDs: Set<String>
    ) -> [NativeInsightDraft]? {
        guard !drafts.isEmpty, drafts.count <= 3 else { return nil }
        var seen: Set<String> = []
        var normalized: [NativeInsightDraft] = []
        for draft in drafts {
            let candidateID = draft.candidateID.trimmingCharacters(
                in: .whitespacesAndNewlines
            )
            let headline = boundedText(
                draft.headline,
                limit: maximumHeadlineCharacters
            )
            let message = boundedText(
                draft.message,
                limit: maximumMessageCharacters
            )
            guard allowedCandidateIDs.contains(candidateID),
                  !seen.contains(candidateID),
                  !headline.isEmpty,
                  !message.isEmpty
            else { return nil }
            seen.insert(candidateID)
            normalized.append(
                NativeInsightDraft(
                    candidateID: candidateID,
                    headline: headline,
                    message: message
                )
            )
        }
        return normalized
    }

    private static func boundedText(_ raw: String, limit: Int) -> String {
        let collapsed = raw
            .split(whereSeparator: \.isWhitespace)
            .joined(separator: " ")
        guard collapsed.count > limit else { return collapsed }
        let end = collapsed.index(collapsed.startIndex, offsetBy: max(1, limit - 1))
        return collapsed[..<end]
            .trimmingCharacters(in: .whitespacesAndNewlines) + "…"
    }

    public static func candidateIDs(from context: String) -> Set<String>? {
        guard let data = context.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data),
              let root = object as? [String: Any],
              let candidates = root["candidates"] as? [[String: Any]]
        else { return nil }
        return Set(candidates.compactMap { $0["candidate_id"] as? String })
    }
}

public enum LocalInsightGenerator {
    public static func generate(
        mode: String,
        context: String
    ) async -> LocalAIResponse {
        let availability = LocalTitleGenerator.availability()
        guard availability.status == "available" else { return availability }
        guard NativeInsightContract.modes.contains(mode),
              let candidateIDs = NativeInsightContract.candidateIDs(from: context),
              !candidateIDs.isEmpty
        else {
            return LocalAIResponse(status: "error", reason: "invalid-insight-context")
        }

        #if canImport(FoundationModels)
        if #available(macOS 26.0, *) {
            do {
                let session = LanguageModelSession(
                    model: SystemLanguageModel.default,
                    instructions: """
                    You produce concise Agenda task-management insights.
                    Select only from supplied candidates. Copy candidate_id exactly.
                    Never calculate dates, invent facts, merge candidates, or add
                    evidence. Prefer one to three non-duplicative insights that are
                    genuinely useful now. Use calm language; do not manufacture
                    urgency. The headline and message must stand alone in the UI.
                    """
                )
                let response = try await session.respond(
                    to: """
                    Mode: \(mode)
                    Deterministic candidate JSON:
                    \(context)

                    Select and phrase the most useful candidates.
                    """,
                    generating: GeneratedInsightSet.self,
                    options: GenerationOptions(
                        sampling: .greedy,
                        temperature: 0,
                        maximumResponseTokens: 320
                    )
                )
                let drafts = response.content.insights.map {
                    NativeInsightDraft(
                        candidateID: $0.candidateID,
                        headline: $0.headline,
                        message: $0.message
                    )
                }
                guard let normalized = NativeInsightContract.normalize(
                    drafts,
                    allowedCandidateIDs: candidateIDs
                ) else {
                    if ProcessInfo.processInfo.environment["AGENDA_AI_DEBUG"] == "1" {
                        FileHandle.standardError.write(
                            Data("Rejected local insight drafts: \(drafts)\n".utf8)
                        )
                    }
                    return LocalAIResponse(
                        status: "invalid-output",
                        provider: "apple-foundation-models",
                        reason: "insight-contract"
                    )
                }
                return LocalAIResponse(
                    status: "ok",
                    insights: normalized,
                    provider: "apple-foundation-models"
                )
            } catch {
                return LocalAIResponse(
                    status: "error",
                    provider: "apple-foundation-models",
                    reason: String(describing: error)
                )
            }
        }
        #endif
        return LocalAIResponse(status: "unavailable", reason: "os-unsupported")
    }
}

public enum LocalTitleGenerator {
    public static func availability() -> LocalAIResponse {
        #if canImport(FoundationModels)
        if #available(macOS 26.0, *) {
            switch SystemLanguageModel.default.availability {
            case .available:
                return LocalAIResponse(
                    status: "available",
                    provider: "apple-foundation-models"
                )
            case .unavailable(let reason):
                return LocalAIResponse(
                    status: "unavailable",
                    provider: "apple-foundation-models",
                    reason: unavailableReason(reason)
                )
            }
        }
        #endif
        return LocalAIResponse(status: "unavailable", reason: "os-unsupported")
    }

    public static func generate(text: String, prefix: String?) async -> LocalAIResponse {
        let availability = availability()
        guard availability.status == "available" else { return availability }

        #if canImport(FoundationModels)
        if #available(macOS 26.0, *) {
            do {
                let model = SystemLanguageModel.default
                let normalizedPrefix = TitleContract.normalizedPrefix(prefix)
                let bodyCharacterLimit =
                    TitleContract.maximumCharacters - normalizedPrefix.count
                let session = LanguageModelSession(
                    model: model,
                    instructions: """
                    Summarize tasks as natural, concise task-list titles.
                    Return no prefix, quotes, bullets, markdown, or explanation.
                    When a response schema requests words, each array item must
                    contain exactly one word. Prefer 3–6 informative words.
                    Preserve the task's meaning; do not invent details.
                    """
                )
                let options = GenerationOptions(
                    sampling: .greedy,
                    temperature: 0,
                    maximumResponseTokens: 64
                )
                let firstResponse = try await session.respond(
                    to: """
                    Task: \(text)

                    Return one natural title body using 3–6 words and at most \
                    \(bodyCharacterLimit) characters.
                    """,
                    options: options
                )
                if let title = TitleContract.modelTitle(
                    firstResponse.content,
                    prefix: normalizedPrefix
                ) {
                    return LocalAIResponse(
                        status: "ok",
                        title: title,
                        provider: "apple-foundation-models"
                    )
                }

                let structuredResponse = try await session.respond(
                    to: """
                    The previous title violated the contract. Return a shorter,
                    different title as 3–6 individual words.
                    """,
                    generating: GeneratedTitleWords.self,
                    options: options
                )
                guard let title = TitleContract.modelTitle(
                    structuredResponse.content.words.joined(separator: " "),
                    prefix: normalizedPrefix
                ) else {
                    if ProcessInfo.processInfo.environment["AGENDA_AI_DEBUG"] == "1" {
                        FileHandle.standardError.write(
                            Data(
                                "Rejected local title words: \(structuredResponse.content.words)\n".utf8
                            )
                        )
                    }
                    return LocalAIResponse(
                        status: "invalid-output",
                        provider: "apple-foundation-models",
                        reason: "title-contract"
                    )
                }
                return LocalAIResponse(
                    status: "ok",
                    title: title,
                    provider: "apple-foundation-models"
                )
            } catch {
                return LocalAIResponse(
                    status: "error",
                    provider: "apple-foundation-models",
                    reason: String(describing: error)
                )
            }
        }
        #endif
        return LocalAIResponse(status: "unavailable", reason: "os-unsupported")
    }

    #if canImport(FoundationModels)
    @available(macOS 26.0, *)
    private static func unavailableReason(
        _ reason: SystemLanguageModel.Availability.UnavailableReason
    ) -> String {
        switch reason {
        case .deviceNotEligible:
            return "device-not-eligible"
        case .appleIntelligenceNotEnabled:
            return "apple-intelligence-not-enabled"
        case .modelNotReady:
            return "model-not-ready"
        @unknown default:
            return "unknown"
        }
    }
    #endif
}
