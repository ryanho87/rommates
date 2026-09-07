import Foundation
import SwiftUI

struct DevicesView: View {
    @EnvironmentObject private var model: AppModel
    @State private var devices: [Device] = []
    @State private var groups: [DeviceGroup] = []
    @State private var loading = true
    @State private var error: String?
    @State private var groupsError: String?
    @State private var showingCreateDevice = false
    @State private var showingCreateGroup = false

    private var independentDevices: [Device] {
        devices.filter { $0.rosterGroupId == nil }
    }

    var body: some View {
        NavigationStack {
            Group {
                if loading && devices.isEmpty {
                    List(0..<5, id: \.self) { _ in DeviceRowPlaceholder() }
                } else if let error, devices.isEmpty {
                    ContentUnavailableView {
                        Label("Devices unavailable", systemImage: "exclamationmark.triangle")
                    } description: {
                        Text(error)
                    } actions: {
                        Button("Try Again") { Task { await load(fresh: true) } }
                    }
                } else if devices.isEmpty {
                    DevicesFirstUseView {
                        showingCreateDevice = true
                    }
                } else {
                    List {
                        if let groupsError {
                            Section {
                                LabeledContent {
                                    Button("Try Again") { Task { await load(fresh: true) } }
                                } label: {
                                    Label(groupsError, systemImage: "exclamationmark.triangle")
                                        .foregroundStyle(.secondary)
                                }
                            }
                        }
                        if !groups.isEmpty {
                            Section("Device groups") {
                                ForEach(groups) { group in
                                    NavigationLink(value: group) {
                                        DeviceGroupRow(group: group)
                                    }
                                }
                            }
                        }
                        if !independentDevices.isEmpty {
                            Section(groups.isEmpty ? "Your devices" : "Independent devices") {
                                ForEach(independentDevices) { device in
                                    NavigationLink(value: device) { DeviceRow(device: device) }
                                }
                            }
                        }
                    }
                    .refreshable { await load(fresh: true) }
                }
            }
            .navigationTitle("Devices")
            .navigationDestination(for: Device.self) { device in
                DeviceDetailView(device: device) {
                    Task { await load(fresh: true) }
                }
            }
            .navigationDestination(for: DeviceGroup.self) { group in
                let members = group.members.compactMap { member in
                    devices.first(where: { $0.id == member.id })
                }
                if let representative = members.first {
                    DeviceDetailView(device: representative, group: group, groupedDevices: members) {
                        Task { await load(fresh: true) }
                    }
                } else {
                    ContentUnavailableView(
                        "Group unavailable",
                        systemImage: "rectangle.3.group",
                        description: Text("Refresh Devices to load this group’s members.")
                    )
                }
            }
            .toolbar {
                if !devices.isEmpty {
                    Menu {
                        Button { showingCreateDevice = true } label: {
                            Label("New Device", systemImage: "gamecontroller")
                        }
                        Button { showingCreateGroup = true } label: {
                            Label("New Device Group", systemImage: "rectangle.3.group")
                        }
                    } label: {
                        Label("Add", systemImage: "plus")
                    }
                }
            }
            .sheet(isPresented: $showingCreateDevice) {
                CreateDeviceView {
                    showingCreateDevice = false
                    Task { await load(fresh: true) }
                }
            }
            .sheet(isPresented: $showingCreateGroup) {
                CreateDeviceGroupView(devices: independentDevices) {
                    showingCreateGroup = false
                    Task { await load(fresh: true) }
                }
            }
            .task { await load() }
        }
    }

    private func load(fresh: Bool = false) async {
        loading = true
        defer { loading = false }
        async let loadedDevices: [Device] = model.request("/api/devices", fresh: fresh)
        async let loadedGroups: [DeviceGroup] = model.request("/api/device-groups", fresh: fresh)
        do {
            devices = try await loadedDevices
            error = nil
        } catch {
            self.error = error.localizedDescription
            _ = try? await loadedGroups
            return
        }
        do {
            groups = try await loadedGroups
            groupsError = nil
        } catch {
            groupsError = "Device groups couldn’t load"
        }
    }
}

private struct DevicesFirstUseView: View {
    let createDevice: () -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                VStack(alignment: .leading, spacing: 9) {
                    Image(systemName: "gamecontroller.fill")
                        .font(.system(size: 28, weight: .semibold))
                        .foregroundStyle(ROMTheme.violet)
                        .frame(width: 52, height: 52)
                        .background(ROMTheme.violet.opacity(0.14), in: Circle())
                        .accessibilityHidden(true)
                    Text("Connect your first device")
                        .font(.title2.bold())
                    Text("Create a device to give one handheld its own ROM collection. Your full library stays on the ROMmates server.")
                        .font(.body)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }

                DeviceIntegrationDiagram()
                DeviceSetupChecklist()

                Button(action: createDevice) {
                    Label("Create First Device", systemImage: "plus")
                        .font(.headline)
                        .frame(maxWidth: .infinity)
                        .frame(minHeight: 48)
                }
                .buttonStyle(.borderedProminent)
                .tint(ROMTheme.violet)
                .accessibilityHint("Choose a device name, delivery method, and ROM capacity")
            }
            .frame(maxWidth: 560, alignment: .leading)
            .padding(.horizontal, 24)
            .padding(.top, 20)
            .padding(.bottom, 32)
            .frame(maxWidth: .infinity)
        }
        .background(Color(.systemGroupedBackground))
    }
}

