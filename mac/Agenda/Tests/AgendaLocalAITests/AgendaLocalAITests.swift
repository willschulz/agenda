import XCTest
@testable import AgendaLocalAI

final class AgendaLocalAITests: XCTestCase {
    func testAcceptsConciseModelTitle() {
        XCTAssertEqual(
            TitleContract.modelTitle(
                "\"Fix launch-at-login registration\"",
                prefix: "BUG: "
            ),
            "BUG: Fix launch-at-login registration"
        )
    }

    func testRemovesDuplicateKnownPrefix() {
        XCTAssertEqual(
            TitleContract.modelTitle(
                "FR: Add local title generation",
                prefix: "FR: "
            ),
            "FR: Add local title generation"
        )
    }

    func testRejectsMultilineAndOverlongOutput() {
        XCTAssertNil(
            TitleContract.modelTitle(
                "Useful title\nHere is an explanation.",
                prefix: nil
            )
        )
        XCTAssertNil(
            TitleContract.modelTitle(
                "This generated title contains far too many words for Agenda cards",
                prefix: nil
            )
        )
        XCTAssertNil(
            TitleContract.modelTitle(
                String(repeating: "x", count: 61),
                prefix: nil
            )
        )
    }

    func testFallbackPreservesPrefixAndLimits() {
        let result = TitleContract.fallback(
            "BUG: This is an exceptionally long todo description that needs a safe deterministic title",
            prefix: "BUG: "
        )
        XCTAssertTrue(result.hasPrefix("BUG: "))
        XCTAssertLessThanOrEqual(result.count, TitleContract.maximumCharacters)
        let body = result.dropFirst("BUG: ".count)
        XCTAssertLessThanOrEqual(
            body.split(whereSeparator: \.isWhitespace).count,
            TitleContract.maximumWords
        )
        XCTAssertTrue(result.hasSuffix("…"))
    }

    func testFallbackHandlesSingleLongToken() {
        let result = TitleContract.fallback(
            String(repeating: "a", count: 100),
            prefix: nil
        )
        XCTAssertEqual(result.count, TitleContract.maximumCharacters)
        XCTAssertTrue(result.hasSuffix("…"))
    }

    func testAvailabilityResponseIsJSON() throws {
        let response = LocalTitleGenerator.availability()
        let data = try JSONEncoder().encode(response)
        let decoded = try JSONDecoder().decode(LocalAIResponse.self, from: data)
        XCTAssertEqual(decoded, response)
        XCTAssertTrue(["available", "unavailable"].contains(decoded.status))
    }

    func testInsightRequestAndResponseJSONContract() throws {
        XCTAssertEqual(
            NativeInsightContract.modes,
            ["plan_today", "stale_work"]
        )
        let request = LocalAIRequest(
            operation: "insights",
            mode: "plan_today",
            context: #"{"candidates":[{"candidate_id":"today-1"}]}"#
        )
        let decodedRequest = try JSONDecoder().decode(
            LocalAIRequest.self,
            from: JSONEncoder().encode(request)
        )
        XCTAssertEqual(decodedRequest, request)

        let response = LocalAIResponse(
            status: "ok",
            insights: [
                NativeInsightDraft(
                    candidateID: "today-1",
                    headline: "Finish the waiting review",
                    message: "It is already ready for your attention."
                )
            ],
            provider: "apple-foundation-models"
        )
        let data = try JSONEncoder().encode(response)
        let json = try XCTUnwrap(
            JSONSerialization.jsonObject(with: data) as? [String: Any]
        )
        let encodedInsights = try XCTUnwrap(json["insights"] as? [[String: Any]])
        XCTAssertEqual(encodedInsights.first?["candidate_id"] as? String, "today-1")
        XCTAssertEqual(
            try JSONDecoder().decode(LocalAIResponse.self, from: data),
            response
        )
    }

    func testInsightContractRequiresKnownUniqueCandidates() {
        let valid = [
            NativeInsightDraft(
                candidateID: "candidate-1",
                headline: "Review the finished work",
                message: "This item is ready for human review."
            )
        ]
        XCTAssertEqual(
            NativeInsightContract.normalize(
                valid,
                allowedCandidateIDs: ["candidate-1"]
            ),
            valid
        )
        XCTAssertNil(
            NativeInsightContract.normalize(
                valid,
                allowedCandidateIDs: ["different-candidate"]
            )
        )
        XCTAssertNil(
            NativeInsightContract.normalize(
                valid + valid,
                allowedCandidateIDs: ["candidate-1"]
            )
        )

        let overlong = NativeInsightDraft(
            candidateID: "candidate-1",
            headline: String(repeating: "h", count: 100),
            message: String(repeating: "message ", count: 50)
        )
        let bounded = NativeInsightContract.normalize(
            [overlong],
            allowedCandidateIDs: ["candidate-1"]
        )
        XCTAssertEqual(
            bounded?.first?.headline.count,
            NativeInsightContract.maximumHeadlineCharacters
        )
        XCTAssertEqual(
            bounded?.first?.message.count,
            NativeInsightContract.maximumMessageCharacters
        )
    }

    func testInsightContextExtractsCandidateIDs() {
        let context = """
        {"candidates":[
          {"candidate_id":"one"},
          {"candidate_id":"two"}
        ]}
        """
        XCTAssertEqual(
            NativeInsightContract.candidateIDs(from: context),
            ["one", "two"]
        )
    }

}
