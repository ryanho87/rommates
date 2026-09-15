import Foundation

struct SaveVault: Decodable, Identifiable, Sendable {
    let id: Int
    let ownerUserId: Int
}

struct SaveVaultList: Decodable, Sendable { let items: [SaveVault] }
struct SavePage<Item: Decodable & Sendable>: Decodable, Sendable {
    let items: [Item]
    let total: Int
}

struct SaveConnection: Decodable, Identifiable, Sendable {
    let id: Int
    let name: String
    let syncthingDeviceId: String?
    let connectedAt: String?
}

struct SaveOverview: Decodable, Sendable {
    struct Inventory: Decodable, Sendable {
        let available: Bool
        let files: Int
        let bytes: Int64
    }
    struct Conflicts: Decodable, Sendable { let total: Int }
    let inventory: Inventory
    let snapshotCount: Int
    let latestSnapshot: SaveSnapshot?
    let devices: [SaveConnection]
    let conflicts: Conflicts
}

struct SaveFile: Decodable, Identifiable, Sendable {
    var id: String { relpath }
    let relpath: String
    let size: Int64
    let mtimeNs: Int64
    var name: String { (relpath as NSString).lastPathComponent }
    var modified: Date { Date(timeIntervalSince1970: Double(mtimeNs) / 1_000_000_000) }
}

struct SaveSnapshot: Decodable, Identifiable, Sendable {
    let id: Int
    let createdAt: String
    let note: String
    let fileCount: Int
    let logicalBytes: Int64
    let addedCount: Int
    let changedCount: Int
    let removedCount: Int
    let pinned: Int
}

struct SaveConflict: Decodable, Identifiable, Sendable {
    var id: String { conflictRelpath }
    let conflictRelpath: String
    let canonicalRelpath: String
    let canonicalExists: Bool
    let deviceName: String
    let deviceId: String
    let identical: Bool
    let canonicalSize: Int64
    let conflictSize: Int64
    let canonicalMtimeNs: Int64
    let conflictMtimeNs: Int64
    let canonicalSha256: String
    let conflictSha256: String
}

struct SaveComparison: Decodable, Sendable {
    let compatible: Bool
    let reason: String?
    let currentTreeHash: String
    let restore: [String]
    let overwrite: [String]
    let delete: [String]
    let unchanged: Int
}

struct SaveJob: Decodable, Sendable {
    let status: String
    let detail: String
}

struct SaveRestoreBody: Encodable {
    let expectedTreeHash: String
    let retroarchClosed: Bool
}

struct SaveResolveBody: Encodable {
    let conflictRelpath: String
    let decision: String
    let expectedCanonicalSha256: String
    let expectedConflictSha256: String
    let deviceId: String
    let deviceName: String
}

/// Every save request includes the immutable vault ID, including administrator
/// sessions. Never fall back to the server's legacy/default save source.
struct SaveAPI {
    let vaultId: Int

    var query: [URLQueryItem] { [.init(name: "vault_id", value: String(vaultId))] }
}