private struct DeviceSetupChecklist: View {
    private let steps = [
        DeviceSetupStep(
            title: "Create the device",
            detail: "Choose its name, delivery method, and ROM capacity."
        ),
        DeviceSetupStep(
            title: "Choose its games",
            detail: "Switches stage the roster without changing any files."
        ),
        DeviceSetupStep(
            title: "Review and apply",
            detail: "ROMmates reconciles the device folder on the server."
        ),
        DeviceSetupStep(
            title: "Finish delivery",
            detail: "Pair Syncthing once, or download an ES-DE-ready ZIP."
        ),
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("WHAT YOU’LL DO")
                .font(.caption2.weight(.bold))
                .tracking(0.7)
                .foregroundStyle(.secondary)

            ForEach(Array(steps.enumerated()), id: \.offset) { index, step in
                HStack(alignment: .top, spacing: 12) {
                    Text("\(index + 1)")
                        .font(.caption.weight(.bold).monospacedDigit())
                        .foregroundStyle(ROMTheme.violet)
                        .frame(width: 28, height: 28)
                        .background(ROMTheme.violet.opacity(0.12), in: Circle())
                    VStack(alignment: .leading, spacing: 2) {
                        Text(step.title)
                            .font(.subheadline.weight(.semibold))
                        Text(step.detail)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("Step \(index + 1). \(step.title). \(step.detail)")
            }
        }
    }
}

private struct DeviceSetupStep {
    let title: String
    let detail: String
}

private struct DeviceIntegrationDiagram: View {
    var body: some View {
        VStack(spacing: 0) {
            Text("HOW IT WORKS")
                .font(.caption2.weight(.bold))
                .tracking(0.7)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.bottom, 16)

            DiagramEndpoint(
                icon: "books.vertical.fill",
                eyebrow: "SERVER",
                title: "Full ROM library",
                detail: "The canonical collection stays here."
            )

            DiagramConnector(label: "Choose ROMs")

            VStack(alignment: .leading, spacing: 12) {
                Text("ROMMATES")
                    .font(.caption2.weight(.bold))
                    .tracking(0.7)
                    .foregroundStyle(ROMTheme.violet)

                HStack(alignment: .center, spacing: 10) {
                    DiagramState(
                        icon: "checklist",
                        title: "Device roster",
                        detail: "Desired collection"
                    )

                    VStack(spacing: 4) {
                        Text("APPLY")
                            .font(.caption2.weight(.bold))
                            .tracking(0.45)
                            .foregroundStyle(.secondary)
                        Image(systemName: "arrow.right")
                            .font(.body.weight(.bold))
                            .foregroundStyle(ROMTheme.violet)
                    }
                    .accessibilityElement(children: .combine)

                    DiagramState(
                        icon: "folder.fill",
                        title: "Device folder",
                        detail: "Filesystem state"
                    )
                }
            }
            .padding(16)
            .background(ROMTheme.violet.opacity(0.10), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .stroke(ROMTheme.violet.opacity(0.24), lineWidth: 0.5)
            }

            DiagramConnector(label: "Syncthing or ZIP")

            DiagramEndpoint(
                icon: "gamecontroller.fill",
                eyebrow: "HANDHELD",
                title: "Your device",
                detail: "Receives the applied device folder."
            )
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("How device integration works")
    }
}

private struct DiagramEndpoint: View {
    let icon: String
    let eyebrow: String
    let title: String
    let detail: String

    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: icon)
                .font(.body.weight(.semibold))
                .foregroundStyle(ROMTheme.violet)
                .frame(width: 46, height: 46)
                .background(ROMTheme.violet.opacity(0.12), in: Circle())
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(eyebrow)
                    .font(.caption2.weight(.bold))
                    .tracking(0.6)
                    .foregroundStyle(.secondary)
                Text(title)
                    .font(.body.weight(.semibold))
                Text(detail)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}

private struct DiagramConnector: View {
    let label: String

    var body: some View {
        VStack(spacing: 3) {
            Rectangle()
                .fill(Color.secondary.opacity(0.30))
                .frame(width: 1, height: 12)
            Text(label.uppercased())
                .font(.caption2.weight(.bold))
                .tracking(0.55)
                .foregroundStyle(.secondary)
            Image(systemName: "chevron.down")
                .font(.caption2.weight(.bold))
                .foregroundStyle(.secondary)
        }
        .padding(.vertical, 5)
        .accessibilityElement(children: .combine)
    }
}

private struct DiagramState: View {
    let icon: String
    let title: String
    let detail: String

    var body: some View {
        VStack(spacing: 5) {
            Image(systemName: icon)
                .font(.title3.weight(.semibold))
                .foregroundStyle(ROMTheme.violet)
                .accessibilityHidden(true)
            Text(title)
                .font(.subheadline.weight(.semibold))
                .multilineTextAlignment(.center)
            Text(detail)
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .accessibilityElement(children: .combine)
    }
}

private struct DeviceRowPlaceholder: View {
    var body: some View {
        HStack(spacing: 14) {
            RoundedRectangle(cornerRadius: 8)
                .fill(Color(.secondarySystemFill))
                .frame(width: 34, height: 34)
            VStack(alignment: .leading, spacing: 7) {
                Text("Handheld name")
                Text("Selected games · on device").font(.caption)
            }
            .redacted(reason: .placeholder)
        }
        .padding(.vertical, 8)
        .accessibilityHidden(true)
    }
}

private struct DeviceGroupRow: View {
    let group: DeviceGroup

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "rectangle.3.group.fill")
                .foregroundStyle(ROMTheme.violet)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 3) {
                Text(group.name).font(.body.weight(.semibold))
                Text("\(group.deviceCount) devices · \(group.selectedGames) selected · one shared roster")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 4)
        .accessibilityHint("Opens the shared roster and storage for every device in this group")
    }
}

private struct DeviceRow: View {
    let device: Device

    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: device.deliveryMode == "syncthing" ? "arrow.triangle.2.circlepath" : "arrow.down.circle")
                .font(.title2)
                .foregroundStyle(ROMTheme.violet)
                .frame(width: 34)
            VStack(alignment: .leading, spacing: 5) {
                HStack {
                    Text(device.name).font(.body.weight(.semibold))
                    if let group = device.rosterGroupName, !group.isEmpty {
                        Text(group).font(.caption2).padding(.horizontal, 6).padding(.vertical, 2)
                            .background(ROMTheme.softViolet, in: Capsule()).foregroundStyle(ROMTheme.ink)
                    }
                }
                Text("\(device.selectedGames) selected · \(device.deployedGames) on device")
                    .font(.caption).foregroundStyle(.secondary)
                StatusLabel(
                    text: device.deliveryMode == "syncthing" ?
                        (device.syncthingReadyAt == nil ? "Waiting for Syncthing setup" : "Syncthing ready") :
                        "Manual download",
                    icon: device.syncthingReadyAt == nil && device.deliveryMode == "syncthing" ? "clock" : "checkmark.circle.fill"
                )
            }
        }
        .padding(.vertical, 5)
    }
}

private struct DeviceDetailView: View {
    @EnvironmentObject private var model: AppModel
    let device: Device
    let group: DeviceGroup?
    let groupedDevices: [Device]
    let didUpdate: () -> Void
    @State private var games: [Game] = []
    @State private var inventory: DeviceInventory?
    @State private var summary: DeviceSummary?
    @State private var memberSummaries: [Int: DeviceSummary] = [:]
    @State private var sync: DeviceSyncStatus?
    @State private var scope = "on_device"
    @State private var platform = ""
    @State private var sort: GameSort = .nameAscending
    @State private var totalGames = 0
    @State private var loadingGames = true
    @State private var hasLoadedGames = false
    @State private var didInitializePlatform = false
    @State private var didRefreshInventory = false
    @State private var applying = false
    @State private var reviewing = false
    @State private var previews: [DeviceChangePreview] = []
    @State private var showingChangeReview = false
    @State private var showingDownloadConfirmation = false
    @State private var downloading = false
    @State private var downloadStatus = ""
    @State private var downloadedFile: URL?
    @State private var showingSyncthingSetup = false

    init(
        device: Device,
        group: DeviceGroup? = nil,
        groupedDevices: [Device] = [],
        didUpdate: @escaping () -> Void
    ) {
        self.device = device
        self.group = group
        self.groupedDevices = groupedDevices
        self.didUpdate = didUpdate
        _scope = State(initialValue: group == nil ? "on_device" : "selected")
    }

