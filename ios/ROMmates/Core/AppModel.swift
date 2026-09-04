import Combine
import Foundation
import UIKit
import UserNotifications

enum AppTab: Hashable {
    case library
    case devices
    case uploads
    case inbox
    case account
}

enum PushAuthorizationState: Equatable {
    case unavailable
    case notDetermined
    case authorized
    case denied
}

@MainActor
final class AppModel: ObservableObject {
    enum SessionState: Equatable { case loading, signedOut, signedIn }

    @Published var sessionState: SessionState = .loading
    @Published var user: User?
    @Published var permissions: Permissions?
    @Published var push: MobileBootstrap.Push?
    @Published var pushAuthorization: PushAuthorizationState = .unavailable
    @Published var latestRelease: MobileRelease?
    @Published var currentRelease: MobileRelease?
    @Published var updateAvailable: MobileRelease?
    @Published var presentedRelease: MobileRelease?
    @Published var inboxUnread = 0
    @Published var selectedTab: AppTab = .library
    @Published var isBusy = false
    @Published var errorMessage: String?

    private(set) var baseURL: URL?
    private var token: String?
    private var client: APIClient?
    private var cancellables: Set<AnyCancellable> = []
    private let defaults = UserDefaults.standard

    init() {
        NotificationCenter.default.publisher(for: .rommatesDeviceToken)
            .compactMap { $0.object as? String }
            .receive(on: RunLoop.main)
            .sink { [weak self] token in
                self?.defaults.set(token, forKey: "pushDeviceToken")
                Task { await self?.registerPushToken(token) }
            }
            .store(in: &cancellables)
        NotificationCenter.default.publisher(for: .rommatesPushOpened)
            .compactMap { $0.object as? String }
            .receive(on: RunLoop.main)
            .sink { [weak self] path in self?.openPushPath(path) }
            .store(in: &cancellables)
        NotificationCenter.default.publisher(for: .rommatesPushReceived)
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in Task { await self?.refreshInboxUnread() } }
            .store(in: &cancellables)
    }

    func start() {
        guard
            let saved = defaults.string(forKey: "serverURL"),
            let url = try? ServerAddress.parse(saved),
            let token = KeychainStore.load()
        else {
            sessionState = .signedOut
            return
        }
        configure(url: url, token: token)
        Task { await bootstrap() }
    }

    func signIn(server: String, username: String, password: String) async {
        isBusy = true
        errorMessage = nil
        defer { isBusy = false }
        do {
            let url = try ServerAddress.parse(server)
            let unsigned = APIClient(baseURL: url)
            let body = try JSONEncoder.rommates.encode(
                MobileLoginBody(
                    username: username,
                    password: password,
                    clientName: "ROMmates for iOS \(Self.appVersion)"
                )
            )
            let session: MobileSession = try await unsigned.request(
                "/api/v1/mobile/session",
                method: "POST",
                body: body
            )
            try KeychainStore.save(session.sessionToken)
            defaults.set(url.absoluteString, forKey: "serverURL")
            configure(url: url, token: session.sessionToken)
            user = session.user
            permissions = session.permissions
            sessionState = .signedIn
            await bootstrap()
        } catch {
            errorMessage = error.localizedDescription
            sessionState = .signedOut
        }
    }

    func bootstrap() async {
        guard let client else { return }
        do {
            let response: MobileBootstrap = try await client.request("/api/v1/mobile/bootstrap")
            user = response.user
            permissions = response.permissions
            push = response.push
            sessionState = .signedIn
            await syncPushAuthorization(requestIfNeeded: response.push.configured)
            await checkForUpdates()
            await refreshInboxUnread()
        } catch let error as APIError where error.statusCode == 401 {
            clearSession()
        } catch {
            errorMessage = error.localizedDescription
            if user == nil { sessionState = .signedOut }
        }
    }

    func changePassword(current: String, new: String) async -> Bool {
        do {
            let body = try JSONEncoder.rommates.encode(
                PasswordBody(currentPassword: current, newPassword: new)
            )
            let response: UserResponse = try await request(
                "/api/auth/password", method: "POST", body: body
            )
            user = response.user
            await bootstrap()
            return true
        } catch {
            errorMessage = error.localizedDescription
            return false
        }
    }

    func signOut() async {
        if let client {
            let installationId = Self.installationId
            let _: UnregisterResponse? = try? await client.request(
                "/api/v1/mobile/push-installation/\(installationId)",
                method: "DELETE"
            )
            let _: SignedOutResponse? = try? await client.request(
                "/api/auth/logout", method: "POST"
            )
        }
        clearSession()
    }

    func request<Response: Decodable & Sendable>(
        _ path: String,
        method: String = "GET",
        query: [URLQueryItem] = [],
        body: Data? = nil
    ) async throws -> Response {
        guard let client else {
            throw APIError(statusCode: 401, message: "Sign in to continue.")
        }
        return try await client.request(path, method: method, query: query, body: body)
    }

    func url(path: String) -> URL? { client?.absoluteURL(path: path) }

    func data(path: String) async throws -> Data {
        guard let client else {
            throw APIError(statusCode: 401, message: "Sign in to continue.")
        }
        return try await client.data(path)
    }

    func uploadChunk(path: String, data: Data, offset: Int64) async throws -> UploadSession {
        guard let client else {
            throw APIError(statusCode: 401, message: "Sign in to continue.")
        }
        return try await client.uploadChunk(path, data: data, offset: offset)
    }

    func setPushPreference(kind: String, enabled: Bool) async {
        do {
            let body = try JSONEncoder.rommates.encode(
                PushPreferencesBody(events: [kind: enabled])
            )
            let response: PushPreferencesResponse = try await request(
                "/api/v1/mobile/push-preferences", method: "PUT", body: body
            )
            if let current = push {
                push = .init(
                    configured: current.configured,
                    bundleId: current.bundleId,
                    events: response.events
                )
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func navigate(path: String) { openPushPath(path) }

    func applyUpdatedUser(_ user: User) { self.user = user }

    func setInboxUnread(_ count: Int) {
        inboxUnread = max(0, count)
    }

    func checkForUpdates() async {
        guard client != nil else { return }
        do {
            let manifest: MobileReleaseManifest = try await request(
                "/api/v1/mobile/releases",
                query: [.init(name: "build", value: String(Self.currentBuild))]
            )
            latestRelease = manifest.latest
            currentRelease = manifest.current
            updateAvailable = manifest.latest.flatMap {
                $0.build > Self.currentBuild ? $0 : nil
            }
            let seenBuild = defaults.integer(forKey: "rommates.whats-new-seen-build")
            if let current = manifest.current, current.build > seenBuild {
                presentedRelease = current
                defaults.set(current.build, forKey: "rommates.whats-new-seen-build")
            }
        } catch let error as APIError where error.statusCode == 404 {
            // Older servers do not expose release metadata yet. Core app flows
            // continue to work while the server is upgraded.
        } catch {
            // Update checks are optional and should never interrupt library use.
        }
    }

    func becameActive() async {
        guard sessionState == .signedIn else { return }
        await syncPushAuthorization(requestIfNeeded: false)
        await checkForUpdates()
        await refreshInboxUnread()
    }

    func showReleaseNotes(_ release: MobileRelease? = nil) {
        presentedRelease = release ?? currentRelease ?? latestRelease
    }

    func openTestFlight() {
        guard let url = URL(string: "itms-beta://") else { return }
        UIApplication.shared.open(url)
    }

    func requestPushPermission() async {
        guard push?.configured == true else { return }
        if pushAuthorization == .denied {
            guard let url = URL(string: UIApplication.openSettingsURLString) else { return }
            await UIApplication.shared.open(url)
            return
        }
        await syncPushAuthorization(requestIfNeeded: true)
    }

    private func configure(url: URL, token: String) {
        baseURL = url
        self.token = token
        client = APIClient(baseURL: url, token: token)
    }

    private func clearSession() {
        KeychainStore.clear()
        token = nil
        client = nil
        user = nil
        permissions = nil
        push = nil
        pushAuthorization = .unavailable
        latestRelease = nil
        currentRelease = nil
        updateAvailable = nil
        presentedRelease = nil
        inboxUnread = 0
        sessionState = .signedOut
    }

    private func syncPushAuthorization(requestIfNeeded: Bool) async {
        guard push?.configured == true else {
            pushAuthorization = .unavailable
            return
        }
        let center = UNUserNotificationCenter.current()
        let settings = await center.notificationSettings()
        if requestIfNeeded && settings.authorizationStatus == .notDetermined {
            _ = try? await center.requestAuthorization(options: [.alert, .badge, .sound])
        }
        let updated = await center.notificationSettings()
        switch updated.authorizationStatus {
        case .authorized, .provisional, .ephemeral:
            pushAuthorization = .authorized
            UIApplication.shared.registerForRemoteNotifications()
            if let saved = defaults.string(forKey: "pushDeviceToken") {
                await registerPushToken(saved)
            }
        case .notDetermined:
            pushAuthorization = .notDetermined
        case .denied:
            pushAuthorization = .denied
        @unknown default:
            pushAuthorization = .denied
        }
    }

    private func registerPushToken(_ deviceToken: String) async {
        guard client != nil, push?.configured == true else { return }
        do {
            let body = try JSONEncoder.rommates.encode(
                PushInstallationBody(
                    installationId: Self.installationId,
                    deviceToken: deviceToken,
                    appVersion: Self.appVersion,
                    notificationsEnabled: true
                )
            )
            let _: PushInstallation = try await request(
                "/api/v1/mobile/push-installation", method: "PUT", body: body
            )
        } catch {
            errorMessage = "Push registration failed: \(error.localizedDescription)"
        }
    }

    private func refreshInboxUnread() async {
        guard client != nil else { return }
        guard let response: InboxResponse = try? await request(
            "/api/inbox", query: [.init(name: "limit", value: "1")]
        ) else { return }
        inboxUnread = response.unread
    }

    private func openPushPath(_ path: String) {
        if path.hasPrefix("release") {
            showReleaseNotes(latestRelease)
        }
        else if path.hasPrefix("devices") { selectedTab = .devices }
        else if path.hasPrefix("transfers") { selectedTab = .uploads }
        else { selectedTab = .inbox }
    }

    private static var installationId: String {
        let defaults = UserDefaults.standard
        if let saved = defaults.string(forKey: "installationId") { return saved }
        let value = UUID().uuidString.lowercased()
        defaults.set(value, forKey: "installationId")
        return value
    }

    static var appVersion: String {
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0"
        let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "0"
        return "\(version) (\(build))"
    }

    static var currentBuild: Int {
        let value = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "0"
        return Int(value) ?? 0
    }
}

private struct MobileLoginBody: Encodable {
    let username: String
    let password: String
    let clientName: String
}

private struct PasswordBody: Encodable {
    let currentPassword: String
    let newPassword: String
}

private struct PushInstallationBody: Encodable {
    let installationId: String
    let deviceToken: String
    let appVersion: String
    let notificationsEnabled: Bool
}

private struct PushPreferencesBody: Encodable { let events: [String: Bool] }
private struct PushPreferencesResponse: Decodable, Sendable { let events: [String: Bool] }
private struct UserResponse: Decodable, Sendable { let user: User }
private struct SignedOutResponse: Decodable, Sendable { let signedOut: Bool }
private struct UnregisterResponse: Decodable, Sendable { let unregistered: Bool }

extension Notification.Name {
    static let rommatesDeviceToken = Notification.Name("rommatesDeviceToken")
    static let rommatesPushOpened = Notification.Name("rommatesPushOpened")
    static let rommatesPushReceived = Notification.Name("rommatesPushReceived")
}
