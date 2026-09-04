import SwiftUI

struct RootView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Group {
            switch model.sessionState {
            case .loading:
                ProgressView("Opening ROMmates…")
            case .signedOut:
                SignInView()
            case .signedIn:
                if model.user?.mustChangePassword == true {
                    PasswordChangeView()
                } else {
                    MainTabView()
                }
            }
        }
        .alert(
            "ROMmates",
            isPresented: Binding(
                get: { model.errorMessage != nil },
                set: { if !$0 { model.errorMessage = nil } }
            ),
            actions: { Button("OK") { model.errorMessage = nil } },
            message: { Text(model.errorMessage ?? "") }
        )
        .sheet(item: $model.presentedRelease) { release in
            ReleaseNotesView(release: release)
                .environmentObject(model)
                .presentationDetents([.medium, .large])
                .presentationDragIndicator(.visible)
        }
    }
}

private struct MainTabView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        VStack(spacing: 0) {
            if let release = model.updateAvailable {
                ReleaseAnnouncementBanner(release: release)
                Divider()
            }
            TabView(selection: $model.selectedTab) {
                LibraryView()
                    .tabItem { Label("Library", systemImage: "books.vertical") }
                    .tag(AppTab.library)
                if model.permissions?.manageDevices == true {
                    DevicesView()
                        .tabItem { Label("Devices", systemImage: "gamecontroller") }
                        .tag(AppTab.devices)
                }
                if model.permissions?.upload == true {
                    UploadsView()
                        .tabItem { Label("Uploads", systemImage: "arrow.up.doc") }
                        .tag(AppTab.uploads)
                }
                InboxView()
                    .tabItem { Label("Inbox", systemImage: "tray") }
                    .badge(model.inboxUnread)
                    .tag(AppTab.inbox)
                AccountView()
                    .tabItem { Label("Account", systemImage: "person.crop.circle") }
                    .tag(AppTab.account)
            }
        }
        .animation(.snappy, value: model.updateAvailable)
        .overlay(alignment: .bottom) {
            if let step = model.activeGuidedTourStep,
               let index = model.guidedTourIndex {
                GuidedTourCard(
                    step: step,
                    index: index,
                    count: model.guidedTourSteps.count,
                    back: model.goBackInGuidedTour,
                    next: model.advanceGuidedTour,
                    skip: model.skipGuidedTour
                )
                .padding(.horizontal, 16)
                .padding(.bottom, 76)
                .transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .animation(.snappy, value: model.guidedTourIndex)
    }
}

private struct ReleaseAnnouncementBanner: View {
    @EnvironmentObject private var model: AppModel
    let release: MobileRelease

    var body: some View {
        Button { model.openTestFlight() } label: {
            HStack(spacing: 12) {
                Image(systemName: "sparkles")
                    .font(.headline)
                    .foregroundStyle(ROMTheme.violet)
                    .frame(width: 30, height: 30)
                    .background(ROMTheme.softViolet, in: Circle())
                VStack(alignment: .leading, spacing: 2) {
                    Text("Build \(release.build) is ready")
                        .font(.subheadline.weight(.semibold))
                    Text("Update in TestFlight")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 8)
                Image(systemName: "chevron.right")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.tertiary)
            }
            .contentShape(Rectangle())
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
        }
        .buttonStyle(.plain)
        .background(.regularMaterial)
        .accessibilityHint("Opens TestFlight to install the update")
    }
}