    private var isGroup: Bool { group != nil }
    private var targetName: String { group?.name ?? device.name }
    private var targetDevices: [Device] { isGroup ? groupedDevices : [device] }

    private var selectedBytes: Int64 {
        summary?.desiredRomBytes
            ?? inventory?.selectedPlatforms.reduce(0) { $0 + ($1.bytes ?? 0) }
            ?? games.filter { $0.selected != 0 }.reduce(0) { $0 + $1.size }
    }
    private var currentBytes: Int64 { summary?.currentRomBytes ?? inventory?.bytes ?? 0 }
    private var projectedBytes: Int64 { summary?.projectedRomBytes ?? selectedBytes }
    private var capacityBytes: Int64 { summary?.storageCapacityBytes ?? device.storageCapacityBytes }
    private var selectedGameCount: Int { summary?.games ?? device.selectedGames }
    private var changes: Int {
        if !memberSummaries.isEmpty {
            return targetDevices.reduce(0) { total, member in
                guard let summary = memberSummaries[member.id] else { return total }
                return total + summary.additions + summary.removals
            }
        }
        return inventory?.changes ?? 0
    }
    private var exceedsCapacity: Bool {
        if isGroup {
            return targetDevices.contains { member in
                memberSummaries[member.id]?.overCapacity == true
            }
        }
        return changes > 0 && capacityBytes > 0 && projectedBytes > capacityBytes
    }
    private var reviewButtonTitle: String {
        if reviewing { return "Inspecting Changes…" }
        return changes == 1 ? "Review 1 Change" : "Review \(changes.formatted()) Changes"
    }
    private var reviewButtonTint: Color {
        if exceedsCapacity { return ROMTheme.danger }
        return ROMTheme.violet
    }
    private var queryID: String { "\(scope)|\(platform)|\(sort.rawValue)" }
    private var platformOptions: [String] {
        var values = Set(inventory?.platforms.map(\.platform) ?? [])
        values.formUnion(inventory?.presentPlatforms.map(\.platform) ?? [])
        values.formUnion(inventory?.selectedPlatforms.map(\.platform) ?? [])
        if !platform.isEmpty { values.insert(platform) }
        return values.sorted { $0.localizedCaseInsensitiveCompare($1) == .orderedAscending }
    }
    private var platformMetrics: [DeviceInventory.Platform] {
        let values = isGroup ? inventory?.selectedPlatforms ?? [] : inventory?.presentPlatforms ?? []
        return values.sorted {
            $0.platform.localizedCaseInsensitiveCompare($1.platform) == .orderedAscending
        }
    }
    private var syncthingIsReady: Bool {
        sync?.linked ?? (device.syncthingReadyAt != nil)
    }

