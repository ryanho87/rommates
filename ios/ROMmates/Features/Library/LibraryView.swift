import SwiftUI

enum GameSort: String, CaseIterable, Identifiable {
    case nameAscending = "name_asc"
    case nameDescending = "name_desc"
    case topRanked = "rank_asc"
    case ratingDescending = "rating_desc"
    case ratingAscending = "rating_asc"
    case sizeDescending = "size_desc"
    case sizeAscending = "size_asc"

    var id: String { rawValue }

    var title: String {
        switch self {
        case .nameAscending: return "Title A–Z"
        case .nameDescending: return "Title Z–A"
        case .topRanked: return "Top 100 rank"
        case .ratingDescending: return "Highest rated"
        case .ratingAscending: return "Lowest rated"
        case .sizeDescending: return "Largest size"
        case .sizeAscending: return "Smallest size"
        }
    }
}

struct LibraryView: View {
    @EnvironmentObject private var model: AppModel
    @State private var games: [Game] = []
    @State private var platforms: [PlatformSummary] = []
    @State private var total = 0
    @State private var search = ""
    @State private var platform = ""
    @State private var sort: GameSort = .nameAscending
    @State private var loading = true
    @State private var loadingMore = false
    @State private var error: String?

    private static let pageSize = 80
    private var queryID: String { "\(search)|\(platform)|\(sort.rawValue)" }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                LibraryCriteriaBar(
                    search: $search,
                    platform: $platform,
                    sort: $sort,
                    platforms: platforms,
                    total: total,
                    loading: loading
                )
                Divider()
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
                            message: search.isEmpty && platform.isEmpty
                                ? "Your indexed library will appear here."
                                : "Try a different search or platform."
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
            }
            .navigationTitle("Library")
            .navigationDestination(for: Game.self) { GameDetailView(game: $0) }
            .searchable(text: $search, prompt: "Search \(total.formatted()) games")
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
                    .init(name: "sort", value: sort.rawValue),
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

private struct LibraryCriteriaBar: View {
    @Binding var search: String
    @Binding var platform: String
    @Binding var sort: GameSort
    let platforms: [PlatformSummary]
    let total: Int
    let loading: Bool

    private var platformLabel: String {
        platform.isEmpty ? "All platforms" : platform.uppercased()
    }

    private var hasCustomCriteria: Bool {
        !search.isEmpty || !platform.isEmpty || sort != .nameAscending
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 9) {
                criteriaMenu(title: "FILTER BY", value: platformLabel, icon: "line.3.horizontal.decrease") {
                    Picker("Platform", selection: $platform) {
                        Text("All platforms").tag("")
                        ForEach(platforms) { item in
                            Text("\(item.platform.uppercased()) (\(item.count.formatted()))")
                                .tag(item.platform)
                        }
                    }
                }
                criteriaMenu(title: "SORT BY", value: sort.title, icon: "arrow.up.arrow.down") {
                    Picker("Sort games", selection: $sort) {
                        ForEach(GameSort.allCases) { option in
                            Text(option.title).tag(option)
                        }
                    }
                }
            }
            HStack(spacing: 8) {
                Text(loading && total == 0
                    ? "Loading games"
                    : "\(total.formatted()) games · \(platformLabel) · \(sort.title)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .accessibilityLabel(loading && total == 0
                        ? "Loading games"
                        : "\(total.formatted()) games, filtered by \(platformLabel), sorted by \(sort.title)")
                Spacer(minLength: 4)
                if hasCustomCriteria {
                    Button("Reset") {
                        search = ""
                        platform = ""
                        sort = .nameAscending
                    }
                    .font(.caption.weight(.semibold))
                }
            }
        }
        .padding(.horizontal, 16)
        .padding(.top, 8)
        .padding(.bottom, 10)
        .background(Color(.systemBackground))
    }

    private func criteriaMenu<Content: View>(
        title: String,
        value: String,
        icon: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        Menu(content: content) {
            HStack(spacing: 10) {
                Image(systemName: icon)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(ROMTheme.violet)
                    .frame(width: 20)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(.caption2.weight(.bold))
                        .tracking(0.6)
                        .foregroundStyle(.secondary)
                    Text(value)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                }
                Spacer(minLength: 4)
                Image(systemName: "chevron.down")
                    .font(.caption2.weight(.bold))
                    .foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 12)
            .frame(maxWidth: .infinity, minHeight: 48)
            .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 11))
            .overlay {
                RoundedRectangle(cornerRadius: 11)
                    .stroke(Color.primary.opacity(0.1), lineWidth: 0.5)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .frame(maxWidth: .infinity)
        .accessibilityLabel("\(title.lowercased()), \(value)")
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
                    PlatformBadge(platform: game.platform)
                    Text(ROMTheme.bytes(game.size))
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                GameRatingAndRankings(game: game)
                if let device = game.devices.first {
                    StatusLabel(
                        text: deviceLabel(device),
                        icon: deviceIcon(device.state),
                        color: deviceColor(device.state)
                    )
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

private struct GameRatingAndRankings: View {
    let game: Game

    var body: some View {
        HStack(spacing: 10) {
            if let rating = game.rating {
                Label {
                    Text("\(rating.formatted(.number.precision(.fractionLength(1))))/20")
                } icon: {
                    Image(systemName: "star.fill")
                }
                .accessibilityLabel("Community rating \(rating.formatted()) out of 20")
            } else {
                Label("Not rated", systemImage: "star")
            }
            if let rank = game.platformRank {
                Label("#\(rank) on \(game.platform.uppercased())", systemImage: "chart.bar.fill")
                    .accessibilityLabel("Ranked \(rank) on \(game.platform) by community rating")
            }
            if let rank = game.rawgRank {
                Text("#\(rank) Top 100")
                    .accessibilityLabel("Number \(rank) in the platform top 100")
            }
        }
        .font(.caption2.weight(.medium))
        .foregroundStyle(.secondary)
        .lineLimit(1)
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
                PlatformBadge(platform: game.platform)
                Text(ROMTheme.bytes(game.size))
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            GameRatingAndRankings(game: game)
        }
    }
}

private struct SelectionBody: Encodable { let gameId: Int; let selected: Bool }
private struct SelectionResponse: Decodable, Sendable { let selected: Bool }
