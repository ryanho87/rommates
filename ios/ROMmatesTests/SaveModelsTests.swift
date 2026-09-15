import XCTest
@testable import ROMmates

final class SaveModelsTests: XCTestCase {
    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder.rommates.decode(type, from: Data(json.utf8))
    }

    func testVaultIdentityAndExplicitScope() throws {
        let vault = try decode(SaveVault.self, #"{"id":7,"owner_user_id":12,"storage_key":"vault-immutable"}"#)
        XCTAssertEqual(vault.ownerUserId, 12)
        XCTAssertEqual(SaveAPI(vaultId: vault.id).query, [URLQueryItem(name: "vault_id", value: "7")])
    }

    func testEmptyPrivateOverview() throws {
        let overview = try decode(SaveOverview.self, #"{"inventory":{"available":true,"files":0,"bytes":0},"snapshot_count":0,"latest_snapshot":null,"conflicts":{"total":0},"devices":[{"id":2,"name":"Handheld","syncthing_device_id":null,"connected_at":null}]}"#)
        XCTAssertEqual(overview.inventory.files, 0)
        XCTAssertNil(overview.latestSnapshot)
        XCTAssertNil(overview.devices.first?.connectedAt)
    }

    func testSnapshotDatabaseIntegerFlags() throws {
        let page = try decode(SavePage<SaveSnapshot>.self, #"{"items":[{"id":3,"created_at":"2026-09-14 12:00:00","note":"Trip","file_count":2,"logical_bytes":128,"added_count":1,"changed_count":1,"removed_count":0,"pinned":1}],"total":1}"#)
        XCTAssertEqual(page.items.first?.pinned, 1)
        XCTAssertEqual(page.items.first?.logicalBytes, 128)
    }

    func testNanosecondDatesPreserveRange() throws {
        let file = try decode(SaveFile.self, #"{"relpath":"retroarch/Pokemon.srm","size":131072,"mtime_ns":1700000000000000000}"#)
        XCTAssertEqual(file.name, "Pokemon.srm")
        XCTAssertEqual(file.modified.timeIntervalSince1970, 1700000000)
    }

    func testConflictHashesAreIncludedInResolution() throws {
        let conflict = try decode(SaveConflict.self, #"{"conflict_relpath":"retroarch/a.sync-conflict.srm","canonical_relpath":"retroarch/a.srm","canonical_exists":false,"device_name":"","device_id":"ABC","identical":false,"canonical_size":0,"conflict_size":64,"canonical_mtime_ns":0,"conflict_mtime_ns":1700000000000000000,"canonical_sha256":"","conflict_sha256":"abc123"}"#)
        let body = SaveResolveBody(conflictRelpath: conflict.conflictRelpath, decision: "conflict", expectedCanonicalSha256: conflict.canonicalSha256, expectedConflictSha256: conflict.conflictSha256, deviceId: conflict.deviceId, deviceName: conflict.deviceName)
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder.rommates.encode(body)) as? [String: String])
        XCTAssertEqual(json["expected_canonical_sha256"], "")
        XCTAssertEqual(json["expected_conflict_sha256"], "abc123")
        XCTAssertEqual(json["decision"], "conflict")
    }

    func testRestorePreviewIncludesRemovalsAndSafetyHash() throws {
        let preview = try decode(SaveComparison.self, #"{"compatible":true,"current_tree_hash":"hash","restore":["a"],"overwrite":["b"],"delete":["c"],"unchanged":4}"#)
        XCTAssertEqual(preview.delete, ["c"])
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder.rommates.encode(SaveRestoreBody(expectedTreeHash: preview.currentTreeHash, retroarchClosed: true))) as? [String: Any])
        XCTAssertEqual(json["expected_tree_hash"] as? String, "hash")
        XCTAssertEqual(json["retroarch_closed"] as? Bool, true)
    }

    func testIncompatibleSnapshotAndFailedJob() throws {
        let preview = try decode(SaveComparison.self, #"{"compatible":false,"reason":"Previous source","current_tree_hash":"","restore":[],"overwrite":[],"delete":[],"unchanged":0}"#)
        XCTAssertFalse(preview.compatible)
        XCTAssertEqual(preview.reason, "Previous source")
        let job = try decode(SaveJob.self, #"{"status":"failed","detail":"Save changed since preview","result":null}"#)
        XCTAssertEqual(job.status, "failed")
    }
}