    var body: some View {
        List {
            Section {
                VStack(alignment: .leading, spacing: 12) {
                    HStack {
                        VStack(alignment: .leading, spacing: 3) {
                            Text(isGroup ? "Shared roster" : (device.deliveryMode == "syncthing" ? "Syncthing delivery" : "Manual download"))
                                .font(.headline)
                            if isGroup {
                                StatusLabel(
                                    text: "Selections stay matched across \(targetDevices.count.formatted()) devices",
                                    icon: "rectangle.3.group.fill",
                                    color: ROMTheme.violet
                                )
                            } else {
                                StatusLabel(
                                    text: syncDetail,
                                    icon: syncIcon,
                                    color: syncColor
                                )
                            }
                        }
                        Spacer()
                        if !isGroup, let run = sync?.syncRun, ["pending", "syncing", "offline"].contains(run.state) {
                            Text(run.completion / 100, format: .percent.precision(.fractionLength(0)))
                                .font(.headline.monospacedDigit())
                        }
                    }
                    if !isGroup, let run = sync?.syncRun, ["pending", "syncing", "offline"].contains(run.state) {
                        ProgressView(value: run.completion, total: 100)
                    }
                    if !isGroup, capacityBytes > 0 {
                        ProgressView(
                            value: min(Double(currentBytes), Double(capacityBytes)),
                            total: Double(capacityBytes)
                        )
                        .tint(summary?.overCapacity == true ? ROMTheme.danger : ROMTheme.violet)
                        HStack(alignment: .firstTextBaseline) {
                            Text("\(ROMTheme.bytes(currentBytes)) used")
                                .font(.subheadline.weight(.semibold))
                            Spacer()
                            Text("of \(ROMTheme.bytes(capacityBytes))")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    } else if !isGroup {
                        Text("\(ROMTheme.bytes(currentBytes)) on device")
                            .font(.subheadline.weight(.semibold))
                    }
                    Text("\(selectedGameCount.formatted()) selected ROMs · \(ROMTheme.bytes(selectedBytes)) in \(isGroup ? "shared roster" : "roster")")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    if changes > 0 {
                        StatusLabel(
                            text: "\(ROMTheme.bytes(projectedBytes)) after \(changes.formatted()) staged \(changes == 1 ? "change" : "changes")",
                            icon: "clock.fill",
                            color: ROMTheme.warning
                        )
                    }
                    if !isGroup, let unrecognized = summary?.unrecognizedRomBytes, unrecognized > 0 {
                        StatusLabel(
                            text: "\(ROMTheme.bytes(unrecognized)) not matched to the library",
                            icon: "questionmark.circle.fill",
                            color: ROMTheme.warning
                        )
                    }
                    if !platformMetrics.isEmpty {
                        Divider()
                        Text(isGroup ? "SHARED ROSTER BY PLATFORM" : "ON DEVICE BY PLATFORM")
                            .font(.caption2.weight(.bold))
                            .tracking(0.7)
                            .foregroundStyle(.secondary)
                        ScrollView(.horizontal, showsIndicators: false) {
                            HStack(spacing: 8) {
                                ForEach(platformMetrics, id: \.platform) { metric in
                                    DevicePlatformMetric(metric: metric)
                                }
                            }
                        }
                    }
                    if isGroup {
                        Divider()
                        Text("STORAGE BY DEVICE")
                            .font(.caption2.weight(.bold))
                            .tracking(0.7)
                            .foregroundStyle(.secondary)
                        ForEach(targetDevices) { member in
                            DeviceGroupStorageRow(
                                device: member,
                                summary: memberSummaries[member.id]
                            )
                        }
                    }
                    if !isGroup, device.deliveryMode == "syncthing" {
                        if !syncthingIsReady {
                            Divider()
                            Button {
                                showingSyncthingSetup = true
                            } label: {
                                Text("Set Up Syncthing")
                                    .font(.headline)
                                    .frame(maxWidth: .infinity, minHeight: 44, alignment: .center)
                            }
                            .buttonStyle(.borderedProminent)
                            .tint(ROMTheme.violet)
                            .disabled(sync?.configured == false)
                        }
                        if sync?.configured == false {
                            StatusLabel(
                                text: "Syncthing is unavailable on the ROMmates server",
                                icon: "exclamationmark.triangle.fill",
                                color: ROMTheme.warning
                            )
                        }
                    }
                }
                .padding(.vertical, 4)
            }
            if changes > 0 {
                Section {
                    Button {
                        Task { await reviewChanges() }
                    } label: {
                        HStack(spacing: 8) {
                            if reviewing {
                                ProgressView()
                                    .controlSize(.small)
                                    .tint(.white)
                            } else {
                                Image(systemName: exceedsCapacity ? "exclamationmark.triangle.fill" : "list.bullet.rectangle")
                            }
                            Text(reviewButtonTitle)
                        }
                        .font(.headline)
                        .frame(maxWidth: .infinity, minHeight: 44, alignment: .center)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(reviewButtonTint)
                    .disabled(reviewing || applying)
                } footer: {
                    if exceedsCapacity {
                        Text(isGroup
                            ? "At least one device is over capacity. Review the plan to see which one."
                            : "The staged roster is \(ROMTheme.bytes(projectedBytes - capacityBytes)) over this device’s capacity.")
                            .foregroundStyle(ROMTheme.danger)
                    }
                }
            }
            Section {
                Picker("View", selection: $scope) {
                    Text("Changes").tag("changes")
                    Text(isGroup ? "Roster" : "On device").tag(isGroup ? "selected" : "on_device")
                    Text("Library").tag("all")
                }
                .pickerStyle(.segmented)
                DeviceCriteriaBar(
                    platform: $platform,
                    sort: $sort,
                    platforms: platformOptions,
                    counts: Dictionary(
                        uniqueKeysWithValues: (inventory?.platforms ?? []).map { ($0.platform, $0.count) }
                    ),
                    total: totalGames,
                    loading: loadingGames
                )
                if loadingGames && games.isEmpty {
                    ForEach(0..<6, id: \.self) { _ in DeviceGamePlaceholder() }
                } else if games.isEmpty {
                    DeviceGamesEmptyState(scope: scope, platform: platform)
                } else {
                    ForEach(games) { game in
                        Toggle(isOn: Binding(
                            get: { game.selected != 0 },
                            set: { selected in Task { await select(game, selected: selected) } }
                        )) {
                            HStack(spacing: 12) {
                                AuthenticatedArtwork(
                                    assetId: game.coverAssetId,
                                    version: game.coverAssetVersion
                                )
                                .frame(width: 52, height: 52)
                                .accessibilityHidden(true)

                                VStack(alignment: .leading, spacing: 5) {
                                    Text(game.displayName)
                                        .lineLimit(2)
                                    HStack(spacing: 7) {
                                        PlatformBadge(platform: game.platform)
                                        Text(ROMTheme.bytes(game.size))
                                        if let state = game.deviceState, state != "on_device" {
                                            DeviceStateBadge(state: state)
                                        }
                                    }
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                }
                                .layoutPriority(1)
                            }
                            .padding(.vertical, 3)
                        }
                    }
                }
            }
            if model.permissions?.download == true {
                Section {
                    if let downloadedFile {
                        ShareLink(item: downloadedFile) {
                            Label("Share Downloaded ROM Package", systemImage: "square.and.arrow.up")
                        }
                    }
                    Button {
                        showingDownloadConfirmation = true
                    } label: {
                        Label(
                            downloading ? (downloadStatus.isEmpty ? "Preparing ROM Package…" : downloadStatus) : "Download Selected ROMs",
                            systemImage: "arrow.down.circle"
                        )
                    }
                    .disabled(downloading || selectedGameCount == 0)
                } header: {
                    Text("ROM package")
                } footer: {
                    Text("Creates one ZIP with ES-DE platform folders for this device’s selected roster.")
                }
            }
        }
        .navigationTitle(targetName)
        .navigationBarTitleDisplayMode(.inline)
        .refreshable {
            didRefreshInventory = true
            await load(refreshDeviceInventory: true)
            await loadSync()
        }
        .confirmationDialog(
            "Download \(targetName)’s selected ROMs?",
            isPresented: $showingDownloadConfirmation,
            titleVisibility: .visible
        ) {
            Button("Prepare Download") { Task { await downloadSelectedROMs() } }
            Button("Cancel", role: .cancel) { }
        } message: {
            Text("ROMmates will validate \(selectedGameCount.formatted()) games and prepare a \(ROMTheme.bytes(selectedBytes)) ZIP. Keep the app open while this large download completes.")
        }
        .sheet(isPresented: $showingChangeReview) {
            DeviceChangeReviewView(
                targetName: targetName,
                devices: targetDevices,
                previews: previews,
                applyChanges: apply,
                discardChanges: discard
            )
        }
        .sheet(isPresented: $showingSyncthingSetup) {
            SyncthingSetupView(device: device) {
                Task {
                    await loadSync()
                    didUpdate()
                }
            }
        }
        .task(id: queryID) { await load() }
        .task(id: hasLoadedGames) {
            guard hasLoadedGames, !didRefreshInventory else { return }
            didRefreshInventory = true
            await load(refreshDeviceInventory: true)
        }
        .task {
            while !Task.isCancelled {
                await loadSync()
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }

    private var syncDetail: String {
        if let run = sync?.syncRun { return run.detail }
        return sync?.status ?? (device.deliveryMode == "syncthing" ? "Checking delivery status…" : "Build a package after applying changes.")
    }

    private var syncColor: Color {
        if let run = sync?.syncRun, ["pending", "syncing", "offline"].contains(run.state) {
            return ROMTheme.warning
        }
        return sync?.status == "Up to date" ? ROMTheme.success : .secondary
    }

    private var syncIcon: String {
        sync?.status == "Up to date" ? "checkmark.circle.fill" : "clock.fill"
    }

    private func load(refreshDeviceInventory: Bool = false) async {
        let requestedScope = scope
        let requestedPlatform = platform
        let requestedSort = sort
        if games.isEmpty && !refreshDeviceInventory { loadingGames = true }
        defer {
            if requestedScope == scope && requestedPlatform == platform && requestedSort == sort {
                loadingGames = false
            }
        }
        do {
            let response: GameList = try await model.request(
                "/api/games",
                query: [
                    .init(name: "device_id", value: String(device.id)),
                    .init(name: "device_scope", value: requestedScope),
                    .init(name: "platform", value: requestedPlatform),
                    .init(name: "sort", value: requestedSort.rawValue),
                    .init(name: "refresh_device_inventory", value: String(refreshDeviceInventory)),
                    .init(name: "limit", value: "500"),
                ],
                fresh: refreshDeviceInventory
            )
            guard requestedScope == scope, requestedPlatform == platform, requestedSort == sort else { return }
            games = response.items
            totalGames = response.total
            inventory = response.deviceInventory
            if !didInitializePlatform {
                didInitializePlatform = true
                if requestedPlatform.isEmpty, let first = platformOptions.first {
                    platform = first
                    return
                }
            }
            hasLoadedGames = true
            if refreshDeviceInventory, isGroup {
                await refreshOtherGroupInventories()
            }
            await loadSummaries()
        } catch let error as URLError where error.code == .cancelled {
        } catch is CancellationError {
        } catch { model.report(error) }
    }

    private func loadSync() async {
        guard !isGroup else { return }
        do { sync = try await model.request("/api/devices/\(device.id)/sync-status", fresh: true) }
        catch { /* Refresh remains available even when Syncthing is offline. */ }
    }

    private func loadSummaries() async {
        var loaded: [Int: DeviceSummary] = [:]
        if let group {
            do {
                let response: DeviceGroupSummaryResponse = try await model.request(
                    "/api/device-groups/\(group.id)/summary", fresh: true
                )
                loaded = Dictionary(
                    uniqueKeysWithValues: response.members.map { ($0.deviceId, $0.summary) }
                )
                memberSummaries = loaded
                summary = loaded[device.id]
                return
            } catch let error as URLError where error.code == .cancelled {
                return
            } catch is CancellationError {
                return
            } catch {
                // Fall back to per-device summaries for servers predating this endpoint.
            }
        }
        for member in targetDevices {
            do {
                let value: DeviceSummary = try await model.request(
                    "/api/devices/\(member.id)/summary", fresh: true
                )
                loaded[member.id] = value
            } catch let error as URLError where error.code == .cancelled {
                return
            } catch is CancellationError {
                return
            } catch {
                // The game response still carries safe roster fallbacks.
            }
        }
        memberSummaries = loaded
        summary = loaded[device.id]
    }

    private func refreshOtherGroupInventories() async {
        for member in targetDevices where member.id != device.id {
            do {
                let _: GameList = try await model.request(
                    "/api/games",
                    query: [
                        .init(name: "device_id", value: String(member.id)),
                        .init(name: "device_scope", value: "selected"),
                        .init(name: "refresh_device_inventory", value: "true"),
                        .init(name: "limit", value: "1"),
                    ],
                    fresh: true
                )
            } catch let error as URLError where error.code == .cancelled {
                return
            } catch is CancellationError {
                return
            } catch {
                model.report(error, prefix: member.name)
            }
        }
    }

    private func select(_ game: Game, selected: Bool) async {
        do {
            let body = try JSONEncoder.rommates.encode(DeviceSelectionBody(gameId: game.id, selected: selected))
            let _: DeviceSelectionResponse = try await model.request(
                isGroup ? "/api/device-groups/\(group!.id)/selection" : "/api/devices/\(device.id)/selection",
                method: "PUT",
                body: body
            )
            await load()
        } catch let error as URLError where error.code == .cancelled {
        } catch is CancellationError {
        } catch { model.report(error) }
    }

    private func reviewChanges() async {
        reviewing = true
        defer { reviewing = false }
        var loaded: [DeviceChangePreview] = []
        do {
            if let group {
                let response: DeviceGroupPreviewResponse = try await model.request(
                    "/api/device-groups/\(group.id)/preview", fresh: true
                )
                let byDevice = Dictionary(
                    uniqueKeysWithValues: response.members.map { ($0.deviceId, $0.preview) }
                )
                loaded = targetDevices.compactMap { byDevice[$0.id] }
            } else {
                let preview: DeviceChangePreview = try await model.request(
                    "/api/devices/\(device.id)/preview", fresh: true
                )
                loaded = [preview]
            }
            previews = loaded
            showingChangeReview = true
        } catch { model.report(error, prefix: "Change review") }
    }

    private func apply() async -> Bool {
        applying = true
        defer { applying = false }
        do {
            if let group {
                let _: DeviceGroupApplyResponse = try await model.request(
                    "/api/device-groups/\(group.id)/apply", method: "POST"
                )
            } else {
                let _: JobReference = try await model.request(
                    "/api/devices/\(device.id)/apply", method: "POST"
                )
            }
            await load()
            await loadSync()
            didUpdate()
            return true
        } catch {
            model.report(error)
            return false
        }
    }

    private func discard() async -> Bool {
        do {
            let _: DiscardResponse = try await model.request(
                isGroup ? "/api/device-groups/\(group!.id)/discard-changes" : "/api/devices/\(device.id)/discard-changes",
                method: "POST"
            )
            await load()
            didUpdate()
            return true
        } catch {
            model.report(error)
            return false
        }
    }

    private func downloadSelectedROMs() async {
        downloading = true
        downloadedFile = nil
        downloadStatus = "Preparing package…"
        defer {
            downloading = false
            downloadStatus = ""
        }
        do {
            let reference: JobReference = try await model.request(
                "/api/devices/\(device.id)/export-ticket", method: "POST"
            )
            let ticket = try await waitForExport(jobId: reference.jobId)
            downloadStatus = "Downloading \(ROMTheme.bytes(ticket.bytes))…"
            guard let url = model.url(path: ticket.url) else {
                throw APIError(statusCode: 0, message: "The download address is invalid.")
            }
            let (temporary, _) = try await URLSession.shared.download(from: url)
            let destination = FileManager.default.temporaryDirectory.appending(path: ticket.filename)
            try? FileManager.default.removeItem(at: destination)
            try FileManager.default.moveItem(at: temporary, to: destination)
            downloadedFile = destination
        } catch {
            model.report(error, prefix: "ROM package")
        }
    }

    private func waitForExport(jobId: Int) async throws -> DeviceExportTicket {
        let deadline = Date().addingTimeInterval(30 * 60)
        while Date() < deadline {
            try Task.checkCancellation()
            let job: DeviceExportJob = try await model.request(
                "/api/jobs/\(jobId)", fresh: true
            )
            switch job.status {
            case "complete":
                guard let ticket = job.result else {
                    throw APIError(statusCode: 0, message: "The completed package had no download ticket.")
                }
                return ticket
            case "failed", "cancelled":
                throw APIError(statusCode: 0, message: job.detail)
            default:
                downloadStatus = job.detail.isEmpty ? "Preparing package…" : job.detail
                try await Task.sleep(for: .milliseconds(700))
            }
        }
        throw APIError(statusCode: 0, message: "The package is still preparing. Try again shortly.")
    }
}

private struct DeviceGroupStorageRow: View {
    let device: Device
    let summary: DeviceSummary?

    private var currentBytes: Int64 { summary?.currentRomBytes ?? 0 }
    private var capacityBytes: Int64 { summary?.storageCapacityBytes ?? device.storageCapacityBytes }
    private var projectedBytes: Int64 { summary?.projectedRomBytes ?? currentBytes }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text(device.name)
                    .font(.subheadline.weight(.semibold))
                    .lineLimit(1)
                Spacer(minLength: 12)
                if summary == nil {
                    Text("Loading storage…")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else if capacityBytes > 0 {
                    Text("\(ROMTheme.bytes(currentBytes)) of \(ROMTheme.bytes(capacityBytes))")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                } else {
                    Text(ROMTheme.bytes(currentBytes))
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
            }
            if let summary, capacityBytes > 0 {
                ProgressView(
                    value: min(Double(currentBytes), Double(capacityBytes)),
                    total: Double(capacityBytes)
                )
                .tint(summary.overCapacity ? ROMTheme.danger : ROMTheme.violet)
            }
            if let summary, projectedBytes != currentBytes {
                Text("\(ROMTheme.bytes(projectedBytes)) after staged changes")
                    .font(.caption)
                    .foregroundStyle(summary.overCapacity ? ROMTheme.danger : ROMTheme.warning)
            }
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .combine)
    }
}

private struct DeviceChangeReviewView: View {
    @Environment(\.dismiss) private var dismiss
    let targetName: String
    let devices: [Device]
    let previews: [DeviceChangePreview]
    let applyChanges: () async -> Bool
    let discardChanges: () async -> Bool
    @State private var busy = false

    private var additions: Int { previews.reduce(0) { $0 + $1.additions } }
    private var removals: Int { previews.reduce(0) { $0 + $1.removals } }
    private var conversions: Int { previews.reduce(0) { $0 + $1.conversions } }
    private var overCapacity: Bool { previews.contains(where: \.overCapacity) }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    VStack(alignment: .leading, spacing: 10) {
                        Text(devices.count > 1
                            ? "This plan reconciles every device in \(targetName) together."
                            : "Review what ROMmates will change on \(targetName).")
                            .font(.subheadline)
                        HStack(spacing: 14) {
                            ChangeCount(value: additions, label: "add", color: ROMTheme.success)
                            ChangeCount(value: removals, label: "remove", color: ROMTheme.danger)
                            if conversions > 0 {
                                ChangeCount(value: conversions, label: "optimize", color: ROMTheme.violet)
                            }
                        }
                        if overCapacity {
                            StatusLabel(
                                text: "At least one device needs more free space",
                                icon: "exclamationmark.triangle.fill",
                                color: ROMTheme.danger
                            )
                        }
                    }
                    .padding(.vertical, 4)
                }

                ForEach(Array(devices.enumerated()), id: \.element.id) { index, device in
                    if previews.indices.contains(index) {
                        DevicePlanSection(device: device, preview: previews[index])
                    }
                }

                Section {
                    Button {
                        Task { await perform(applyChanges) }
                    } label: {
                        Label(
                            busy ? "Working…" : (devices.count > 1 ? "Apply to All Devices" : "Apply Changes"),
                            systemImage: "arrow.triangle.2.circlepath"
                        )
                        .font(.headline)
                        .frame(maxWidth: .infinity, minHeight: 44)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(ROMTheme.violet)
                    .disabled(busy || overCapacity)

                    Button("Discard Staged Changes", role: .destructive) {
                        Task { await perform(discardChanges) }
                    }
                    .frame(maxWidth: .infinity, alignment: .center)
                    .disabled(busy)
                } footer: {
                    Text("Discarding changes updates the desired roster only; it does not remove files from a device.")
                }
            }
            .navigationTitle("Review Changes")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Keep Editing") { dismiss() }
                        .disabled(busy)
                }
            }
        }
    }

    private func perform(_ action: () async -> Bool) async {
        busy = true
        let succeeded = await action()
        busy = false
        if succeeded { dismiss() }
    }
}

