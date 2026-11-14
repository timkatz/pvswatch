import Charts
import SwiftUI

struct PanelHistory: Decodable {
    let serial: String
    let range: String?
    let bucketSeconds: Double?
    let totalKwh: Double?
    let latest: Latest?
    let buckets: [Bucket]

    struct Latest: Decodable {
        let watts: Double?
        let state: String?
        let tempC: Double?
        let vAc: Double?
        let vDc: Double?
        let iDc: Double?
        let lifetimeKwh: Double?
        let panelModel: String?
        let ts: Double?
    }
    struct Bucket: Decodable, Identifiable {
        var id: Double { bucket }
        let bucket: Double
        let kwh: Double?
        let avgW: Double?
        let avgTempC: Double?
        var date: Date { Date(timeIntervalSince1970: bucket) }
    }
}

// One panel's detail: now, energy per bucket, power and temperature
// trends. From /api/v1/panels/<serial>/history.
struct PanelDetail: View {
    @Environment(Store.self) private var store
    let panel: Now.Panel
    let close: () -> Void
    @State private var range = "7d"
    @State private var data: PanelHistory?
    @State private var error: String?
    @State private var sel: Date?

    var body: some View {
        VStack(spacing: 0) {
            ModalHeader(title: "Panel \(panel.serial.suffix(6))", subtitle: panel.model, close: close)
            Page {
                Picker("Range", selection: $range) {
                    ForEach([("24h", "24H"), ("7d", "7D"), ("30d", "30D"), ("90d", "90D")], id: \.0) { Text($0.1).tag($0.0) }
                }
                .pickerStyle(.segmented)

                TileGrid {
                    Tile(icon: "bolt.fill", label: "Now", value: panel.status == "ok" ? "\(Int(panel.w ?? 0)) W" : panel.status.capitalized,
                         sub: panel.state, color: panel.status == "error" ? Theme.red : Theme.solar)
                    Tile(icon: "sum", label: "This period", value: Fmt.kwh(data?.totalKwh), color: Theme.green)
                    Tile(icon: "calendar", label: "Lifetime", value: Fmt.kwh(data?.latest?.lifetimeKwh ?? panel.lifetimeKwh), color: Theme.accent)
                    Tile(icon: "thermometer.medium", label: "Temp", value: (data?.latest?.tempC ?? panel.tempC).map { "\(Int($0))°C" } ?? "—", color: Theme.cyan)
                }

                if let d = data, !d.buckets.isEmpty {
                    Card(title: "Energy per \(bucketLabel(d.bucketSeconds))") {
                        let hit = nearest(d.buckets, to: sel, by: \.date)
                        Chart {
                            ForEach(d.buckets) { b in
                                BarMark(x: .value("t", b.date), y: .value("kWh", b.kwh ?? 0))
                                    .foregroundStyle(Theme.solar.gradient)
                            }
                            if let b = hit {
                                RuleMark(x: .value("t", b.date)).foregroundStyle(Theme.dim.opacity(0.6))
                                    .annotation(position: .top, spacing: 0, overflowResolution: .init(x: .fit(to: .chart), y: .fit(to: .chart))) {
                                        ChartTip(title: b.date.tipLabel, rows: [
                                            ("Energy", b.kwh.map { String(format: "%.3f kWh", $0) } ?? "—", Theme.solar),
                                            ("Avg power", b.avgW.map { "\(Int($0)) W" } ?? "—", Theme.accent),
                                            ("Avg temp", b.avgTempC.map { "\(Int($0))°C" } ?? "—", Theme.cyan),
                                        ])
                                    }
                            }
                        }
                        .chartXSelection(value: $sel)
                        .chartYAxisLabel("kWh")
                        .frame(height: 180)
                    }
                    Card(title: "Average power and temperature") {
                        Chart {
                          ForEach(d.buckets) { b in
                            if let w = b.avgW {
                                LineMark(x: .value("t", b.date), y: .value("value", w), series: .value("s", "Watts"))
                                    .foregroundStyle(Theme.solar)
                            }
                            if let t = b.avgTempC {
                                LineMark(x: .value("t", b.date), y: .value("value", t), series: .value("s", "°C"))
                                    .foregroundStyle(Theme.cyan).lineStyle(StrokeStyle(lineWidth: 1.2, dash: [4, 3]))
                            }
                          }
                          if let b = nearest(d.buckets, to: sel, by: \.date) {
                              RuleMark(x: .value("t", b.date)).foregroundStyle(Theme.dim.opacity(0.6))
                          }
                        }
                        .chartXSelection(value: $sel)
                        .chartForegroundStyleScale(["Watts": Theme.solar, "°C": Theme.cyan])
                        .chartLegend(position: .top)
                        .frame(height: 160)
                    }
                } else if let error {
                    Card { Text(error).foregroundStyle(Theme.red) }
                } else {
                    ProgressView().frame(maxWidth: .infinity, minHeight: 200)
                }

                Card(title: "Panel") {
                    Row("Serial", panel.serial)
                    Row("Model", panel.model)
                    Row("Firmware", panel.firmware)
                    Row("AC voltage", (data?.latest?.vAc ?? panel.acV).map { String(format: "%.1f V", $0) })
                    Row("DC voltage / current", "\((data?.latest?.vDc ?? panel.dcV).map { String(format: "%.1f V", $0) } ?? "—") / \((data?.latest?.iDc ?? panel.dcA).map { String(format: "%.2f A", $0) } ?? "—")")
                }
            }
        }
        .task(id: range) { await load() }
    }

    private func load() async {
        guard let api = store.api else { return }
        do {
            data = try await api.get("api/v1/panels/\(panel.serial)/history?range=\(range)")
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func bucketLabel(_ s: Double?) -> String {
        switch Int(s ?? 0) {
        case ..<3600: return "5 min"
        case 3600: return "hour"
        case 21600: return "6 hours"
        default: return "day"
        }
    }
}
