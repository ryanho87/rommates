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
                    EmptyState(
                        icon: "gamecontroller",
                        title: "No devices yet",
                        message: "Create a device to stage a tailored ROM collection."
                    )
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
                                    DeviceGroupRow(group: group, devices: devices)
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
            .navigationDestination(for: Device.self) { DeviceDetailView(device: $0) }
            .toolbar {
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
    let devices: [Device]

    var body: some View {
        DisclosureGroup {
            ForEach(group.members) { member in
                if let device = devices.first(where: { $0.id == member.id }) {
                    NavigationLink(value: device) {
                        DeviceGroupMemberRow(member: member)
                    }
                } else {
                    DeviceGroupMemberRow(member: member)
                }
            }
        } label: {
            HStack(spacing: 12) {
                Image(systemName: "rectangle.3.group.fill")
                    .foregroundStyle(ROMTheme.violet)
                    .frame(width: 28)
                VStack(alignment: .leading, spacing: 3) {
                    Text(group.name).font(.body.weight(.semibold))
                    Text("\(group.deviceCount) devices · \(group.selectedGames) selected")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.vertical, 4)
        }
        .accessibilityHint("Shows devices that share this game roster")
    }
}

private struct DeviceGroupMemberRow: View {
    let member: DeviceGroupMember

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: member.deliveryMode == "syncthing" ? "arrow.triangle.2.circlepath" : "arrow.down.circle")
                .foregroundStyle(.secondary)
                .frame(width: 24)
            VStack(alignment: .leading, spacing: 2) {
                Text(member.name)
                Text(member.deliveryMode == "syncthing" ?
                    (member.syncthingReadyAt == nil ? "Syncthing setup pending" : "Syncthing ready") :
                    "Manual download")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.leading, 4)
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
    @State private var games: [Game] = []
    @State private var inventory: DeviceInventory?
    @State private var sync: DeviceSyncStatus?
    @State private var scope = "on_device"
    @State private var platform = ""
    @State private var loadingGames = true
    @State private var hasLoadedGames = false
    @State private var didRefreshInventory = false
    @State private var applying = false

    private var selectedBytes: Int64 {
        inventory?.selectedPlatforms.reduce(0) { $0 + ($1.bytes ?? 0) }
            ?? games.filter { $0.selected != 0 }.reduce(0) { $0 + $1.size }
    }
    private var changes: Int { inventory?.changes ?? 0 }
    private var queryID: String { "\(scope)|\(platform)" }
    private var platformOptions: [String] {
        var values = inventory?.platforms.map(\.platform) ?? []
        if !platform.isEmpty && !values.contains(platform) { values.append(platform) }
        return values
    }

    var body: some View {
        List {
            Section {
                VStack(alignment: .leading, spacing: 10) {
                    HStack {
                        VStack(alignment: .leading, spacing: 3) {
                            Text(device.deliveryMode == "syncthing" ? "Syncthing delivery" : "Manual download")
                                .font(.headline)
                            Text(syncDetail).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if let run = sync?.syncRun, ["pending", "syncing", "offline"].contains(run.state) {
                            Text(run.completion / 100, format: .percent.precision(.fractionLength(0)))
                                .font(.headline.monospacedDigit())
                        }
                    }
                    if let run = sync?.syncRun, ["pending", "syncing", "offline"].contains(run.state) {
                        ProgressView(value: run.completion, total: 100)
                    }
                    if device.storageCapacityBytes > 0 {
                        ProgressView(value: min(Double(selectedBytes), Double(device.storageCapacityBytes)), total: Double(device.storageCapacityBytes))
                            .tint(selectedBytes > device.storageCapacityBytes ? .red : ROMTheme.violet)
                        Text("\(ROMTheme.bytes(selectedBytes)) staged of \(ROMTheme.bytes(device.storageCapacityBytes))")
                            .font(.caption).foregroundStyle(selectedBytes > device.storageCapacityBytes ? .red : .secondary)
                    }
                }
                .padding(.vertical, 4)
            }
            Section {
                Picker("View", selection: $scope) {
                    Text("Changes").tag("changes")
                    Text("On device").tag("on_device")
                    Text("Library").tag("all")
                }
                .pickerStyle(.segmented)
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
                            VStack(alignment: .leading, spacing: 3) {
                                Text(game.displayName).lineLimit(1)
                                HStack {
                                    Text(game.platform)
                                    Text("·")
                                    Text(ROMTheme.bytes(game.size))
                                    if let state = game.deviceState {
                                        Text("· \(state.replacingOccurrences(of: "_", with: " "))")
                                    }
                                }
                                .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            } header: {
                Text(changes == 1 ? "1 staged change" : "\(changes) staged changes")
            }
            Section {
                Button {
                    Task { await apply() }
                } label: {
                    Label(applying ? "Applying…" : "Apply Changes", systemImage: "arrow.triangle.2.circlepath")
                }
                .disabled(changes == 0 || applying || selectedBytes > device.storageCapacityBytes && device.storageCapacityBytes > 0)
                Button("Discard Staged Changes", role: .destructive) {
                    Task { await discard() }
                }
                .disabled(changes == 0 || applying)
            }
        }
        .navigationTitle(device.name)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            Menu {
                Picker("Platform", selection: $platform) {
                    Text("All platforms").tag("")
                    ForEach(platformOptions, id: \.self) { value in
                        Text(value).tag(value)
                    }
                }
            } label: {
                Label(
                    platform.isEmpty ? "Platform" : platform,
                    systemImage: platform.isEmpty ? "line.3.horizontal.decrease.circle" : "line.3.horizontal.decrease.circle.fill"
                )
            }
            .accessibilityLabel(platform.isEmpty ? "Filter by platform" : "Platform: \(platform)")
        }
        .refreshable {
            didRefreshInventory = true
            await load(refreshDeviceInventory: true)
            await loadSync()
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

    private func load(refreshDeviceInventory: Bool = false) async {
        let requestedScope = scope
        let requestedPlatform = platform
        if games.isEmpty && !refreshDeviceInventory { loadingGames = true }
        defer {
            if requestedScope == scope && requestedPlatform == platform {
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
                    .init(name: "refresh_device_inventory", value: String(refreshDeviceInventory)),
                    .init(name: "limit", value: "500"),
                ]
            )
            guard requestedScope == scope, requestedPlatform == platform else { return }
            games = response.items
            inventory = response.deviceInventory
            hasLoadedGames = true
        } catch let error as URLError where error.code == .cancelled {
        } catch is CancellationError {
        } catch { model.report(error) }
    }

    private func loadSync() async {
        do { sync = try await model.request("/api/devices/\(device.id)/sync-status", fresh: true) }
        catch { /* Refresh remains available even when Syncthing is offline. */ }
    }

    private func select(_ game: Game, selected: Bool) async {
        do {
            let body = try JSONEncoder.rommates.encode(DeviceSelectionBody(gameId: game.id, selected: selected))
            let _: DeviceSelectionResponse = try await model.request(
                "/api/devices/\(device.id)/selection", method: "PUT", body: body
            )
            await load()
        } catch let error as URLError where error.code == .cancelled {
        } catch is CancellationError {
        } catch { model.report(error) }
    }

    private func apply() async {
        applying = true
        defer { applying = false }
        do {
            let _: JobReference = try await model.request("/api/devices/\(device.id)/apply", method: "POST")
            await load()
            await loadSync()
        } catch { model.report(error) }
    }

    private func discard() async {
        do {
            let _: DiscardResponse = try await model.request(
                "/api/devices/\(device.id)/discard-changes", method: "POST"
            )
            await load()
        } catch { model.report(error) }
    }
}

private struct DeviceGamePlaceholder: View {
    var body: some View {
        Toggle(isOn: .constant(false)) {
            VStack(alignment: .leading, spacing: 5) {
                Text("Game title placeholder").font(.body)
                Text("platform · file size · device state").font(.caption)
            }
            .redacted(reason: .placeholder)
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
        default: return "No games available"
        }
    }

    private var message: String {
        if !platform.isEmpty { return "Choose another platform or clear the filter." }
        switch scope {
        case "changes": return "Your staged collection matches the device."
        case "on_device": return "Use Library to choose games for this device."
        default: return "The indexed library will appear here."
        }
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
                        "An administrator must complete the Syncthing share before the first delivery." :
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
