import SwiftUI

struct LibraryView: View {
    @EnvironmentObject private var model: AppModel
    @State private var games: [Game] = []
    @State private var platforms: [PlatformSummary] = []
    @State private var total = 0
    @State private var search = ""
    @State private var platform = ""
    @State private var sort = "name_asc"
    @State private var loading = true
    @State private var loadingMore = false
    @State private var error: String?

    private static let pageSize = 80
    private var queryID: String { "\(search)|\(platform)|\(sort)" }

    var body: some View {
        NavigationStack {
            Group {
                if loading && games.isEmpty {
                    List(0..<8, id: \.self) { _ in LibraryRowPlaceholder() }
                        .listStyle(.plain)
                } else if let error, games.isEmpty {
                    ContentUnavailableView {
                        Label("Library unavailable", systemImage: "exclamationmark.triangle")
                    } description: {
                        Text(error)
                    } actions: {
                        Button("Try Again") { Task { await load(reset: true, fresh: true) } }
                    }
                } else if games.isEmpty {
                    EmptyState(
                        icon: "books.vertical",
                        title: "No games found",
                        message: search.isEmpty ? "Your indexed library will appear here." : "Try a different search or platform."
                    )
                } else {
                    List {
                        ForEach(games) { game in
                            NavigationLink(value: game) { GameRow(game: game) }
                        }
                        if games.count < total {
                            HStack {
                                Spacer()
                                ProgressView()
                                Spacer()
                            }
                            .listRowSeparator(.hidden)
                            .task(id: games.count) { await loadMore() }
                        }
                    }
                    .listStyle(.plain)
                    .refreshable { await load(reset: true, fresh: true) }
                }
            }
            .navigationTitle("Library")
            .navigationDestination(for: Game.self) { GameDetailView(game: $0) }
            .searchable(text: $search, prompt: "Search \(total.formatted()) games")
            .toolbar {
                ToolbarItemGroup(placement: .topBarTrailing) {
                    Menu {
                        Picker("Platform", selection: $platform) {
                            Text("All platforms").tag("")
                            ForEach(platforms) { Text($0.platform).tag($0.platform) }
                        }
                    } label: {
                        Label(platform.isEmpty ? "Platform" : platform, systemImage: "line.3.horizontal.decrease.circle")
                    }
                    Menu {
                        Picker("Sort", selection: $sort) {
                            Text("Name").tag("name_asc")
                            Text("Top ranked").tag("rank_asc")
                            Text("Highest rated").tag("rating_desc")
                            Text("Largest first").tag("size_desc")
                        }
                    } label: { Label("Sort", systemImage: "arrow.up.arrow.down") }
                }
            }
            .task { await loadPlatforms() }
            .task(id: queryID) {
                if !search.isEmpty {
                    try? await Task.sleep(for: .milliseconds(250))
                    guard !Task.isCancelled else { return }
                }
                await load(reset: true)
            }
        }
    }

    private func loadPlatforms() async {
        do { platforms = try await model.request("/api/platforms") }
        catch where error.isRequestCancellation { }
        catch { model.report(error) }
    }