private struct ChangeCount: View {
    let value: Int
    let label: String
    let color: Color

    var body: some View {
        Label("\(value.formatted()) \(label)", systemImage: value == 0 ? "minus" : "circle.fill")
            .font(.caption.weight(.semibold))
            .foregroundStyle(value == 0 ? Color.secondary : color)
            .accessibilityLabel("\(value) files to \(label)")
    }
}

private struct DevicePlanSection: View {
    let device: Device
    let preview: DeviceChangePreview

    var body: some View {
        Section {
            LabeledContent("Storage now", value: ROMTheme.bytes(preview.currentRomBytes))
            LabeledContent("After changes") {
                Text(ROMTheme.bytes(preview.projectedRomBytes))
                    .foregroundStyle(preview.overCapacity ? ROMTheme.danger : .primary)
            }
            if preview.storageCapacityBytes > 0 {
                LabeledContent("Capacity", value: ROMTheme.bytes(preview.storageCapacityBytes))
            }
            ChangeItemsDisclosure(
                title: "Additions",
                items: preview.changes.additions,
                icon: "plus.circle.fill",
                color: ROMTheme.success
            )
            ChangeItemsDisclosure(
                title: "Removals",
                items: preview.changes.removals,
                icon: "minus.circle.fill",
                color: ROMTheme.danger
            )
            ChangeItemsDisclosure(
                title: "Storage optimizations",
                items: preview.changes.conversions,
                icon: "link.circle.fill",
                color: ROMTheme.violet
            )
        } header: {
            Text(device.name)
        }
    }
}

