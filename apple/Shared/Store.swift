import Foundation
import Observation

enum AppSection: String, CaseIterable, Identifiable, Hashable {
    case now, battery, panels, history, money, outages, system, settings
    var id: String { rawValue }
    /// Sidebar groups: what's happening, how it's trending, setup.
    var group: String {
        switch self {
        case .now, .battery, .panels: "Live"
        case .history, .money, .outages: "Trends"
        case .system, .settings: "Setup"
        }
    }
    static let groups = ["Live", "Trends", "Setup"]
    var title: String {
        switch self {
        case .now: "Now"
        case .battery: "SunVault"
        case .history: "History"
        case .money: "Money"
        case .outages: "Outages"
        case .panels: "Panels"
        case .system: "System"
        case .settings: "Settings"
        }
    }
    var icon: String {
        switch self {
        case .now: "bolt.fill"
        case .battery: "battery.100percent.bolt"
        case .history: "chart.xyaxis.line"
        case .money: "dollarsign.circle"
        case .outages: "powerplug"
        case .panels: "square.grid.3x3.fill"
        case .system: "info.circle"
        case .settings: "gearshape"
        }
    }
}

@Observable
final class Store {
    // The pvswatch server, e.g. https://pvswatch.<tailnet>.ts.net. Set in
    // Settings (or `-server <url>` as a launch argument in development).
    var server: String = UserDefaults.standard.string(forKey: "server") ?? "" {
        didSet {
            UserDefaults.standard.set(server, forKey: "server")
            reset()
            Task { await refreshAll() }
        }
    }

    var now: Now?
    var sunvault: SunVault?
    var history: History?
    /// Since local midnight, for the Now screen's "Today" row.
    var today: History.Totals?
    /// The whole of yesterday, shown next to today.
    var yesterday: History.Totals?
    var historyRange = "24h" { didSet { Task { await loadHistory() } } }
    var outages: Outages?
    var tou: Tou?
    var finance: Finance?
    var financeYears: Finance?
    var settings: SettingsDoc?
    var battery: BatteryDoc?
    var reserveBusy = false
    var reserveMessage: String?

    var error: String?
    var lastOK: Date?
    var modeBusy = false
    var modeMessage: String?

    var api: API? {
        let s = server.trimmingCharacters(in: .whitespaces)
        guard !s.isEmpty, let url = URL(string: s.hasSuffix("/") ? s : s + "/"), url.scheme != nil else { return nil }
        return API(base: url)
    }

    private var loop: Task<Void, Never>?

    init() {
        // A `-server` launch argument only lives for that launch; save it so
        // the app keeps the address when opened from the home screen.
        if !server.isEmpty { UserDefaults.standard.set(server, forKey: "server") }
    }

    func start() {
        guard loop == nil else { return }
        loop = Task { [weak self] in
            var tick = 0
            while !Task.isCancelled {
                guard let self else { return }
                if tick % 9 == 0 { await self.refreshAll() } else { await self.refreshLive() }
                tick += 1
                try? await Task.sleep(for: .seconds(20))
            }
        }
    }

    func stop() { loop?.cancel(); loop = nil }

    private func reset() {
        now = nil; battery = nil; sunvault = nil; history = nil; outages = nil; tou = nil; finance = nil; settings = nil
        error = nil; lastOK = nil
    }

    // Every 20 s: the live screen. Every 3 min (and on demand): the rest.
    func refreshLive() async {
        guard let api else { return }
        do {
            now = try await api.get("api/v1/now")
            sunvault = try? await api.get("api/v1/sunvault")
            battery = try? await api.get("api/v1/battery")
            error = nil
            lastOK = Date()
        } catch {
            self.error = error.localizedDescription
        }
    }

    func refreshAll() async {
        await refreshLive()
        guard api != nil, error == nil else { return }
        await loadToday()
        await loadHistory()
        await loadOutages()
        await loadMoney()
    }

    func loadToday() async {
        guard let api else { return }
        let midnight = Calendar.current.startOfDay(for: Date())
        let start = Calendar.current.date(byAdding: .day, value: -1, to: midnight)!
        let t: History? = try? await api.get("api/v1/history?start=\(Int(midnight.timeIntervalSince1970))&end=\(Int(Date().timeIntervalSince1970))")
        today = t?.periodTotals
        let y: History? = try? await api.get("api/v1/history?start=\(Int(start.timeIntervalSince1970))&end=\(Int(midnight.timeIntervalSince1970))")
        yesterday = y?.periodTotals
    }

    func loadHistory() async {
        guard let api else { return }
        history = try? await api.get("api/v1/history?range=\(historyRange)")
    }

    func loadOutages() async {
        guard let api else { return }
        outages = try? await api.get("api/v1/outages?range=30d")
    }

    func loadMoney() async {
        guard let api else { return }
        tou = try? await api.get("api/v1/tou")
        finance = try? await api.get("api/v1/finance?group=month&spread=1")
        financeYears = try? await api.get("api/v1/finance?group=year&spread=1")
    }

    func loadSettings() async {
        guard let api else { return }
        do { settings = try await api.get("api/v1/settings") } catch { self.error = error.localizedDescription }
    }

    /// PATCH settings; nil resets a key to its .env default. Returns an error message, or nil.
    func saveSettings(_ changes: [String: Any?]) async -> String? {
        guard let api else { return "No server set" }
        do {
            settings = try await api.send("api/v1/settings", method: "PATCH", body: changes)
            return nil
        } catch {
            return error.localizedDescription
        }
    }

    struct ModeResult: Decodable { let batteryMode: String? }

    func setBatteryMode(_ mode: String, label: String) async {
        guard let api, !modeBusy else { return }
        modeBusy = true
        modeMessage = "Switching to \(label)…"
        defer { modeBusy = false }
        do {
            let _: ModeResult = try await api.send("api/v1/battery/mode", method: "POST", body: ["mode": mode])
            modeMessage = "Set to \(label)"
            await refreshLive()
            if settings != nil { await loadSettings() }
        } catch {
            modeMessage = "Not changed: \(error.localizedDescription)"
        }
    }

    struct ReserveResult: Decodable { let reserve: Double? }

    func setReserve(pct: Int) async {
        guard let api, !reserveBusy else { return }
        reserveBusy = true
        reserveMessage = "Setting reserve to \(pct)%…"
        defer { reserveBusy = false }
        do {
            let _: ReserveResult = try await api.send("api/v1/battery/reserve", method: "POST", body: ["pct": pct])
            reserveMessage = "Reserve set to \(pct)%"
            await refreshLive()
        } catch {
            reserveMessage = "Not changed: \(error.localizedDescription)"
        }
    }

    var currentMode: String? { now?.battery?.mode ?? sunvault?.batteryMode }
}

// Battery modes, in the SunStrong app's order.
let batteryModes: [(value: String, label: String)] = [
    ("SELF_CONSUMPTION", "Self-Supply"),
    ("ENERGY_ARBITRAGE", "Cost Savings"),
    ("BACKUP_ONLY", "Reserve"),
]