    private func load(reset: Bool, fresh: Bool = false) async {
        let requestedQuery = queryID
        let offset = reset ? 0 : games.count
        if reset { loading = true } else { loadingMore = true }
        defer {
            if reset { loading = false } else { loadingMore = false }
        }
        do {
            let response: GameList = try await model.request(
                "/api/games",
                query: [
                    .init(name: "search", value: search),
                    .init(name: "platform", value: platform),
                    .init(name: "sort", value: sort),
                    .init(name: "limit", value: String(Self.pageSize)),
                    .init(name: "offset", value: String(offset)),
                ],
                fresh: fresh
            )
            guard requestedQuery == queryID else { return }
            if reset {
                games = response.items
            } else {
                let existing = Set(games.map(\.id))
                games.append(contentsOf: response.items.filter { !existing.contains($0.id) })
            }
            total = response.total
            self.error = nil
        } catch where error.isRequestCancellation {
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func loadMore() async {
        guard !loading, !loadingMore, games.count < total else { return }
        await load(reset: false)
    }
}

private struct LibraryRowPlaceholder: View {
    var body: some View {
        HStack(spacing: 12) {
            RoundedRectangle(cornerRadius: 7)
                .fill(Color(.secondarySystemFill))
                .frame(width: 58, height: 58)
            VStack(alignment: .leading, spacing: 7) {
                Text("Game title placeholder")
                Text("platform · file size")
                    .font(.caption)
            }
            .redacted(reason: .placeholder)
        }
        .padding(.vertical, 4)
        .accessibilityHidden(true)
    }
}

private struct GameRow: View {
    let game: Game

    var body: some View {
        HStack(spacing: 12) {
            AuthenticatedArtwork(assetId: game.coverAssetId, version: game.coverAssetVersion)
                .frame(width: 58, height: 58)
                .clipped()
            VStack(alignment: .leading, spacing: 4) {
                Text(game.displayName)
                    .font(.body.weight(.medium))
                    .lineLimit(2)
                HStack(spacing: 6) {
                    Text(game.platform)
                    Text("·")
                    Text(ROMTheme.bytes(game.size))
                    if let rating = game.rating {
                        Text("·")
                        Label(rating.formatted(.number.precision(.fractionLength(1))), systemImage: "star.fill")
                            .labelStyle(.titleAndIcon)
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                if let device = game.devices.first {
                    StatusLabel(
                        text: deviceLabel(device),
                        icon: deviceIcon(device.state),
                        color: deviceColor(device.state)
                    )
                } else if let rank = game.rawgRank ?? game.platformRank {
                    StatusLabel(text: "#\(rank) on \(game.platform)", icon: "chart.bar.fill")
                }
            }
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .combine)
    }

    private func deviceLabel(_ device: DeviceState) -> String {
        switch device.state {
        case "synced", "present": return "On \(device.name)"
        case "pending_add": return "Add to \(device.name)"
        case "pending_remove": return "Remove from \(device.name)"
        default: return "\(device.name) · \(device.state.replacingOccurrences(of: "_", with: " "))"
        }
    }

    private func deviceIcon(_ state: String) -> String {
        state == "synced" || state == "present" ? "checkmark.circle.fill" : "clock.fill"
    }

    private func deviceColor(_ state: String) -> Color {
        state == "pending_remove" ? .orange : ROMTheme.violet
    }
}

struct AuthenticatedArtwork: View {
    @EnvironmentObject private var model: AppModel
    let assetId: Int?
    let version: String?
    @State private var image: UIImage?

    private var cacheKey: String? {
        guard let assetId else { return nil }
        return [model.baseURL?.absoluteString ?? "", String(assetId), version ?? "unversioned"]
            .joined(separator: ":")
    }

    var body: some View {
        GeometryReader { proxy in
            ZStack {
                RoundedRectangle(cornerRadius: 7)
                    .fill(Color(.secondarySystemFill))
                if let image {
                    Image(uiImage: image)
                        .resizable()
                        .scaledToFit()
                        .frame(width: proxy.size.width, height: proxy.size.height)
                } else {
                    Image(systemName: "gamecontroller.fill")
                        .foregroundStyle(.tertiary)
                }
            }
            .frame(width: proxy.size.width, height: proxy.size.height)
            .clipped()
            .clipShape(RoundedRectangle(cornerRadius: 7))
        }
        .task(id: cacheKey) {
            image = nil
            guard let assetId, let cacheKey else { return }
            if let cached = ArtworkMemoryCache.shared.image(for: cacheKey) {
                image = cached
                return
            }
            let suffix = version.map { "?v=\($0)" } ?? ""
            guard let data = try? await model.data(path: "/api/artwork/thumbnails/\(assetId)\(suffix)") else { return }
            guard !Task.isCancelled, let loaded = UIImage(data: data) else { return }
            ArtworkMemoryCache.shared.insert(loaded, for: cacheKey)
            image = loaded
        }
    }
}

@MainActor
private final class ArtworkMemoryCache {
    static let shared = ArtworkMemoryCache()

    private let cache: NSCache<NSString, UIImage> = {
        let cache = NSCache<NSString, UIImage>()
        cache.totalCostLimit = 48 * 1_024 * 1_024
        cache.countLimit = 500
        return cache
    }()

    func image(for key: String) -> UIImage? {
        cache.object(forKey: key as NSString)
    }

    func insert(_ image: UIImage, for key: String) {
        let decodedCost = (image.cgImage?.bytesPerRow ?? 0) * (image.cgImage?.height ?? 0)
        let cost = max(decodedCost, 1)
        cache.setObject(image, forKey: key as NSString, cost: cost)
    }
}

private struct GameDetailView: View {
    @EnvironmentObject private var model: AppModel
    let game: Game
    @State private var detail: GameDetail?
    @State private var downloading = false
    @State private var downloadedFile: URL?

    var body: some View {
        List {
            Section {
                GameDetailHeader(game: game)
                .padding(.vertical, 8)
            }
            if let metadata = detail?.artwork.metadata {
                Section("About") {
                    if let description = metadata.description, !description.isEmpty { Text(description) }
                    LabeledContent("Released", value: metadata.releaseDate ?? "Unknown")
                    if let developer = metadata.developer, !developer.isEmpty {
                        LabeledContent("Developer", value: developer)
                    }
                }
            }
            if let devices = detail?.devices, !devices.isEmpty {
                Section {
                    ForEach(devices) { device in
                        Toggle(device.name, isOn: Binding(
                            get: { device.selected != 0 },
                            set: { selected in Task { await select(device: device, selected: selected) } }
                        ))
                    }
                } header: {
                    Text("Devices")
                } footer: {
                    Text("Selections are staged until you apply the device changes.")
                }
            }
            Section("Files") {
                if let files = detail?.files {
                    ForEach(files) { file in
                        LabeledContent(file.relpath, value: ROMTheme.bytes(file.size))
                            .font(.caption)
                    }
                } else { ProgressView() }
            }
            Section {
                if let downloadedFile {
                    ShareLink(item: downloadedFile) {
                        Label("Share Download", systemImage: "square.and.arrow.up")
                    }
                } else {
                    Button(action: download) {
                        Label(downloading ? "Downloading…" : "Download ROM", systemImage: "arrow.down.circle")
                    }
                    .disabled(downloading)
                }
            }
        }
        .navigationTitle("Game")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
    }

    private func load() async {
        do { detail = try await model.request("/api/games/\(game.id)") }
        catch { model.report(error) }
    }

    private func select(device: GameDetailDevice, selected: Bool) async {
        do {
            let body = try JSONEncoder.rommates.encode(SelectionBody(gameId: game.id, selected: selected))
            let _: SelectionResponse = try await model.request(
                "/api/devices/\(device.id)/selection", method: "PUT", body: body
            )
            await load()
        } catch { model.report(error) }
    }

    private func download() {
        downloading = true
        Task {
            defer { downloading = false }
            do {
                let ticket: DownloadTicket = try await model.request(
                    "/api/games/\(game.id)/download-ticket", method: "POST"
                )
                guard let url = model.url(path: ticket.url) else {
                    throw APIError(statusCode: 0, message: "The download address is invalid.")
                }
                let (temporary, _) = try await URLSession.shared.download(from: url)
                let destination = FileManager.default.temporaryDirectory.appending(path: ticket.filename)
                try? FileManager.default.removeItem(at: destination)
                try FileManager.default.moveItem(at: temporary, to: destination)
                downloadedFile = destination
            } catch { model.report(error) }
        }
    }
}

private struct GameDetailHeader: View {
    let game: Game

    var body: some View {
        ViewThatFits(in: .horizontal) {
            HStack(alignment: .top, spacing: 16) {
                artwork
                metadata
                    .frame(minWidth: 150, maxWidth: .infinity, alignment: .leading)
                    .layoutPriority(1)
            }
            VStack(alignment: .leading, spacing: 14) {
                artwork
                metadata
            }
        }
    }

    private var artwork: some View {
        AuthenticatedArtwork(assetId: game.coverAssetId, version: game.coverAssetVersion)
            .frame(width: 104, height: 142)
            .clipped()
            .accessibilityLabel("Cover art for \(game.displayName)")
    }

    private var metadata: some View {
        VStack(alignment: .leading, spacing: 9) {
            Text(game.displayName)
                .font(.title3.bold())
                .lineLimit(3)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                Text(game.platform.uppercased())
                    .font(.caption2.weight(.semibold))
                    .padding(.horizontal, 7)
                    .padding(.vertical, 3)
                    .background(ROMTheme.softViolet, in: Capsule())
                    .foregroundStyle(ROMTheme.ink)
                Text(ROMTheme.bytes(game.size))
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            if let rating = game.rating {
                Label(rating.formatted(.number.precision(.fractionLength(1))), systemImage: "star.fill")
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(ROMTheme.violet)
            }
        }
    }
}

private struct SelectionBody: Encodable { let gameId: Int; let selected: Bool }
private struct SelectionResponse: Decodable, Sendable { let selected: Bool }
