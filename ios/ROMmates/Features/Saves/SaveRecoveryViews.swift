import SwiftUI

struct SaveSnapshotView: View {
    @EnvironmentObject private var model: AppModel
    let vaultId: Int
    let snapshot: SaveSnapshot
    @State private var comparison: SaveComparison?
    @State private var confirmed = false
    @State private var busy = false
    @State private var pinned: Bool
    @State private var jobId: Int?
    @State private var failure: String?

    init(vaultId: Int, snapshot: SaveSnapshot) {
        self.vaultId = vaultId
        self.snapshot = snapshot
        _pinned = State(initialValue: snapshot.pinned != 0)
    }

    var body: some View {
        List {
            Section {
                Text("\(snapshot.fileCount) files · \(ROMTheme.bytes(snapshot.logicalBytes))")
                Text(snapshot.createdAt).foregroundStyle(.secondary)
                if !snapshot.note.isEmpty { Text(snapshot.note) }
                Button(pinned ? "Unpin Snapshot" : "Pin Snapshot") { Task { await pin() } }.disabled(busy)
            } footer: { Text("Pinned snapshots are kept when automatic retention removes older snapshots.") }
            if let failure { Section { Text(failure).foregroundStyle(.red) } }
            if jobId == nil {
                Section {
                    Button(comparison == nil ? "Preview Restore" : "Refresh Restore Preview") { Task { await preview() } }
                        .disabled(busy)
                } footer: { Text("A restore replaces your private save folder with this snapshot and can remove newer files. A safety snapshot is created first.") }
                if let comparison {
                    if comparison.compatible {
                        changes("Restore missing files", paths: comparison.restore)
                        changes("Replace changed files", paths: comparison.overwrite)
                        changes("Remove newer files", paths: comparison.delete)
                        Section {
                            Text("\(comparison.unchanged) files unchanged")
                            Toggle("All emulators are closed and Syncthing has finished", isOn: $confirmed)
                            Button("Restore This Snapshot", role: .destructive) { Task { await restore() } }
                                .disabled(!confirmed || busy)
                        } footer: { Text("The restored saves will sync to your connected devices. If saves change after this preview, the server will reject the restore. Refresh the preview before trying again.") }
                    } else { Text(comparison.reason ?? "This snapshot cannot be restored.") }
                }
            } else if let jobId {
                SaveJobProgress(jobId: jobId)
            }
            if busy { ProgressView("Working…") }
        }
        .navigationTitle("Snapshot #\(snapshot.id)")
        .navigationBarTitleDisplayMode(.inline)
    }

    @ViewBuilder private func changes(_ title: String, paths: [String]) -> some View {
        if !paths.isEmpty {
            Section {
                DisclosureGroup("\(title) (\(paths.count))") {
                    ForEach(paths, id: \.self) { Text($0).font(.caption).textSelection(.enabled) }
                }
            }
        }
    }

    private func pin() async {
        busy = true
        defer { busy = false }
        do {
            let _: EmptyResponse = try await model.request("/api/saves/snapshots/\(snapshot.id)/pin", method: "PUT",
                query: SaveAPI(vaultId: vaultId).query, body: JSONEncoder.rommates.encode(["pinned": !pinned]))
            pinned.toggle()
        } catch { model.report(error) }
    }

    private func preview() async {
        busy = true
        confirmed = false
        comparison = nil
        defer { busy = false }
        do {
            comparison = try await model.request("/api/saves/snapshots/\(snapshot.id)/compare", query: SaveAPI(vaultId: vaultId).query, fresh: true)
            failure = nil
        } catch { if !error.isRequestCancellation { failure = error.localizedDescription } }
    }

    private func restore() async {
        guard let comparison, comparison.compatible, confirmed else { return }
        busy = true
        defer { busy = false }
        do {
            let job: JobReference = try await model.request("/api/saves/snapshots/\(snapshot.id)/restore", method: "POST",
                query: SaveAPI(vaultId: vaultId).query,
                body: JSONEncoder.rommates.encode(SaveRestoreBody(expectedTreeHash: comparison.currentTreeHash, retroarchClosed: confirmed)))
            jobId = job.jobId
        } catch {
            self.comparison = nil
            confirmed = false
            model.report(error)
        }
    }
}

