import SwiftUI

@main
struct PvsWatchApp: App {
    @State private var store = Store()
    @Environment(\.scenePhase) private var phase

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(store)
                .preferredColorScheme(.dark)
                .tint(Theme.accent)
        }
        .onChange(of: phase) { _, p in
            if p == .active { store.start(); Task { await store.refreshAll() } } else { store.stop() }
        }
    }
}

// iPhone Duo postures (same detection as arc):
//   cover  — folded, the outer screen: a tab bar
//   flat   — open flat: sidebar | section
//   book   — half-open, crease vertical: Now on the left leaf, the section on the right
//   laptop — half-open, crease horizontal: the flow on top, battery + section below
enum Posture { case cover, flat, book, laptop }

struct RootView: View {
    @Environment(Store.self) private var store
    @Environment(\.horizontalSizeClass) private var hsc
    @State private var hinge: Hinge?
    @State private var lastHalf: Posture?
    // `-section <name>` launch argument (development) opens a section.
    @State private var section: AppSection = AppSection(rawValue: UserDefaults.standard.string(forKey: "section") ?? "") ?? .now
    @State private var detail: AppSection = .history

    var body: some View {
        GeometryReader { geo in
            let crease = geo.reservedRegions(kind: .division).first(where: \.isActive)?.frame
            let p = posture(crease)
            layout(p, crease: crease, size: geo.size)
                .animation(.snappy, value: p)
                .onChange(of: p, initial: true) { _, p in if p == .book || p == .laptop { lastHalf = p } }
        }
        .ignoresSafeArea(.keyboard)
        .background(Theme.bg)
        .background(HingeReader { hinge = $0 })
        .onHingeChange { _, ctx in hinge = ctx.hinge.map(Hinge.init) }
        .task { store.start() }
    }

    private func posture(_ crease: CGRect?) -> Posture {
        if hsc == .compact { return .cover }
        guard hinge?.halfOpen == true else { return .flat }
        guard let crease else { return lastHalf ?? .flat }
        return crease.width > crease.height ? .laptop : .book
    }

    @ViewBuilder
    private func layout(_ p: Posture, crease: CGRect?, size: CGSize) -> some View {
        switch p {
        case .cover:
            Tabs(selection: $section)
        case .flat:
            Tabs(selection: $section).tabViewStyle(.sidebarAdaptable)
        case .book:
            let split = crease.map { $0.minX } ?? size.width / 2
            let gap = crease?.width ?? 0
            HStack(spacing: gap) {
                NavigationStack { NowView().navigationTitle("Now") }.frame(width: split)
                NavigationStack {
                    screen(detail)
                        .navigationTitle(detail.title)
                        .toolbar { SectionMenu(selection: $detail, exclude: .now) }
                }
            }
        case .laptop:
            let split = crease.map { $0.minY } ?? size.height / 2
            let gap = crease?.height ?? 0
            VStack(spacing: gap) {
                // Upright leaf: the live flow, sized to fit — nothing to scroll.
                LaptopFlow().frame(height: split)
                // Flat leaf: SunVault controls by default (thumb-reachable),
                // or any section picked from the menu.
                NavigationStack {
                    Group {
                        if detail == .history || detail == .now || detail == .battery { BatteryDeck() } else { screen(detail) }
                    }
                    .toolbar { SectionMenu(selection: $detail, exclude: .now) }
                    #if os(iOS)
                    .navigationBarTitleDisplayMode(.inline)
                    #endif
                }
            }
        }
    }
}

struct Tabs: View {
    @Binding var selection: AppSection
    var body: some View {
        // Grouped in the open-flat sidebar; a flat tab bar when folded.
        TabView(selection: $selection) {
            ForEach(AppSection.groups, id: \.self) { g in
                TabSection(g) {
                    ForEach(AppSection.allCases.filter { $0.group == g }) { s in
                        Tab(s.title, systemImage: s.icon, value: s) {
                            NavigationStack { screen(s).navigationTitle(s.title) }
                        }
                    }
                }
            }
        }
    }
}

// Laptop posture's lower leaf by default: battery + mode, thumb-reachable.
struct BatteryDeck: View {
    @Environment(Store.self) private var store
    var body: some View {
        Page {
            if let d = store.battery {
                ModeCards(doc: d)
                ReserveCard(doc: d)
            } else { EmptyState() }
        }
    }
}

// Laptop posture's upright leaf: the diagram and the breakdown side by side.
struct LaptopFlow: View {
    @Environment(Store.self) private var store
    var body: some View {
        Group {
            if let n = store.now {
                HStack(alignment: .center, spacing: 16) {
                    FlowDiagram(now: n).frame(maxWidth: .infinity, maxHeight: .infinity)
                    FlowLegend(now: n, compact: true).frame(width: 240)
                }
                .padding(.horizontal, 16).padding(.vertical, 10)
            } else { EmptyState() }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Theme.bg)
    }
}

struct SectionMenu: ToolbarContent {
    @Binding var selection: AppSection
    let exclude: AppSection?
    var body: some ToolbarContent {
        ToolbarItem(placement: .principal) {
            // A titled menu ("SunVault ▾"), not a bare icon.
            Menu {
                ForEach(AppSection.allCases.filter { $0 != exclude }) { s in
                    Button { selection = s } label: { Label(s.title, systemImage: s.icon) }
                }
            } label: {
                HStack(spacing: 4) {
                    Text(selection == .history ? AppSection.battery.title : selection.title).font(.headline)
                    Image(systemName: "chevron.down").font(.caption.weight(.bold))
                }
                .foregroundStyle(Theme.text)
            }
        }
    }
}

@ViewBuilder
func screen(_ s: AppSection) -> some View {
    switch s {
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

struct Hinge: Equatable {
    var halfOpen: Bool

    init(_ h: DeviceHinge) { halfOpen = h.status == .partiallyOpen }
    init(_ h: UIHinge) { halfOpen = h.status == .partiallyOpen }
}

// UIKit's hinge interaction reports the current state when attached;
// onHingeChange only reports later changes.
struct HingeReader: UIViewRepresentable {
    var onChange: (Hinge?) -> Void

    func makeUIView(context: Context) -> UIView {
        let v = UIView()
        v.isUserInteractionEnabled = false
        v.addInteraction(UIHingeInteraction { _, update in
            let h = update.hinge.map(Hinge.init)
            DispatchQueue.main.async { onChange(h) }
        })
        return v
    }

    func updateUIView(_ v: UIView, context: Context) {}
}
