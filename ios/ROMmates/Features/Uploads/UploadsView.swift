import SwiftUI
import UniformTypeIdentifiers

struct UploadsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var uploads: [UploadSession] = []
    @State private var importing = false
    @State private var selectedFiles: [URL] = []
    @State private var showingPlan = false
    @State private var loading = true

    var body: some View {
        NavigationStack {
            Group {
                if loading && uploads.isEmpty {
                    ProgressView("Loading uploads…")
                } else if uploads.isEmpty {
                    EmptyState(
                        icon: "arrow.up.doc",
                        title: "No uploads",
                        message: "Choose a ROM file to contribute it for administrator review."
                    )
                } else {
                    List {
                        ForEach(uploads) { upload in UploadRow(upload: upload) }
                    }
                    .refreshable { await load(fresh: true) }
                }
            }
            .navigationTitle("Uploads")
            .toolbar {
                Button { importing = true } label: {
                    Label("Upload ROM", systemImage: "plus")
                }
            }
            .fileImporter(
                isPresented: $importing,
                allowedContentTypes: [.data, .archive, .diskImage],
                allowsMultipleSelection: true
            ) { result in
                do {
                    selectedFiles = try result.get()
                    showingPlan = !selectedFiles.isEmpty
                } catch { model.report(error) }
            }
            .sheet(isPresented: $showingPlan) {
                UploadPlanView(files: selectedFiles) {
                    showingPlan = false
                    selectedFiles = []
                    Task { await load() }
                }
            }
            .task { await load() }
        }
    }

    private func load(fresh: Bool = false) async {
        loading = true
        defer { loading = false }
        do {
            let response: UploadList = try await model.request("/api/uploads", fresh: fresh)
            uploads = response.items
        } catch { model.report(error) }
    }
}

private struct UploadRow: View {
    let upload: UploadSession

    private var progress: Double {
        upload.totalSize == 0 ? 0 : Double(upload.receivedSize) / Double(upload.totalSize)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text(upload.bundleName.isEmpty ? upload.files.first?.relativePath ?? "ROM upload" : upload.bundleName)
                        .font(.body.weight(.medium)).lineLimit(1)
                    Text("\(upload.platform) · \(ROMTheme.bytes(upload.totalSize))")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                StatusLabel(
                    text: statusText,
                    icon: statusIcon,
                    color: upload.status == "rejected" ? .red : ROMTheme.violet
                )
            }
            if upload.status == "uploading" { ProgressView(value: progress) }
            if upload.status == "rejected", let note = upload.reviewNote, !note.isEmpty {
                Text(note).font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 5)
    }

    private var statusText: String {
        switch upload.status {
        case "uploading": return "Uploading"
        case "pending_review": return "Awaiting review"
        case "finalizing": return "Adding to library"
        case "rejected": return "Not approved"
        default: return upload.status.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    private var statusIcon: String {
        switch upload.status {
        case "pending_review": return "clock.fill"
        case "rejected": return "xmark.circle.fill"
        case "complete": return "checkmark.circle.fill"
        default: return "arrow.up.circle.fill"
        }
    }
}

private struct UploadPlanView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let files: [URL]
    let didSubmit: () -> Void
    @State private var platforms: [PlatformSummary] = []
    @State private var platform = ""
    @State private var bundleName = ""
    @State private var uploading = false
    @State private var progress = 0.0
    @State private var status = "Ready to upload"

    var body: some View {
        NavigationStack {
            Form {
                Section("Destination") {
                    Picker("Platform", selection: $platform) {
                        Text("Choose a platform").tag("")
                        ForEach(platforms) { Text($0.platform).tag($0.platform) }
                    }
                    if files.count > 1 {
                        TextField("Bundle name", text: $bundleName)
                    }
                }
                Section("Files") {
                    ForEach(files, id: \.self) { file in
                        Label(file.lastPathComponent, systemImage: "doc")
                            .lineLimit(1)
                    }
                }
                if uploading {
                    Section {
                        ProgressView(value: progress)
                        Text(status).font(.caption).foregroundStyle(.secondary)
                    }
                }
                Section {
                    Text("Contributions are uploaded securely, then held for an administrator to approve.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Contribute ROM")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }.disabled(uploading)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Upload") { Task { await upload() } }
                        .disabled(platform.isEmpty || uploading || files.count > 1 && bundleName.isEmpty)
                }
            }
            .task {
                do { platforms = try await model.request("/api/platforms") }
                catch { model.report(error) }
            }
        }
    }

    private func upload() async {
        let access = files.map { $0.startAccessingSecurityScopedResource() }
        defer {
            for (index, file) in files.enumerated() where access[index] {
                file.stopAccessingSecurityScopedResource()
            }
        }
        uploading = true
        defer { uploading = false }
        do {
            let specs = try files.map { file -> UploadManifestBody.FileSpec in
                let values = try file.resourceValues(forKeys: [.fileSizeKey])
                return .init(relativePath: file.lastPathComponent, size: Int64(values.fileSize ?? 0))
            }
            let body = try JSONEncoder.rommates.encode(
                UploadManifestBody(
                    platform: platform,
                    bundleName: files.count > 1 ? bundleName : "",
                    folderMode: files.count > 1,
                    files: specs
                )
            )
            var session: UploadSession = try await model.request("/api/uploads", method: "POST", body: body)
            let total = max(session.totalSize, 1)
            for (index, file) in files.enumerated() {
                var offset = session.files[index].receivedSize
                while offset < specs[index].size {
                    status = "Uploading \(file.lastPathComponent)"
                    let length = min(Int64(session.chunkBytes), specs[index].size - offset)
                    let chunk = try await readChunk(file, offset: offset, count: Int(length))
                    guard !chunk.isEmpty else {
                        throw APIError(statusCode: 0, message: "The selected file ended before expected.")
                    }
                    session = try await model.uploadChunk(
                        path: "/api/uploads/\(session.id)/files/\(index)",
                        data: chunk,
                        offset: offset
                    )
                    offset += Int64(chunk.count)
                    progress = Double(session.receivedSize) / Double(total)
                }
            }
            status = "Submitting for review"
            let _: UploadFinalizeResponse = try await model.request(
                "/api/uploads/\(session.id)/finalize", method: "POST"
            )
            didSubmit()
        } catch { model.report(error) }
    }

    private func readChunk(_ url: URL, offset: Int64, count: Int) async throws -> Data {
        try await Task.detached(priority: .utility) {
            let handle = try FileHandle(forReadingFrom: url)
            defer { try? handle.close() }
            try handle.seek(toOffset: UInt64(offset))
            return try handle.read(upToCount: count) ?? Data()
        }.value
    }
}

private struct UploadManifestBody: Encodable {
    struct FileSpec: Encodable { let relativePath: String; let size: Int64 }
    let platform: String
    let bundleName: String
    let folderMode: Bool
    let files: [FileSpec]
}

private struct UploadFinalizeResponse: Decodable, Sendable {
    let submitted: Bool?
    let session: UploadSession?
    let jobId: Int?
}
