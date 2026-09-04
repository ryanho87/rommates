import SwiftUI
import UIKit

struct GuidedTourStep: Identifiable, Equatable, Sendable {
    let id: String
    let tab: AppTab
    let icon: String
    let title: String
    let message: String
}

enum GuidedTourCatalog {
    static let version = 1

    static func key(for permissions: Permissions) -> String {
        switch (permissions.manageDevices, permissions.upload) {
        case (true, true): return "getting-started-ios-member-contributor"
        case (true, false): return "getting-started-ios-member"
        case (false, true): return "getting-started-ios-contributor"
        case (false, false): return "getting-started-ios-viewer"
        }
    }

    static func steps(for permissions: Permissions) -> [GuidedTourStep] {
        var steps = [
            GuidedTourStep(
                id: "library",
                tab: .library,
                icon: "magnifyingglass",
                title: "Find your next game",
                message: "Search by title, filter by platform, and open a game to see details or choose where it belongs."
            )
        ]
        if permissions.manageDevices {
            steps.append(contentsOf: [
                GuidedTourStep(
                    id: "devices",
                    tab: .devices,
                    icon: "gamecontroller.fill",
                    title: "Your devices and groups",
                    message: "Open a handheld or shared group to manage its desired collection. Use + to create another device or group."
                ),
                GuidedTourStep(
                    id: "device-views",
                    tab: .devices,
                    icon: "line.3.horizontal.decrease.circle",
                    title: "Three useful views",
                    message: "Changes shows staged adds and removals. On Device shows files physically present. Library shows everything available."
                ),
                GuidedTourStep(
                    id: "device-apply",
                    tab: .devices,
                    icon: "arrow.triangle.2.circlepath.circle.fill",
                    title: "Apply when you are ready",
                    message: "Switches stage your desired roster. Review the change count, then apply it when you are ready. Syncthing handles delivery."
                )
            ])
        }
        if permissions.upload {
            steps.append(
                GuidedTourStep(
                    id: "uploads",
                    tab: .uploads,
                    icon: "arrow.up.doc.fill",
                    title: "Send a ROM for review",
                    message: "Uploads resume if interrupted and stay in review until an administrator approves them."
                )
            )
        }
        steps.append(contentsOf: [
            GuidedTourStep(
                id: "inbox",
                tab: .inbox,
                icon: "tray.full.fill",
                title: "Follow the work",
                message: "Delivery and review updates land in Inbox. The badge shows how many items are unread."
            ),
            GuidedTourStep(
                id: "account",
                tab: .account,
                icon: "person.crop.circle.fill",
                title: "Make ROMmates yours",
                message: "Manage push notifications, profile details, and restart this guided tour here anytime."
            )
        ])
        return steps
    }
}

struct GuidedTourCard: View {
    let step: GuidedTourStep
    let index: Int
    let count: Int
    let back: () -> Void
    let next: () -> Void
    let skip: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("GUIDED TOUR")
                    .font(.caption2.weight(.bold))
                    .tracking(0.9)
                    .foregroundStyle(ROMTheme.violet)
                Spacer()
                Text("\(index + 1) of \(count)")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }

            HStack(alignment: .top, spacing: 14) {
                Image(systemName: step.icon)
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(ROMTheme.violet)
                    .frame(width: 38, height: 38)
                    .background(ROMTheme.softViolet, in: Circle())
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 5) {
                    Text(step.title)
                        .font(.headline)
                    Text(step.message)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            HStack(spacing: 10) {
                Button("Skip tour", action: skip)
                    .buttonStyle(.plain)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(.secondary)
                Spacer(minLength: 8)
                if index > 0 {
                    Button("Back", action: back)
                        .buttonStyle(.bordered)
                }
                Button(index + 1 == count ? "Finish" : "Next", action: next)
                    .buttonStyle(.borderedProminent)
            }
        }
        .padding(18)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 22, style: .continuous)
                .stroke(Color.primary.opacity(0.12), lineWidth: 0.5)
        }
        .shadow(color: .black.opacity(0.18), radius: 18, y: 8)
        .accessibilityElement(children: .contain)
        .onAppear(perform: announce)
        .onChange(of: step.id) { _, _ in announce() }
    }

    private func announce() {
        UIAccessibility.post(
            notification: .announcement,
            argument: "Step \(index + 1) of \(count). \(step.title). \(step.message)"
        )
    }
}