private struct ReleaseNotesView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let release: MobileRelease

    private var isUpdate: Bool { release.build > AppModel.currentBuild }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    VStack(alignment: .leading, spacing: 12) {
                        Image(systemName: "sparkles.rectangle.stack.fill")
                            .font(.system(size: 38, weight: .semibold))
                            .symbolRenderingMode(.palette)
                            .foregroundStyle(ROMTheme.violet, ROMTheme.softViolet)
                        Text(isUpdate ? "A new build is ready" : "What’s new")
                            .font(.largeTitle.bold())
                            .tracking(-0.7)
                        Text("ROMmates \(release.version) · Build \(release.build)")
                            .font(.subheadline.weight(.medium))
                            .foregroundStyle(.secondary)
                    }
                    Text(release.notes)
                        .font(.body)
                        .lineSpacing(5)
                        .textSelection(.enabled)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(24)
            }
            .background(Color(.systemGroupedBackground))
            .navigationTitle("Release Notes")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
            .safeAreaInset(edge: .bottom) {
                if isUpdate {
                    Button {
                        model.openTestFlight()
                    } label: {
                        Label("Open TestFlight", systemImage: "arrow.down.circle.fill")
                            .font(.headline)
                            .frame(maxWidth: .infinity)
                            .frame(minHeight: 48)
                    }
                    .buttonStyle(.borderedProminent)
                    .padding(.horizontal, 20)
                    .padding(.vertical, 12)
                    .background(.regularMaterial)
                }
            }
        }
    }
}

private struct SignInView: View {
    @EnvironmentObject private var model: AppModel
    @AppStorage("serverURL") private var savedServer = ""
    @State private var server = ""
    @State private var username = ""
    @State private var password = ""
    @FocusState private var focus: Field?

    enum Field { case server, username, password }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 28) {
                    VStack(alignment: .leading, spacing: 12) {
                        Image(systemName: "rectangle.portrait.on.rectangle.portrait")
                            .font(.system(size: 36, weight: .semibold))
                            .foregroundStyle(ROMTheme.violet)
                            .accessibilityHidden(true)
                        Text("Your library,\nclose at hand.")
                            .font(.largeTitle.bold())
                            .tracking(-0.8)
                        Text("Sign in to the public HTTPS address for your ROMmates server.")
                            .foregroundStyle(.secondary)
                    }
                    VStack(spacing: 0) {
                        TextField("https://rommates.example.com", text: $server)
                            .textContentType(.URL)
                            .keyboardType(.URL)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .focused($focus, equals: .server)
                            .submitLabel(.next)
                            .onSubmit { focus = .username }
                            .padding()
                        Divider().padding(.leading)
                        TextField("Username", text: $username)
                            .textContentType(.username)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .focused($focus, equals: .username)
                            .submitLabel(.next)
                            .onSubmit { focus = .password }
                            .padding()
                        Divider().padding(.leading)
                        SecureField("Password", text: $password)
                            .textContentType(.password)
                            .focused($focus, equals: .password)
                            .submitLabel(.go)
                            .onSubmit { submit() }
                            .padding()
                    }
                    .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 16))
                    Button(action: submit) {
                        HStack {
                            Spacer()
                            if model.isBusy { ProgressView().tint(.white) }
                            else { Text("Sign In").fontWeight(.semibold) }
                            Spacer()
                        }
                        .frame(minHeight: 50)
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(model.isBusy || server.isEmpty || username.isEmpty || password.isEmpty)
                    Text("Administrator accounts continue to use the web app.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity)
                }
                .padding(24)
                .frame(maxWidth: 520)
                .frame(maxWidth: .infinity)
            }
            .background(Color(.systemGroupedBackground))
            .onAppear { if server.isEmpty { server = savedServer } }
        }
    }

    private func submit() {
        focus = nil
        Task { await model.signIn(server: server, username: username, password: password) }
    }
}

private struct PasswordChangeView: View {
    @EnvironmentObject private var model: AppModel
    @State private var current = ""
    @State private var new = ""
    @State private var confirmation = ""

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    SecureField("Temporary password", text: $current)
                    SecureField("New password", text: $new)
                    SecureField("Confirm new password", text: $confirmation)
                } header: {
                    Text("Secure your account")
                } footer: {
                    Text("Use at least 12 characters. Your current iPhone session will remain signed in.")
                }
                Button("Change Password") {
                    guard new == confirmation else {
                        model.errorMessage = "The new passwords do not match."
                        return
                    }
                    Task { _ = await model.changePassword(current: current, new: new) }
                }
                .disabled(current.isEmpty || new.count < 12 || new != confirmation)
            }
            .navigationTitle("Change Password")
        }
    }
}
