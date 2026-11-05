import CloudKit
import Foundation

/// CloudKit rejects a single fetch or modify request with more than 400 items
/// (`CKError.limitExceeded`, "BatchTooLarge"). After a long gap between
/// devices the needed set can exceed that, which used to fail the whole sync
/// cycle. Every multi-record request goes through these helpers.
public let agendaCloudKitMaxBatchSize = 400

public func agendaBatches<T>(
    _ items: [T],
    size: Int = agendaCloudKitMaxBatchSize
) -> [ArraySlice<T>] {
    precondition(size > 0)
    return stride(from: 0, to: items.count, by: size).map {
        items[$0..<min($0 + size, items.count)]
    }
}

extension CKDatabase {
    public func agendaRecords(
        for ids: [CKRecord.ID]
    ) async throws -> [CKRecord.ID: Result<CKRecord, Error>] {
        var merged: [CKRecord.ID: Result<CKRecord, Error>] = [:]
        for batch in agendaBatches(ids) {
            let fetched = try await records(for: Array(batch))
            merged.merge(fetched) { _, new in new }
        }
        return merged
    }

    public func agendaSaveRecords(
        _ records: [CKRecord],
        savePolicy: CKModifyRecordsOperation.RecordSavePolicy
    ) async throws -> [CKRecord.ID: Result<CKRecord, Error>] {
        var merged: [CKRecord.ID: Result<CKRecord, Error>] = [:]
        for batch in agendaBatches(records) {
            let response = try await modifyRecords(
                saving: Array(batch),
                deleting: [],
                savePolicy: savePolicy,
                atomically: false
            )
            merged.merge(response.saveResults) { _, new in new }
        }
        return merged
    }
}
