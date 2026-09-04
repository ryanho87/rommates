import SwiftUI

enum ROMTheme {
    static let violet = Color(red: 0.43, green: 0.25, blue: 0.88)
    static let softViolet = Color(red: 0.91, green: 0.88, blue: 1.0)
    static let ink = Color(red: 0.08, green: 0.075, blue: 0.10)

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