private struct ChangeItemsDisclosure: View {
    let title: String
    let items: [DeviceChangeItem]
    let icon: String
    let color: Color

    var body: some View {
        if !items.isEmpty {
            DisclosureGroup {
                ForEach(items) { item in
                    VStack(alignment: .leading, spacing: 3) {
                        Text(item.displayName)
                            .font(.subheadline)
                            .lineLimit(2)
                        HStack(spacing: 7) {
                            PlatformBadge(platform: item.platform)
                            Text("\(item.files.formatted()) \(item.files == 1 ? "file" : "files")")
                            if let bytes = item.bytes {
                                Text(ROMTheme.bytes(bytes))
                            }
                        }
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 3)
                }
            } label: {
                Label("\(title) (\(items.count.formatted()))", systemImage: icon)
                    .foregroundStyle(color)
            }
        }
    }
}

private struct DeviceCriteriaBar: View {
    @Binding var platform: String
    @Binding var sort: GameSort
    let platforms: [String]
    let counts: [String: Int]
    let total: Int
    let loading: Bool

    private var platformLabel: String {
        platform.isEmpty ? "All platforms" : platform.uppercased()
    }

    private var defaultPlatform: String {
        platforms.first ?? ""
    }

    private var hasCustomCriteria: Bool {
        platform != defaultPlatform || sort != .nameAscending
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 9) {
                criteriaMenu(
                    title: "FILTER BY",
                    value: platformLabel,
                    icon: "line.3.horizontal.decrease"
                ) {
                    Picker("Platform", selection: $platform) {
                        Text("All platforms").tag("")
                        ForEach(platforms, id: \.self) { value in
                            if let count = counts[value] {
                                Text("\(value.uppercased()) (\(count.formatted()))").tag(value)
                            } else {
                                Text(value.uppercased()).tag(value)
                            }
                        }
                    }
                }
                criteriaMenu(
                    title: "SORT BY",
                    value: sort.title,
                    icon: "arrow.up.arrow.down"
                ) {
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
                        platform = defaultPlatform
                        sort = .nameAscending
                    }
                    .font(.caption.weight(.semibold))
                }
            }
        }
        .padding(.vertical, 4)
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

private struct DevicePlatformMetric: View {
    let metric: DeviceInventory.Platform

