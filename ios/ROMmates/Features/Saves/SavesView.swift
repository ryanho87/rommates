import SwiftUI

struct SavesView: View {
    @EnvironmentObject private var model: AppModel
    @State private var vault: SaveVault?
    @State private var overview: SaveOverview?
    @State private var loading = true
    @State private var busy = false
    @State private var failure: String?
    @State private var message: String?
    @State private var jobId: Int?
    @State private var jobStatus = ""
    @State private var disconnect: SaveConnection?

    var body: some View {
        NavigationStack {
            List {
                if let failure {
                    Section {
                        Text(failure).foregroundStyle(.red)
                        Button("Try Again") { Task { await load() } }
                    }
                }
                if loading && overview == nil {
                    Section { ProgressView("Loading private saves…") }
                } else if let vault, let overview {
                    vaultSections(vault, overview)
                } else if failure == nil {
                    Section {
                        Label("Your saves, across your devices", systemImage: "externaldrive.badge.checkmark")
                            .font(.headline)
                        Text("Keep a private save folder with snapshots and conflict recovery. Only your connected devices share it.")
                        Button("Enable Private Saves") { Task { await enable() } }
                            .disabled(busy)
                    } footer: {
                        Text("This creates a new, empty private folder. Existing shared saves and device connections are not moved or changed.")
                    }
                }
                if let message {
                    Section { Text(message).font(.subheadline).accessibilityLabel(message) }
                }
                if jobId != nil {
                    Section { ProgressView(jobStatus.isEmpty ? "Waiting for server…" : jobStatus) }
                }
            }
            .navigationTitle("Saves")
            .refreshable { await load() }
            .task { await load() }
            .task(id: jobId) { await watchJob() }
            .confirmationDialog("Disconnect save sharing?", isPresented: Binding(
                get: { disconnect != nil }, set: { if !$0 { disconnect = nil } }
            ), titleVisibility: .visible) {
                if let device = disconnect {
                    Button("Disconnect \(device.name)", role: .destructive) {
                        Task { await connect(device, remove: true) }
                    }
                }
            } message: {
                Text("Save files and snapshots are preserved. This device will stop sharing changes with your private saves.")
            }
        }
    }

    @ViewBuilder
    private func vaultSections(_ vault: SaveVault, _ overview: SaveOverview) -> some View {
        Section("Private saves") {
            LabeledContent("On server", value: "\(overview.inventory.files) files · \(ROMTheme.bytes(overview.inventory.bytes))")
            if !overview.inventory.available {
                Label("Save folder unavailable", systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
            }
            NavigationLink {
                SaveCollectionView(vaultId: vault.id, kind: .files)
            } label: { Label("Current Saves", systemImage: "doc.on.doc") }
            NavigationLink {
                SaveCollectionView(vaultId: vault.id, kind: .snapshots)
            } label: { LabeledContent("Snapshots", value: String(overview.snapshotCount)) }
            NavigationLink {
                SaveCollectionView(vaultId: vault.id, kind: .conflicts)
            } label: { LabeledContent("Conflicts", value: String(overview.conflicts.total)) }
            if let snapshot = overview.latestSnapshot {
                LabeledContent("Last snapshot", value: ROMTheme.relativeDate(snapshot.createdAt))
            }
            Button("Snapshot Now") { Task { await snapshotNow() } }
                .disabled(busy || jobId != nil || !overview.inventory.available)
        }
        Section {
            ForEach(overview.devices) { device in
                VStack(alignment: .leading, spacing: 8) {
                    Text(device.name).font(.body).fixedSize(horizontal: false, vertical: true)
                    if device.connectedAt != nil {
                        Label("Save share configured", systemImage: "checkmark.circle").font(.caption).foregroundStyle(.secondary)
                        Button("Disconnect", role: .destructive) { disconnect = device }.disabled(busy)
                    } else if device.syncthingDeviceId?.isEmpty != false {
                        Text("Set up Syncthing in Devices first.").font(.subheadline).foregroundStyle(.secondary)
                    } else {
                        Button("Connect Saves") { Task { await connect(device) } }.disabled(busy)
                    }
                }.padding(.vertical, 4)
            }
            if overview.devices.isEmpty { Text("Add a device from the Devices tab to connect saves.") }
        } header: { Text("My devices") } footer: {
            Text("After connecting, accept the Private saves share in Syncthing on the handheld at Emulation/saves, using Send & Receive. Set RetroArch to save in retroarch and standalone emulators in their own subfolders. A configured share does not mean the handheld has finished syncing.")
        }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            let response: SaveVaultList = try await model.request("/api/save-vaults", fresh: true)
            vault = response.items.first { $0.ownerUserId == model.user?.id }
            if let vault {
                overview = try await model.request("/api/saves", query: SaveAPI(vaultId: vault.id).query, fresh: true)
            }
            failure = nil
        } catch { if !error.isRequestCancellation { failure = error.localizedDescription } }
    }

    private func enable() async {
        busy = true
        defer { busy = false }
        do {
            let _: SaveVault = try await model.request("/api/save-vaults", method: "POST")
            await load()
        } catch { model.report(error) }
    }

    private func connect(_ device: SaveConnection, remove: Bool = false) async {
        guard let vault else { return }
        busy = true
        defer { busy = false }
        do {
            let _: EmptyResponse = try await model.request(
                "/api/save-vaults/\(vault.id)/devices/\(device.id)", method: remove ? "DELETE" : "POST"
            )
            message = remove ? "Save sharing disconnected. Files are preserved." : "Share sent. Accept Private saves in Syncthing on \(device.name), pointing it at Emulation/saves."
            await load()
        } catch { model.report(error) }
    }

