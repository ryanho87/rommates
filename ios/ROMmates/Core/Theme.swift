import SwiftUI

enum ROMTheme {
    static let violet = Color(red: 0.43, green: 0.25, blue: 0.88)
    static let softViolet = Color(red: 0.91, green: 0.88, blue: 1.0)
    static let ink = Color(red: 0.08, green: 0.075, blue: 0.10)
    static let success = Color(red: 0.19, green: 0.60, blue: 0.39)
    static let warning = Color(red: 0.83, green: 0.55, blue: 0.13)
    static let danger = Color(red: 0.82, green: 0.25, blue: 0.24)

    static func platformColor(_ platform: String) -> Color {
        switch platform.lowercased() {
        case "nes", "snes", "n64", "gb", "gbc", "gba", "nds", "3ds", "gc", "wii", "wiiu", "switch":
            return Color(red: 0.82, green: 0.25, blue: 0.31)
        case "psx", "ps2", "ps3", "psp", "psvita":
            return Color(red: 0.27, green: 0.48, blue: 0.88)
        case "genesis", "megadrive", "mastersystem", "gamegear", "saturn", "dreamcast":
            return Color(red: 0.10, green: 0.60, blue: 0.68)
        case "xbox", "xbox360":
            return Color(red: 0.24, green: 0.62, blue: 0.29)
        case "arcade", "mame", "fbneo", "neogeo":
            return Color(red: 0.84, green: 0.51, blue: 0.14)
        default:
            return violet
        }
    }

    static func bytes(_ value: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: value, countStyle: .file)
    }

    static func relativeDate(_ value: String) -> String {
        let formatter = ISO8601DateFormatter()
        let normalized = value.replacingOccurrences(of: " ", with: "T") + (value.contains("Z") ? "" : "Z")
        guard let date = formatter.date(from: normalized) else { return value }
        return date.formatted(.relative(presentation: .named))
    }
}

struct PlatformBadge: View {
    let platform: String

    var body: some View {
        Text(platform.uppercased())
            .font(.caption2.weight(.bold))
            .tracking(0.45)
            .foregroundStyle(ROMTheme.platformColor(platform))
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(ROMTheme.platformColor(platform).opacity(0.14), in: Capsule())
            .overlay {
                Capsule().stroke(ROMTheme.platformColor(platform).opacity(0.28), lineWidth: 0.5)
            }
            .accessibilityLabel("Platform (platform)")
    }
}

struct DeviceStateBadge: View {
    let state: String

    private var presentation: (String, String, Color) {
        switch state {
        case "on_device", "synced", "present":
            return ("On device", "checkmark.circle.fill", ROMTheme.success)
        case "pending_add":
            return ("Adding", "plus.circle.fill", ROMTheme.violet)
        case "pending_update":
            return ("Updating", "arrow.triangle.2.circlepath.circle.fill", ROMTheme.warning)
        case "pending_remove":
            return ("Removing", "minus.circle.fill", ROMTheme.warning)
        default:
            return (state.replacingOccurrences(of: "_", with: " ").capitalized, "circle.fill", .secondary)
        }
    }

    var body: some View {
        Label(presentation.0, systemImage: presentation.1)
            .font(.caption2.weight(.semibold))
            .foregroundStyle(presentation.2)
            .lineLimit(1)
    }
}

struct StatusLabel: View {
    let text: String
    var icon: String = "circle.fill"
    var color: Color = ROMTheme.violet

    var body: some View {
        Label(text, systemImage: icon)
            .font(.caption.weight(.medium))
            .foregroundStyle(color)
            .lineLimit(1)
    }
}

struct EmptyState: View {
    let icon: String
    let title: String
    let message: String

    var body: some View {
        ContentUnavailableView(title, systemImage: icon, description: Text(message))
    }
}