    var body: some View {
        HStack(spacing: 8) {
            PlatformBadge(platform: metric.platform)
            Text("\(metric.count.formatted()) · \(ROMTheme.bytes(metric.bytes ?? 0))")
                .font(.caption2.weight(.medium))
                .foregroundStyle(.secondary)
        }
        .padding(.leading, 6)
        .padding(.trailing, 10)
        .padding(.vertical, 5)
        .background(Color(.tertiarySystemBackground), in: Capsule())
        .overlay {
            Capsule().stroke(Color.primary.opacity(0.09), lineWidth: 0.5)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(metric.platform), \(metric.count) ROMs, \(ROMTheme.bytes(metric.bytes ?? 0))")
    }
}

private struct DeviceGamePlaceholder: View {
    var body: some View {
        Toggle(isOn: .constant(false)) {
            HStack(spacing: 12) {
                RoundedRectangle(cornerRadius: 7)
                    .fill(Color(.secondarySystemFill))
                    .frame(width: 52, height: 52)
                VStack(alignment: .leading, spacing: 5) {
                    Text("Game title placeholder").font(.body)
                    Text("platform · file size · device state").font(.caption)
                }
            }
            .redacted(reason: .placeholder)
            .padding(.vertical, 3)
        }
        .disabled(true)
        .accessibilityHidden(true)
    }
}

private struct DeviceGamesEmptyState: View {
    let scope: String
    let platform: String

    var body: some View {
        ContentUnavailableView {
            Label(title, systemImage: "gamecontroller")
        } description: {
            Text(message)
        }
    }

    private var title: String {
        if !platform.isEmpty { return "No \(platform) games" }
        switch scope {
        case "changes": return "No staged changes"
        case "on_device": return "No games on this device"
        case "selected": return "No games in this roster"
        default: return "No games available"
        }
    }

    private var message: String {
        if !platform.isEmpty { return "Choose another platform or clear the filter." }
        switch scope {
        case "changes": return "Your staged collection matches the device."
        case "on_device": return "Use Library to choose games for this device."
        case "selected": return "Use Library to choose games for every device in this group."
        default: return "The indexed library will appear here."
        }
    }
}

private struct SyncthingSetupView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let device: Device
    let didComplete: () -> Void
    @State private var syncthingDeviceId = ""
    @State private var busy = false
    @State private var result: SyncthingShareResult?

    private var normalizedId: String {
        syncthingDeviceId.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    var body: some View {
        NavigationStack {
            Form {
                if let result {
                    Section {
                        Label("Syncthing share ready", systemImage: "checkmark.circle.fill")
                            .font(.headline)
                            .foregroundStyle(ROMTheme.success)
                        LabeledContent("Device") {
                            Text(device.name)
                        }
                        LabeledContent("Syncthing ID") {
                            Text(result.deviceId)
                                .font(.caption.monospaced())
                                .multilineTextAlignment(.trailing)
                                .textSelection(.enabled)
                        }
                        LabeledContent("Folder") {
                            Text(result.folderId)
                                .font(.caption.monospaced())
                                .textSelection(.enabled)
                        }
                    } footer: {
                        Text(result.created
                            ? "ROMmates created the folder, shared it with the handheld, and requested a scan. Accept the folder on the handheld if Syncthing asks."
                            : "ROMmates reused the existing folder, confirmed the share, and requested a scan.")
                    }
                } else {
                    Section {
                        SyncthingPrerequisite(
                            icon: "arrow.down.app",
                            title: "Install and open Syncthing",
                            detail: "Use Syncthing or a compatible client. Open it once and complete any storage-permission prompts."
                        )
                        SyncthingPrerequisite(
                            icon: "externaldrive",
                            title: "Prepare ROM storage",
                            detail: "Choose a writable location with enough free space. You’ll select the local ROM folder when accepting the share."
                        )
                        SyncthingPrerequisite(
                            icon: "wifi",
                            title: "Allow syncing to stay active",
                            detail: "Keep the handheld online and Syncthing running. Allow local-network and background access; on Android, disable battery optimization during the first sync."
                        )
                        SyncthingPrerequisite(
                            icon: "qrcode",
                            title: "Copy this handheld’s Device ID",
                            detail: "In Syncthing, open Actions, then Show ID. Copy the handheld’s ID, not the NUC’s ID."
                        )
                    } header: {
                        Text("Prerequisites")
                    } footer: {
                        Text("You do not need to add the NUC manually. After creating the share, return to Syncthing on the handheld, accept the NUC as a remote device, then accept the shared ROM folder and choose its local location.")
                    }

                    Section {
                        TextField("XXXXXXX-XXXXXXX-…", text: $syncthingDeviceId, axis: .vertical)
                            .font(.body.monospaced())
                            .textInputAutocapitalization(.characters)
                            .autocorrectionDisabled()
                            .lineLimit(2...3)
                    } header: {
                        Text("Handheld device ID")
                    } footer: {
                        Text("Paste the complete Device ID exactly as shown. Hyphens are accepted.")
                    }

                    Section {
                        Label("Add this handheld to the NUC’s Syncthing", systemImage: "plus.circle")
                        Label("Create or reuse \(device.name)’s ROM folder", systemImage: "folder")
                        Label("Share the folder and request a scan", systemImage: "arrow.triangle.2.circlepath")
                    } header: {
                        Text("ROMmates will")
                    } footer: {
                        Text("Only the Syncthing Device ID is accepted. ROMmates derives the server folder path from this device and does not allow a custom path.")
                    }

                }
            }
            .navigationTitle(result == nil ? "Set Up Syncthing" : "Syncthing Ready")
            .navigationBarTitleDisplayMode(.inline)
            .interactiveDismissDisabled(busy)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(result == nil ? "Cancel" : "Done") { dismiss() }
                        .disabled(busy)
                }
                if result == nil {
                    ToolbarItem(placement: .confirmationAction) {
                        Button(busy ? "Creating…" : "Create Share") {
                            Task { await createShare() }
                        }
                        .disabled(normalizedId.count < 7 || busy)
                    }
                }
            }
        }
    }

    private func createShare() async {
        guard normalizedId.count >= 7 else { return }
        busy = true
        defer { busy = false }
        do {
            let body = try JSONEncoder.rommates.encode(
                SyncthingShareBody(deviceId: normalizedId)
            )
            result = try await model.request(
                "/api/devices/\(device.id)/syncthing-share",
                method: "POST",
                body: body
            )
            didComplete()
        } catch {
            model.report(error, prefix: "Syncthing setup")
        }
    }
}

private struct SyncthingPrerequisite: View {
    let icon: String
    let title: String
    let detail: String

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: icon)
                .font(.body.weight(.semibold))
                .foregroundStyle(ROMTheme.violet)
                .frame(width: 22, alignment: .center)
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.body.weight(.semibold))
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, 3)
        .accessibilityElement(children: .combine)
    }
}

