import Foundation

struct User: Codable, Identifiable, Sendable {
    let id: Int
    let username: String
    let displayName: String
    let role: String
    let roles: [String]
    let bootstrap: Bool
    let mustChangePassword: Bool
}

struct Permissions: Codable, Sendable {
    let admin: Bool
    let manageDevices: Bool
    let upload: Bool
    let download: Bool
}

struct MobileSession: Codable, Sendable {
    let sessionToken: String
    let expiresAt: Int
    let user: User
    let permissions: Permissions
}

struct MobileBootstrap: Codable, Sendable {
    struct Push: Codable, Sendable {
        let configured: Bool
        let bundleId: String
        let events: [String: Bool]
    }

    let apiVersion: Int
    let user: User
    let permissions: Permissions
    let push: Push
}

struct MobileRelease: Codable, Identifiable, Hashable, Sendable {
    var id: Int { build }
    let build: Int
    let version: String
    let notes: String
    let releasedAt: String
}

struct MobileReleaseManifest: Codable, Sendable {
    let latest: MobileRelease?
    let current: MobileRelease?
}

struct OnboardingProgress: Codable, Sendable {
    let tourKey: String
    let tourVersion: Int
    let currentStep: Int
    let dismissed: Bool
    let completed: Bool
    let persistent: Bool
}

struct PlatformSummary: Codable, Identifiable, Hashable, Sendable {
    var id: String { platform }
    let platform: String
    let count: Int
    let ratedCount: Int?
}

struct DeviceState: Codable, Identifiable, Hashable, Sendable {
    let id: Int
    let name: String
    let state: String
}

struct Game: Codable, Identifiable, Hashable, Sendable {
    let id: Int
    let platform: String
    let primaryRelpath: String
    let displayName: String
    let `extension`: String
    let size: Int64
    let duplicateStatus: String
    let fileCount: Int
    let deviceCount: Int
    let coverAssetId: Int?
    let coverAssetVersion: String?
    let artworkCount: Int
    let rating: Double?
    let platformRank: Int?
    let rawgRank: Int?
    let selected: Int?
    let onDevice: Int?
    let managed: Int?
    let synced: Int?
    let devices: [DeviceState]
    let deviceState: String?
}

struct GameList: Codable, Sendable {
    let items: [Game]
    let total: Int
    let limit: Int
    let offset: Int
    let deviceInventory: DeviceInventory?
}

struct DeviceInventory: Codable, Sendable {
    struct Platform: Codable, Sendable {
        let platform: String
        let count: Int
        let bytes: Int64?
    }
    let presentGames: Int
    let changes: Int
    let files: Int
    let bytes: Int64
    let platforms: [Platform]
    let selectedPlatforms: [Platform]
}

struct GameDetail: Codable, Sendable {
    let game: GameDetailRecord
    let files: [GameFile]
    let devices: [GameDetailDevice]
    let artwork: ArtworkDetail
}

struct GameDetailDevice: Codable, Identifiable, Sendable {
    let id: Int
    let name: String
    let selected: Int
}

struct ArtworkDetail: Codable, Sendable {
    let metadata: GameMetadata?
    let assets: [GameAsset]
}

struct GameDetailRecord: Codable, Identifiable, Sendable {
    let id: Int
    let platform: String
    let displayName: String
    let primaryRelpath: String
    let size: Int64
    let `extension`: String
}

struct GameFile: Codable, Identifiable, Sendable {
    var id: String { relpath }
    let relpath: String
    let size: Int64
    let kind: String
}

struct GameMetadata: Codable, Sendable {
    let description: String?
    let releaseDate: String?
    let developer: String?
    let publisher: String?
    let players: String?
    let rating: Double?
}

struct GameAsset: Codable, Identifiable, Sendable {
    let id: Int
    let kind: String
    let contentType: String
}

struct Device: Codable, Identifiable, Hashable, Sendable {
    let id: Int
    let name: String
    let deliveryMode: String
    let rosterGroupId: Int?
    let rosterGroupName: String?
    let ownerUserId: Int?
    let selectedGames: Int
    let deployedGames: Int
    let storageCapacityBytes: Int64
    let syncthingReadyAt: String?
}

struct DeviceGroup: Codable, Identifiable, Hashable, Sendable {
    let id: Int
    let name: String
    let ownerUserId: Int
    let deviceCount: Int
    let selectedGames: Int
    let members: [DeviceGroupMember]
}

struct DeviceGroupMember: Codable, Identifiable, Hashable, Sendable {
    let id: Int
    let name: String
    let deliveryMode: String
    let syncthingReadyAt: String?
}

struct DeviceSyncStatus: Codable, Sendable {
    struct SyncRun: Codable, Sendable {
        let state: String
        let completion: Double
        let needBytes: Int64
        let detail: String
        let added: Int
        let removed: Int
    }

    let configured: Bool
    let linked: Bool
    let connected: Bool?
    let completion: Double?
    let status: String?
    let syncRun: SyncRun?
}

struct UploadList: Codable, Sendable {
    let items: [UploadSession]
    let maxBytes: Int64
    let chunkBytes: Int
}

struct UploadSession: Codable, Identifiable, Sendable {
    let id: String
    let platform: String
    let bundleName: String
    let folderMode: Bool
    let status: String
    let totalSize: Int64
    let receivedSize: Int64
    let fileCount: Int
    let reviewNote: String?
    let files: [UploadFile]
    let chunkBytes: Int
}

struct UploadFile: Codable, Identifiable, Sendable {
    var id: Int { fileIndex }
    let fileIndex: Int
    let relativePath: String
    let size: Int64
    let receivedSize: Int64
}

struct InboxResponse: Codable, Sendable {
    let items: [InboxItem]
    let unread: Int
}

struct InboxItem: Codable, Identifiable, Sendable {
    let id: Int
    let kind: String
    let title: String
    let detail: String
    let path: String
    let readAt: String?
    let createdAt: String
}

struct AccountSummary: Codable, Sendable {
    struct AccountDevice: Codable, Identifiable, Sendable {
        let id: Int
        let name: String
        let deliveryMode: String
        let groupName: String?
        let selectedRoms: Int
        let syncedRoms: Int
    }

    struct AccountPlatform: Codable, Identifiable, Sendable {
        var id: String { platform }
        let platform: String
        let syncedRoms: Int
    }

    let user: User
    let devices: [AccountDevice]
    let platforms: [AccountPlatform]
    let totalSyncedRoms: Int
    let uniqueSyncedRoms: Int
}

struct DownloadTicket: Codable, Sendable {
    let url: String
    let filename: String
    let files: Int
    let bytes: Int64
    let expiresAt: Int
}

struct JobReference: Codable, Sendable {
    let jobId: Int
}

struct PushInstallation: Codable, Sendable {
    let id: String
    let appVersion: String
    let notificationsEnabled: Bool
    let pushConfigured: Bool
}

struct EmptyResponse: Codable, Sendable {}
