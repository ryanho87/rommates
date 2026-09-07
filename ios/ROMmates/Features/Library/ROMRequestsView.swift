import SwiftUI

struct ROMRequestsView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var requests: [ROMRequest] = []
    @State private var loading = true
    @State private var showingNewRequest = false

    private var openRequests: [ROMRequest] {
        requests.filter { ["requested", "in_progress"].contains($0.status) }
    }

    private var history: [ROMRequest] {
        requests.filter { !["requested", "in_progress"].contains($0.status) }
    }

    var body: some View {
        NavigationStack {
            Group {
                if loading && requests.isEmpty {
                    ProgressView("Loading requests…")
                } else if requests.isEmpty {
                    EmptyState(
                        icon: "text.badge.plus",
                        title: "No ROM requests",
                        message: "Ask for a game that is missing from the library."
                    )
                } else {
                    List {
                        if !openRequests.isEmpty {
                            Section("Open") {
                                ForEach(openRequests) { request in
                                    ROMRequestRow(request: request, cancel: cancel)
                                }
                            }
                        }
                        if !history.isEmpty {
                            Section("History") {
                                ForEach(history) { request in
                                    ROMRequestRow(request: request, cancel: cancel)
                                }
                            }
                        }
                    }
                    .listStyle(.insetGrouped)
                    .refreshable { await load(fresh: true) }
                }
            }
            .navigationTitle("ROM Requests")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Done") { dismiss() }
                }
                ToolbarItem(placement: .primaryAction) {
                    Button { showingNewRequest = true } label: {
                        Label("New Request", systemImage: "plus")
                    }
                }
            }
            .safeAreaInset(edge: .bottom) {
                if requests.isEmpty && !loading {
                    Button { showingNewRequest = true } label: {
                        Label("Request a ROM", systemImage: "plus")
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
            .sheet(isPresented: $showingNewRequest) {
                NewROMRequestView {
                    Task { await load(fresh: true) }
                }
            }
            .task { await load() }
        }
    }

    private func load(fresh: Bool = false) async {
        loading = true
        defer { loading = false }
        do {
            let response: ROMRequestList = try await model.request(
                "/api/rom-requests", fresh: fresh
            )
            requests = response.items
        } catch { model.report(error) }
    }

    private func cancel(_ request: ROMRequest) {
        Task {
            do {
                let _: ROMRequestCancelResponse = try await model.request(
                    "/api/rom-requests/\(request.id)/cancel", method: "POST"
                )
                await load(fresh: true)
            } catch { model.report(error) }
        }
    }
}

private struct ROMRequestRow: View {
    let request: ROMRequest
    let cancel: (ROMRequest) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text(request.title)
                    .font(.body.weight(.semibold))
                Spacer(minLength: 8)
                statusLabel
            }
            HStack(spacing: 8) {
                PlatformBadge(platform: request.platform)
                Text(ROMTheme.relativeDate(request.createdAt))
                    .font(.caption)
                    .foregroundStyle(.tertiary)
            }
            if !request.details.isEmpty {
                Text(request.details)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            if !request.resolutionNote.isEmpty {
                VStack(alignment: .leading, spacing: 3) {
                    Text("UPDATE")
                        .font(.caption2.weight(.bold))
                        .tracking(0.5)
                        .foregroundStyle(ROMTheme.violet)
                    Text(request.resolutionNote)
                        .font(.subheadline)
                }
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(ROMTheme.softViolet.opacity(0.28), in: RoundedRectangle(cornerRadius: 10))
            }
            if request.canCancel {
                Button("Cancel request", role: .destructive) { cancel(request) }
                    .font(.caption.weight(.semibold))
            }
        }
        .padding(.vertical, 5)
    }

    @ViewBuilder
    private var statusLabel: some View {
        switch request.status {
        case "requested":
            StatusLabel(text: "Requested", icon: "clock.fill", color: ROMTheme.warning)
        case "in_progress":
            StatusLabel(text: "In progress", icon: "arrow.triangle.2.circlepath", color: ROMTheme.violet)
        case "fulfilled":
            StatusLabel(text: "Fulfilled", icon: "checkmark.circle.fill", color: ROMTheme.success)
        case "declined":
            StatusLabel(text: "Declined", icon: "xmark.circle.fill", color: ROMTheme.danger)
        default:
            StatusLabel(text: "Cancelled", icon: "minus.circle.fill", color: .secondary)
        }
    }
}

private struct NewROMRequestView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let didSubmit: () -> Void
    @State private var title = ""
    @State private var platform = ""
    @State private var details = ""
    @State private var platforms: [PlatformSummary] = []
    @State private var submitting = false
    @FocusState private var titleFocused: Bool

    var body: some View {
        NavigationStack {
            Form {
                Section("Game") {
                    TextField("Game title", text: $title)
                        .textInputAutocapitalization(.words)
                        .focused($titleFocused)
                    Picker("Platform", selection: $platform) {
                        Text("Choose a platform").tag("")
                        ForEach(platforms) { item in
                            Text(item.platform.uppercased()).tag(item.platform)
                        }
                    }
                }
                Section {
                    TextEditor(text: $details)
                        .frame(minHeight: 96)
                } header: {
                    Text("Notes · Optional")
                } footer: {
                    Text("Include a preferred region, language, version, or edition if it matters.")
                }
            }
            .navigationTitle("Request a ROM")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }.disabled(submitting)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Send") { Task { await submit() } }
                        .disabled(title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || platform.isEmpty || submitting)
                }
            }
            .task {
                do {
                    platforms = try await model.request("/api/platforms")
                    platforms.sort {
                        $0.platform.localizedCaseInsensitiveCompare($1.platform) == .orderedAscending
                    }
                    if platform.isEmpty { platform = platforms.first?.platform ?? "" }
                    titleFocused = true
                } catch { model.report(error) }
            }
        }
    }

    private func submit() async {
        submitting = true
        defer { submitting = false }
        do {
            let body = try JSONEncoder.rommates.encode(
                ROMRequestCreateBody(
                    title: title.trimmingCharacters(in: .whitespacesAndNewlines),
                    platform: platform,
                    details: details.trimmingCharacters(in: .whitespacesAndNewlines)
                )
            )
            let _: ROMRequest = try await model.request(
                "/api/rom-requests", method: "POST", body: body
            )
            didSubmit()
            dismiss()
        } catch { model.report(error) }
    }
}

private struct ROMRequestCreateBody: Encodable {
    let title: String
    let platform: String
    let details: String
}

private struct ROMRequestCancelResponse: Decodable, Sendable {
    let cancelled: Int
}