private struct CreateDeviceGroupView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let devices: [Device]
    let didCreate: () -> Void
    @State private var name = ""
    @State private var sourceDeviceId: Int?
    @State private var memberIds: Set<Int> = []
    @State private var showingConfirmation = false
    @State private var busy = false

    private var sourceDevice: Device? {
        devices.first { $0.id == sourceDeviceId }
    }
    private var selectedMembers: [Device] {
        devices.filter { memberIds.contains($0.id) }
    }
    private var canCreate: Bool {
        !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && sourceDevice != nil
            && !selectedMembers.isEmpty
            && !busy
    }

    var body: some View {
        NavigationStack {
            Form {
                if devices.count < 2 {
                    ContentUnavailableView {
                        Label("Two devices required", systemImage: "rectangle.3.group")
                    } description: {
                        Text("Create another device, or remove one from its current group, before starting a shared roster.")
                    }
                } else {
                    Section("Group") {
                        TextField("Name", text: $name, prompt: Text("Travel handhelds"))
                        Picker("Use selections from", selection: $sourceDeviceId) {
                            Text("Choose a device").tag(nil as Int?)
                            ForEach(devices) { device in
                                Text(device.name).tag(device.id as Int?)
                            }
                        }
                    }
                    Section {
                        ForEach(devices.filter { $0.id != sourceDeviceId }) { device in
                            Toggle(device.name, isOn: memberBinding(for: device.id))
                        }
                    } header: {
                        Text("Other devices")
                    } footer: {
                        Text("The source device’s selections will replace selections on the other devices, then stay shared across the group.")
                    }
                }
            }
            .navigationTitle("New Device Group")
            .navigationBarTitleDisplayMode(.inline)
            .onChange(of: sourceDeviceId) { oldValue, newValue in
                if let oldValue, oldValue != newValue { memberIds.remove(oldValue) }
                if let newValue { memberIds.remove(newValue) }
            }
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                if devices.count >= 2 {
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Review") { showingConfirmation = true }
                            .disabled(!canCreate)
                    }
                }
            }
            .confirmationDialog(
                "Create \(name.trimmingCharacters(in: .whitespacesAndNewlines))?",
                isPresented: $showingConfirmation,
                titleVisibility: .visible
            ) {
                Button("Use Roster & Create Group") { Task { await create() } }
                Button("Cancel", role: .cancel) { }
            } message: {
                Text(confirmationMessage)
            }
        }
    }

    private var confirmationMessage: String {
        guard let sourceDevice else { return "Choose a source device." }
        let targets = selectedMembers.map(\.name).joined(separator: ", ")
        return "Use \(sourceDevice.name)’s selections for \(targets). Future selection changes will affect every device in the group."
    }

    private func memberBinding(for id: Int) -> Binding<Bool> {
        Binding(
            get: { memberIds.contains(id) },
            set: { selected in
                if selected { memberIds.insert(id) }
                else { memberIds.remove(id) }
            }
        )
    }

    private func create() async {
        guard canCreate, let sourceDeviceId else { return }
        busy = true
        defer { busy = false }
        do {
            let body = try JSONEncoder.rommates.encode(
                CreateDeviceGroupBody(
                    name: name.trimmingCharacters(in: .whitespacesAndNewlines),
                    sourceDeviceId: sourceDeviceId,
                    memberDeviceIds: selectedMembers.map(\.id)
                )
            )
            let _: CreatedDeviceGroup = try await model.request(
                "/api/device-groups", method: "POST", body: body
            )
            didCreate()
        } catch { model.report(error) }
    }
}

private struct CreateDeviceView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let didCreate: () -> Void
    @State private var name = ""
    @State private var delivery = "syncthing"
    @State private var capacityGB = 128
    @State private var busy = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Device") {
                    TextField("Name", text: $name)
                    Picker("Delivery", selection: $delivery) {
                        Text("Syncthing").tag("syncthing")
                        Text("Manual download").tag("download")
                    }
                    Stepper("ROM capacity: \(capacityGB) GB", value: $capacityGB, in: 1...2048)
                }
                Section {
                    Text(delivery == "syncthing" ?
                        "After creating the device, open it and enter the handheld’s Syncthing Device ID to create the share." :
                        "ROMmates will build a downloadable ZIP after you apply selections.")
                }
            }
            .navigationTitle("New Device")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Create") { Task { await create() } }.disabled(name.isEmpty || busy)
                }
            }
        }
    }

    private func create() async {
        busy = true
        defer { busy = false }
        do {
            let body = try JSONEncoder.rommates.encode(
                CreateDeviceBody(
                    name: name,
                    deploymentMode: "hardlink",
                    deliveryMode: delivery,
                    keepInSync: false,
                    storageCapacityBytes: Int64(capacityGB) * 1_000_000_000
                )
            )
            let _: CreatedDevice = try await model.request("/api/devices", method: "POST", body: body)
            didCreate()
        } catch { model.report(error) }
    }
}

private struct DeviceSelectionBody: Encodable { let gameId: Int; let selected: Bool }
private struct DeviceSelectionResponse: Decodable, Sendable { let selected: Bool }
private struct DiscardResponse: Decodable, Sendable { let devices: Int; let games: Int }
private struct DeviceGroupApplyResponse: Decodable, Sendable {
    let groupId: Int
    let devices: Int
    let jobIds: [Int]
}
private struct DeviceGroupSummaryResponse: Decodable, Sendable {
    struct Member: Decodable, Sendable {
        let deviceId: Int
        let summary: DeviceSummary
    }

    let groupId: Int
    let members: [Member]
}
private struct DeviceGroupPreviewResponse: Decodable, Sendable {
    struct Member: Decodable, Sendable {
        let deviceId: Int
        let preview: DeviceChangePreview
    }

    let groupId: Int
    let members: [Member]
}
private struct DeviceChangePreview: Decodable, Sendable {
    struct Changes: Decodable, Sendable {
        let additions: [DeviceChangeItem]
        let conversions: [DeviceChangeItem]
        let removals: [DeviceChangeItem]
    }

    let currentRomBytes: Int64
    let projectedRomBytes: Int64
    let storageCapacityBytes: Int64
    let overCapacity: Bool
    let additions: Int
    let removals: Int
    let conversions: Int
    let changes: Changes
}
private struct DeviceChangeItem: Decodable, Identifiable, Sendable {
    let id: Int
    let displayName: String
    let platform: String
    let files: Int
    let bytes: Int64?
}
private struct CreateDeviceBody: Encodable {
    let name: String
    let deploymentMode: String
    let deliveryMode: String
    let keepInSync: Bool
    let storageCapacityBytes: Int64
}
private struct CreatedDevice: Decodable, Sendable { let id: Int; let name: String }
private struct CreateDeviceGroupBody: Encodable {
    let name: String
    let sourceDeviceId: Int
    let memberDeviceIds: [Int]
}
private struct CreatedDeviceGroup: Decodable, Sendable {
    let id: Int
    let name: String
    let devices: Int
    let games: Int
}

private struct SyncthingShareBody: Encodable {
    let deviceId: String
}

private struct SyncthingShareResult: Decodable, Sendable {
    let deviceId: String
    let folderId: String
    let folderPath: String
    let created: Bool
}
