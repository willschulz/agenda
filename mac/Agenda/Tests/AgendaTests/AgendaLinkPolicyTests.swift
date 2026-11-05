import XCTest
@testable import Agenda

final class AgendaLinkPolicyTests: XCTestCase {
    private let origin = URL(string: "http://127.0.0.1:8552/")!

    func testAllowsAgendaOriginIncludingPaths() {
        XCTAssertFalse(
            AgendaLinkPolicy.shouldOpenExternally(
                URL(string: "http://127.0.0.1:8552/")!,
                appOrigin: origin
            )
        )
        XCTAssertFalse(
            AgendaLinkPolicy.shouldOpenExternally(
                URL(string: "http://127.0.0.1:8552/api/projects")!,
                appOrigin: origin
            )
        )
    }

    func testOpensExternalHTTPSInBrowser() {
        XCTAssertTrue(
            AgendaLinkPolicy.shouldOpenExternally(
                URL(string: "https://github.com/willschulz/agenda")!,
                appOrigin: origin
            )
        )
    }

    func testOpensOtherLocalPortsExternally() {
        XCTAssertTrue(
            AgendaLinkPolicy.shouldOpenExternally(
                URL(string: "http://127.0.0.1:1948/")!,
                appOrigin: origin
            )
        )
    }

    func testIgnoresAboutAndJavascript() {
        XCTAssertFalse(
            AgendaLinkPolicy.shouldOpenExternally(
                URL(string: "about:blank")!,
                appOrigin: origin
            )
        )
        XCTAssertFalse(
            AgendaLinkPolicy.shouldOpenExternally(
                URL(string: "javascript:void(0)")!,
                appOrigin: origin
            )
        )
    }

    func testMailtoLeavesTheWebView() {
        XCTAssertTrue(
            AgendaLinkPolicy.shouldOpenExternally(
                URL(string: "mailto:will@example.com")!,
                appOrigin: origin
            )
        )
    }

    func testCancellationErrorDetection() {
        let cancelled = NSError(
            domain: NSURLErrorDomain,
            code: NSURLErrorCancelled,
            userInfo: nil
        )
        XCTAssertTrue(AgendaLinkPolicy.isCancellation(cancelled))
        XCTAssertFalse(
            AgendaLinkPolicy.isCancellation(
                NSError(domain: NSURLErrorDomain, code: NSURLErrorTimedOut, userInfo: nil)
            )
        )
    }
}