struct SaveConflictView: View {
    @EnvironmentObject private var model: AppModel
    let vaultId: Int
    let conflict: SaveConflict
    @State private var decision = ""
    @State private var confirmed = false
    @State private var busy = false
    @State private var jobId: Int?
    @State private var stale = false

    var body: some View {
        List {
            Section {
                Text(conflict.canonicalRelpath).textSelection(.enabled)
                Text(conflict.identical ? "Both versions contain identical data." : "These saves differ. ROMmates cannot merge game progress or tell which playthrough you want to keep.")
                    .foregroundStyle(.secondary)
            }
            if jobId == nil {
                Section("Choose the version to keep") {
                    if conflict.canonicalExists {
                        version("Current save", size: conflict.canonicalSize, mtime: conflict.canonicalMtimeNs, value: "current")
                    }
                    version(conflict.deviceName.isEmpty ? "Conflicting save" : "Save from \(conflict.deviceName)",
                        size: conflict.conflictSize, mtime: conflict.conflictMtimeNs, value: "conflict")
                }
                Section {
                    Toggle("All emulators are closed and Syncthing has finished", isOn: $confirmed)
                    Button("Keep Selected Version", role: .destructive) { Task { await resolve() } }
                        .disabled(decision.isEmpty || !confirmed || busy || stale)
                } footer: {
                    Text("The selected version becomes the current save and the conflict copy is removed. A safety snapshot preserves both versions first. The result syncs to your connected devices.")
                }
                if stale { Text("Reload the conflict list before trying again, so the versions you review are current.").foregroundStyle(.orange) }
            } else if let jobId { SaveJobProgress(jobId: jobId) }
            if busy { ProgressView("Submitting…") }
        }
        .navigationTitle("Resolve Conflict")
        .navigationBarTitleDisplayMode(.inline)
    }

    private func version(_ title: String, size: Int64, mtime: Int64, value: String) -> some View {
        Button { decision = value } label: {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(title)
                    Text("\(ROMTheme.bytes(size)) · \(Date(timeIntervalSince1970: Double(mtime) / 1_000_000_000).formatted())")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Image(systemName: decision == value ? "checkmark.circle.fill" : "circle")
                    .foregroundStyle(ROMTheme.violet)
            }.padding(.vertical, 6)
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(decision == value ? [.isSelected] : [])
        .disabled(busy || stale)
    }

    private func resolve() async {
        busy = true
        defer { busy = false }
        do {
            let body = SaveResolveBody(conflictRelpath: conflict.conflictRelpath, decision: decision,
                expectedCanonicalSha256: conflict.canonicalSha256, expectedConflictSha256: conflict.conflictSha256,
                deviceId: conflict.deviceId, deviceName: conflict.deviceName)
            let job: JobReference = try await model.request("/api/saves/conflicts/resolve", method: "POST",
                query: SaveAPI(vaultId: vaultId).query, body: JSONEncoder.rommates.encode(body))
            jobId = job.jobId
        } catch {
            stale = true
            model.report(error)
        }
    }
}

struct SaveJobProgress: View {
    @EnvironmentObject private var model: AppModel
    let jobId: Int
    @State private var detail = "Waiting for server…"
    @State private var status = "queued"
    @State private var failure: String?
    @State private var retry = 0
    private var finished: Bool { ["complete", "failed", "cancelled"].contains(status) }

    var body: some View {
        Section {
            if let failure {
                Text(failure).foregroundStyle(.red)
                Button("Check Again") { retry += 1 }
            } else if finished {
                Label(status.capitalized, systemImage: status == "complete" ? "checkmark.circle" : "exclamationmark.triangle")
                Text(detail)
            } else { ProgressView(detail) }
        } footer: { Text("This runs on the server. You can leave this screen and check Saves or Inbox later.") }
        .task(id: retry) {
            failure = nil
            do {
                while !Task.isCancelled {
                    let job: SaveJob = try await model.request("/api/jobs/\(jobId)", fresh: true)
                    status = job.status
                    detail = job.detail
                    if finished { return }
                    try await Task.sleep(for: .seconds(2))
                }
            } catch { if !error.isRequestCancellation { failure = "Could not check job status: \(error.localizedDescription)" } }
        }
    }
}