    private func snapshotNow() async {
        guard let vault else { return }
        busy = true
        defer { busy = false }
        do {
            let job: JobReference = try await model.request("/api/saves/snapshots", method: "POST",
                query: SaveAPI(vaultId: vault.id).query, body: Data("{}".utf8))
            jobStatus = "Snapshot queued…"
            jobId = job.jobId
        } catch { model.report(error) }
    }

    private func watchJob() async {
        guard let id = jobId else { return }
        do {
            while !Task.isCancelled {
                let job: SaveJob = try await model.request("/api/jobs/\(id)", fresh: true)
                jobStatus = job.detail
                if ["complete", "failed", "cancelled"].contains(job.status) {
                    message = "\(job.status.capitalized): \(job.detail)"
                    await load()
                    jobId = nil
                    return
                }
                try await Task.sleep(for: .seconds(2))
            }
        } catch {
            if !error.isRequestCancellation {
                message = "Unable to check snapshot status. Refresh Saves or check Inbox."
                jobId = nil
                model.report(error)
            }
        }
    }
}

struct SaveCollectionView: View {
    enum Kind: String { case files = "Current Saves", snapshots = "Snapshots", conflicts = "Conflicts" }
    @EnvironmentObject private var model: AppModel
    let vaultId: Int
    let kind: Kind
    @State private var files: [SaveFile] = []
    @State private var snapshots: [SaveSnapshot] = []
    @State private var conflicts: [SaveConflict] = []
    @State private var total = 0
    @State private var loading = true
    @State private var failure: String?
    @State private var search = ""
    @State private var requestID = UUID()

    private var count: Int { kind == .files ? files.count : kind == .snapshots ? snapshots.count : conflicts.count }

    var body: some View {
        Group {
            if kind == .snapshots { contents }
            else { contents.searchable(text: $search, prompt: "Search saves") }
        }
        .navigationTitle(kind.rawValue)
        .refreshable { await load() }
        .task(id: search) {
            do { try await Task.sleep(for: .milliseconds(250)); await load() } catch { }
        }
    }

    private var contents: some View {
        List {
            if let failure { Text(failure).foregroundStyle(.red); Button("Retry") { Task { await load() } } }
            if kind == .files {
                ForEach(files) { file in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(file.name)
                        Text(file.relpath).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                        Text("\(ROMTheme.bytes(file.size)) · \(file.modified.formatted())").font(.caption).foregroundStyle(.secondary)
                    }.padding(.vertical, 4)
                }
            } else if kind == .snapshots {
                ForEach(snapshots) { snapshot in
                    NavigationLink {
                        SaveSnapshotView(vaultId: vaultId, snapshot: snapshot)
                    } label: {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Snapshot #\(snapshot.id)\(snapshot.pinned != 0 ? " · Pinned" : "")")
                            Text("\(snapshot.fileCount) files · \(ROMTheme.bytes(snapshot.logicalBytes)) · \(ROMTheme.relativeDate(snapshot.createdAt))")
                                .font(.caption).foregroundStyle(.secondary)
                            Text("\(snapshot.addedCount) added · \(snapshot.changedCount) changed · \(snapshot.removedCount) removed")
                                .font(.caption).foregroundStyle(.secondary)
                            if !snapshot.note.isEmpty { Text(snapshot.note).font(.subheadline) }
                        }.padding(.vertical, 4)
                    }
                }
            } else {
                ForEach(conflicts) { conflict in
                    NavigationLink {
                        SaveConflictView(vaultId: vaultId, conflict: conflict)
                    } label: {
                        VStack(alignment: .leading, spacing: 4) {
                            Text((conflict.canonicalRelpath as NSString).lastPathComponent)
                            Text(conflict.identical ? "Identical copies" : "Different save versions").font(.caption).foregroundStyle(.secondary)
                            if !conflict.deviceName.isEmpty { Text(conflict.deviceName).font(.caption) }
                        }
                    }
                }
            }
            if loading { ProgressView("Loading…") }
            else if count == 0 && failure == nil {
                Text(kind == .conflicts ? "No conflicts to resolve." : "No \(kind.rawValue.lowercased()) found.").foregroundStyle(.secondary)
            }
            if count < total { Button("Load More (\(count) of \(total))") { Task { await load(more: true) } }.disabled(loading) }
        }
    }

    private func load(more: Bool = false) async {
        let id = UUID()
        requestID = id
        loading = true
        defer { if requestID == id { loading = false } }
        var query = SaveAPI(vaultId: vaultId).query
        query += [.init(name: "limit", value: "100"), .init(name: "offset", value: more ? String(count) : "0"), .init(name: "search", value: search)]
        do {
            switch kind {
            case .files:
                let page: SavePage<SaveFile> = try await model.request("/api/saves/current", query: query, fresh: true)
                guard requestID == id else { return }
                files = more ? files + page.items : page.items; total = page.total
            case .snapshots:
                let page: SavePage<SaveSnapshot> = try await model.request("/api/saves/snapshots", query: query, fresh: true)
                guard requestID == id else { return }
                snapshots = more ? snapshots + page.items : page.items; total = page.total
            case .conflicts:
                let page: SavePage<SaveConflict> = try await model.request("/api/saves/conflicts", query: query, fresh: true)
                guard requestID == id else { return }
                conflicts = more ? conflicts + page.items : page.items; total = page.total
            }
            failure = nil
        } catch { if requestID == id && !error.isRequestCancellation { failure = error.localizedDescription } }
    }
}
