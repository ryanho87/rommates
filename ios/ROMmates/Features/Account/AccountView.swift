import SwiftUI

struct AccountView: View {
    @EnvironmentObject private var model: AppModel
    @State private var summary: AccountSummary?
    @State private var editingProfile = false

    var body: some View {
        NavigationStack {
            List {
                Section {
                    HStack(spacing: 14) {
                        Image(systemName: "person.crop.circle.fill")
                            .font(.system(size: 44)).foregroundStyle(ROMTheme.violet)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(model.user?.displayName ?? "ROMmates user").font(.headline)
                            Text("@\(model.user?.username ?? "")").font(.subheadline).foregroundStyle(.secondary)
                        }
                    }
                    Button("Edit Profile") { editingProfile = true }
                }
                Section("Access") {
                    LabeledContent("Roles") {
                        Text(model.user?.roles.map(roleName).joined(separator: ", ") ?? "Viewer")
                    }
                    LabeledContent("Server", value: model.baseURL?.host ?? "")
                }
                Section("About") {
                    LabeledContent("Version", value: AppModel.appVersion)
                    if let update = model.updateAvailable {
                        Button { model.openTestFlight() } label: {
                            Label("Build \(update.build) is ready", systemImage: "arrow.down.circle.fill")
                        }
                    } else {
                        Label("You’re on the latest build", systemImage: "checkmark.circle.fill")
                            .foregroundStyle(.secondary)
                    }
                }
                Section("Help") {
                    Button {
                        model.startGuidedTour()
                    } label: {
                        Label("Take the Guided Tour", systemImage: "sparkles.rectangle.stack")
                    }
                }
                if let summary {
                    Section("Your library") {
                        LabeledContent("Unique synced ROMs", value: summary.uniqueSyncedRoms.formatted())
                        LabeledContent("Across devices", value: summary.devices.count.formatted())
                        ForEach(summary.platforms.prefix(5)) { platform in
                            LabeledContent(platform.platform, value: platform.syncedRoms.formatted())
                        }
                    }
                }
                Section {
                    if model.push?.configured == true {
                        pushAuthorizationRow
                        ForEach(pushKinds, id: \.key) { item in
                            Toggle(item.label, isOn: Binding(
                                get: { model.push?.events[item.key] ?? true },
                                set: { enabled in Task { await model.setPushPreference(kind: item.key, enabled: enabled) } }
                            ))
                        }
                    } else {
                        Label("Push is not configured on this server", systemImage: "bell.slash")
                            .foregroundStyle(.secondary)
                    }
                } header: {
                    Text("Push notifications")
                } footer: {
                    Text("Notification permission can also be changed in iOS Settings.")
                }
                Section {
                    Button("Sign Out", role: .destructive) { Task { await model.signOut() } }
                }
            }
            .navigationTitle("Account")
            .refreshable { await load(fresh: true) }
            .sheet(isPresented: $editingProfile) { EditProfileView { Task { await load() } } }
            .task { await load() }
        }
    }

    private let pushKinds = [
        (key: "new_build", label: "New TestFlight builds"),
        (key: "device_ready", label: "Device ready"),
        (key: "device_sync", label: "Device delivery complete"),
        (key: "device_apply", label: "Device apply problems"),
        (key: "upload_approved", label: "Upload approved"),
        (key: "upload_rejected", label: "Upload not approved"),
    ]

    @ViewBuilder
    private var pushAuthorizationRow: some View {
        switch model.pushAuthorization {
        case .authorized:
            Label("Notifications allowed", systemImage: "bell.badge.fill")
                .foregroundStyle(.secondary)
        case .notDetermined:
            Button {
                Task { await model.requestPushPermission() }
            } label: {
                Label("Enable notifications", systemImage: "bell.badge")
            }
        case .denied:
            Button {
                Task { await model.requestPushPermission() }
            } label: {
                Label("Notifications off — Open Settings", systemImage: "bell.slash")
            }
        case .unavailable:
            EmptyView()
        }
    }

    private func load(fresh: Bool = false) async {
        do { summary = try await model.request("/api/account/summary", fresh: fresh) }
        catch { model.report(error) }
    }

    private func roleName(_ role: String) -> String {
        switch role {
        case "viewer": return "Viewer"
        case "contributor": return "Contributor"
        case "member": return "Member"
        case "admin": return "Administrator"
        default: return role.capitalized
        }
    }
}

private struct EditProfileView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let didSave: () -> Void
    @State private var username = ""
    @State private var displayName = ""
    @State private var saving = false

    var body: some View {
        NavigationStack {
            Form {
                TextField("Display name", text: $displayName)
                    .textContentType(.name)
                TextField("Username", text: $username)
                    .textContentType(.username)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
            }
            .navigationTitle("Edit Profile")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") { Task { await save() } }
                        .disabled(username.isEmpty || displayName.isEmpty || saving)
                }
            }
            .onAppear {
                username = model.user?.username ?? ""
                displayName = model.user?.displayName ?? ""
            }
        }
    }

    private func save() async {
        saving = true
        defer { saving = false }
        do {
            let body = try JSONEncoder.rommates.encode(
                ProfileBody(username: username, displayName: displayName)
            )
            let response: ProfileResponse = try await model.request(
                "/api/auth/profile", method: "PATCH", body: body
            )
            model.applyUpdatedUser(response.user)
            didSave()
            dismiss()
        } catch { model.report(error) }
    }
}

private struct ProfileBody: Encodable { let username: String; let displayName: String }
private struct ProfileResponse: Decodable, Sendable { let user: User }
