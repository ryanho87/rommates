import SwiftUI

struct DevicesView: View {
    @EnvironmentObject private var model: AppModel
    @State private var devices: [Device] = []
    @State private var loading = true
    @State private var showingCreate = false

    var body: some View {
        NavigationStack {
            Group {
                if loading && devices.isEmpty {
                    ProgressView("Loading devices…")
                } else if devices.isEmpty {
                    EmptyState(
                        icon: "gamecontroller",
                        title: "No devices yet",
                        message: "Create a device to stage a tailored ROM collection."
                    )
                } else {
                    List(devices) { device in
                        NavigationLink(value: device) { DeviceRow(device: device) }
                    }
                    .refreshable { await load() }
                }
            }
            .navigationTitle("Devices")
            .navigationDestination(for: Device.self) { DeviceDetailView(device: $0) }
            .toolbar {
                Button { showingCreate = true } label: {
                    Label("New Device", systemImage: "plus")
                }
            }
            .sheet(isPresented: $showingCreate) {
                CreateDeviceView {
                    showingCreate = false
                    Task { await load() }
                }
            }
            .task { await load() }
        }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do { devices = try await model.request("/api/devices") }
        catch { model.errorMessage = error.localizedDescription }
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
    @State private var scope = "changes"
    @State private var applying = false

    private var selectedBytes: Int64 {
        inventory?.selectedPlatforms.reduce(0) { $0 + ($1.bytes ?? 0) }
            ?? games.filter { $0.selected != 0 }.reduce(0) { $0 + $1.size }
    }
    private var changes: Int { inventory?.changes ?? 0 }

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
                    Text("Selected").tag("selected")
                    Text("On device").tag("on_device")
                    Text("All").tag("all")
                }
                .pickerStyle(.segmented)
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
        .refreshable { await load(); await loadSync() }
        .task(id: scope) { await load() }
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

    private func load() async {
        do {
            let response: GameList = try await model.request(
                "/api/games",
                query: [
                    .init(name: "device_id", value: String(device.id)),
                    .init(name: "device_scope", value: scope),
                    .init(name: "refresh_device_inventory", value: "true"),
                    .init(name: "limit", value: "500"),
                ]
            )
            games = response.items
            inventory = response.deviceInventory
        } catch { model.errorMessage = error.localizedDescription }
    }

    private func loadSync() async {
        do { sync = try await model.request("/api/devices/\(device.id)/sync-status") }
        catch { /* Refresh remains available even when Syncthing is offline. */ }
    }

    private func select(_ game: Game, selected: Bool) async {
        do {
            let body = try JSONEncoder.rommates.encode(DeviceSelectionBody(gameId: game.id, selected: selected))
            let _: DeviceSelectionResponse = try await model.request(
                "/api/devices/\(device.id)/selection", method: "PUT", body: body
            )
            await load()
        } catch { model.errorMessage = error.localizedDescription }
    }

    private func apply() async {
        applying = true
        defer { applying = false }
        do {
            let _: JobReference = try await model.request("/api/devices/\(device.id)/apply", method: "POST")
            await load()
            await loadSync()
        } catch { model.errorMessage = error.localizedDescription }
    }

    private func discard() async {
        do {
            let _: DiscardResponse = try await model.request(
                "/api/devices/\(device.id)/discard-changes", method: "POST"
            )
            await load()
        } catch { model.errorMessage = error.localizedDescription }
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
        } catch { model.errorMessage = error.localizedDescription }
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
