import SwiftUI

@main
struct ROMmatesApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(model)
                .tint(ROMTheme.violet)
                .task { model.start() }
        }
    }
}
