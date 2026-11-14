import SwiftUI

@main
struct PvsWatchMacApp: App {
    @State private var store = Store()

    var body: some Scene {
        WindowGroup("PVS Watch") {
            MacRoot()
                .environment(store)
                .preferredColorScheme(.dark)
                .tint(Theme.accent)
                .frame(minWidth: 760, minHeight: 520)
                .task { store.start() }
        }
        .commands {
            CommandGroup(after: .toolbar) {
                Button("Refresh") { Task { await store.refreshAll() } }.keyboardShortcut("r")
            }
        }

        Settings {
            SettingsView()
                .environment(store)
                .preferredColorScheme(.dark)
                .frame(width: 520, height: 640)
        }
    }
}

struct MacRoot: View {
    @Environment(Store.self) private var store
    @SceneStorage("section") private var section: AppSection = .now

    var body: some View {
        NavigationSplitView {
            List(selection: Binding(get: { section }, set: { if let s = $0 { section = s } })) {
                ForEach(AppSection.groups, id: \.self) { g in
                    Section(g) {
                        ForEach(AppSection.allCases.filter { $0.group == g }) { s in
                            Label(s.title, systemImage: s.icon).tag(s)
                        }
                    }
                }
            }
            .navigationSplitViewColumnWidth(min: 160, ideal: 190)
        } detail: {
            Group {
                switch section {
                case .now: NowView()
                case .battery: BatteryView()
                case .history: HistoryView()
                case .money: MoneyView()
                case .outages: OutagesView()
                case .panels: PanelsView()
                case .system: SystemView()
                case .settings: SettingsView()
                }
            }
            .navigationTitle(section.title)
            .toolbar {
                ToolbarItem {
                    Button { Task { await store.refreshAll() } } label: { Label("Refresh", systemImage: "arrow.clockwise") }
                }
            }
        }
        .background(Theme.bg)
        // `-section <name>` launch argument (development) opens a section.
        .onAppear { if let s = AppSection(rawValue: UserDefaults.standard.string(forKey: "section") ?? "") { section = s } }
    }
}
