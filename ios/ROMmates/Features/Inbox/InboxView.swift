import SwiftUI

struct InboxView: View {
    @EnvironmentObject private var model: AppModel
    @State private var items: [InboxItem] = []
    @State private var unread = 0
    @State private var loading = true

    var body: some View {
        NavigationStack {
            Group {
                if loading && items.isEmpty {
                    ProgressView("Loading inbox…")
                } else if items.isEmpty {
                    EmptyState(
                        icon: "tray",
                        title: "You’re all caught up",
                        message: "Device delivery and upload updates will appear here."
                    )
                } else {
                    List(items) { item in
                        Button { Task { await open(item) } } label: {
                            InboxRow(item: item)
                        }
                        .buttonStyle(.plain)
                    }
                    .listStyle(.plain)
                    .refreshable { await load() }
                }
            }
            .navigationTitle(unread > 0 ? "Inbox (\(unread))" : "Inbox")
            .toolbar {
                if unread > 0 {
                    Button("Read All") { Task { await readAll() } }
                }
            }
            .task { await load() }
        }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            let response: InboxResponse = try await model.request("/api/inbox")
            items = response.items
            unread = response.unread
            model.setInboxUnread(response.unread)
        } catch { model.errorMessage = error.localizedDescription }
    }

    private func open(_ item: InboxItem) async {
        if item.readAt == nil {
            do {
                let _: ReadResponse = try await model.request(
                    "/api/inbox/\(item.id)/read", method: "POST"
                )
            } catch { model.errorMessage = error.localizedDescription }
        }
        await load()
        model.navigate(path: item.path)
    }

    private func readAll() async {
        do {
            let _: ReadAllResponse = try await model.request("/api/inbox/read-all", method: "POST")
            await load()
        } catch { model.errorMessage = error.localizedDescription }
    }
}

private struct InboxRow: View {
    let item: InboxItem

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            ZStack {
                Circle().fill(item.readAt == nil ? ROMTheme.softViolet : Color(.secondarySystemFill))
                Image(systemName: icon).foregroundStyle(item.readAt == nil ? ROMTheme.violet : .secondary)
            }
            .frame(width: 38, height: 38)
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline) {
                    Text(item.title).font(.body.weight(item.readAt == nil ? .semibold : .regular))
                    Spacer()
                    if item.readAt == nil { Circle().fill(ROMTheme.violet).frame(width: 7, height: 7) }
                }
                Text(item.detail).font(.subheadline).foregroundStyle(.secondary).lineLimit(3)
                Text(ROMTheme.relativeDate(item.createdAt)).font(.caption).foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 6)
    }

    private var icon: String {
        if item.kind == "new_build" { return "sparkles" }
        if item.kind.hasPrefix("device") { return "gamecontroller.fill" }
        if item.kind.hasPrefix("upload") { return "arrow.up.doc.fill" }
        return "bell.fill"
    }
}

private struct ReadResponse: Decodable, Sendable { let read: Int }
private struct ReadAllResponse: Decodable, Sendable { let updated: Int }
