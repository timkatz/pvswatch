import Foundation

// Models for pvswatch's /api/v1 (docs/API.md). The server does all the math;
// these just carry it. Keys arrive snake_case and decode to camelCase.

struct Now: Decodable {
    let ts: Double
    let fetchedAt: Double?
    let dataTime: Double?
    let solarKw: Double
    let homeKw: Double
    let grid: Grid
    let battery: Battery?
    let houseSides: HouseSides?
    let flow: Flow
    let lifetimeKwh: Double?
    let electrical: Electrical
    let gridState: GridState
    let panels: Panels
    let system: SystemInfo

    struct Grid: Decodable {
        let kw: Double
        let direction: String   // importing | exporting | balanced
        let source: String
    }
    struct Battery: Decodable {
        let kw: Double?
        let soc: Double?
        let backupMin: Double?
        let state: String       // charging | discharging | idle
        let chargeSource: String?
        let fetchedAt: Double?
        let stale: Bool
        let mode: String?
        let modeLabel: String?
    }
    struct HouseSides: Decodable {
        let l1Kw: Double
        let l2Kw: Double
        let gapKw: Double
        let balanced: Bool
        let batteryCoverKw: Double
    }
    struct Flow: Decodable {
        let paths: [String: Double]
        let headline: String
    }
    struct Electrical: Decodable {
        let systemV: Double?
        let l1V: Double?
        let l2V: Double?
        let freqHz: Double?
        let pfSolar: Double?
        let pfHome: Double?
    }
    struct GridState: Decodable {
        let kind: String
        let midstate: Int?
    }
    struct Panels: Decodable {
        let mode: String        // producing | night | idle | comms_error | none
        let summary: String
        let count: Int
        let errors: Int
        let items: [Panel]
    }
    struct Panel: Decodable, Identifiable {
        var id: String { serial }
        let serial: String
        let model: String
        let firmware: String?
        let state: String?
        let status: String      // ok | idle | error
        let low: Bool
        let w: Double?
        let acV: Double?
        let dcV: Double?
        let dcA: Double?
        let tempC: Double?
        let lifetimeKwh: Double?
    }
    struct SystemInfo: Decodable {
        let model: String?
        let serial: String?
        let hw: String?
        let firmware: String?
        let state: String?
        let memKib: Double?
        let ctType: String?
    }
}

struct History: Decodable {
    let range: String?
    let readings: [Reading]
    let periodTotals: Totals?

    struct Reading: Decodable, Identifiable {
        var id: Double { t }
        let bucket: Double?
        let ts: Double?
        let productionKw: Double?
        let consumptionKw: Double?
        let batteryKw: Double?
        let netKw: Double?
        let batterySoc: Double?
        var t: Double { bucket ?? ts ?? 0 }
        var date: Date { Date(timeIntervalSince1970: t) }
    }
    struct Totals: Decodable {
        let solarKwh: Double?
        let homeKwh: Double?
        let batteryNetKwh: Double?
        let gridNetKwh: Double?
        let gridImportKwh: Double?
        let gridExportKwh: Double?
        let savingsDollars: Double?
        let co2Lbs: Double?
        let independencePct: Double?
    }
}

struct Outages: Decodable {
    let outages: [Outage]
    let summary: Summary
    let gridTracking: Bool?

    struct Outage: Decodable, Identifiable {
        var id: String { "\(kind)-\(start)" }
        let kind: String        // grid | pvs
        let start: Double
        let end: Double?
        let durationS: Double
        let ongoing: Bool
        let cause: String?
        let restartAt: Double?
    }
    struct Summary: Decodable {
        let pvs: Kind
        let grid: Kind
        struct Kind: Decodable { let count: Int; let totalS: Double }
    }
}

struct Tou: Decodable {
    let enabled: Bool
    let now: Period?
    let today: [Segment]?
    let todayLabel: String?
    let season: String?

    struct Segment: Decodable, Hashable {
        let name: String
        let rate: Double
        let start: Int
        let end: Int
    }
    struct Period: Decodable {
        let name: String
        let rate: Double
        let start: Int?
        let end: Int?
        let until: Double?
        let cheapest: Bool?
        let priciest: Bool?
        let next: Segment?
    }
}

struct Finance: Decodable {
    let headline: Headline?
    let rows: [Row]

    struct Headline: Decodable {
        let ytd: Ytd?
        let lifetime: Lifetime?
        let netCost12: Double?
        let bills12: Int?
    }
    struct Ytd: Decodable {
        let year: Int
        let saved: Double?
        let bills: Int
        let prev: Prev?
        struct Prev: Decodable { let year: Int; let saved: Double? }
    }
    struct Lifetime: Decodable {
        let since: String?
        let saved: Double?
        let perMonth: Double?
        let paid: Double?
        let noSolar: Double?
    }
    struct Row: Decodable, Identifiable {
        var id: String { key }
        let key: String
        let total: Double?
        let saved: Double?
        let noSolar: Double?
        let inProgress: Bool?
    }
}

struct SunVault: Decodable {
    let midstate: Int?
    let kind: String
    let since: Double?
    let batteryMode: String?
}

struct SettingsDoc: Decodable {
    let settings: [Item]
    let batteryMode: BatteryMode
    let status: Status

    struct Item: Decodable, Identifiable {
        var id: String { key }
        let key: String
        let group: String
        let label: String
        let type: String        // int | number | text | json
        let min: Double?
        let max: Double?
        let unit: String?
        let value: JSONValue
        let `default`: JSONValue
        let overridden: Bool
    }
    struct BatteryMode: Decodable {
        let value: String?
        let options: [Option]
        struct Option: Decodable, Hashable { let value: String; let label: String }
    }
    struct Status: Decodable {
        let mock: Bool
        let pvsConfigured: Bool
        let alerts: Bool
        let alertsFallback: Bool
        let upsTracking: Bool
        let utilityApi: Bool
        let refreshSecs: Int
    }
}

// A setting's value: a number or a string (the TOU plan is a JSON string).
enum JSONValue: Decodable, Encodable, Equatable {
    case number(Double), string(String), null

    init(from d: Decoder) throws {
        let c = try d.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else { self = .string(try c.decode(String.self)) }
    }
    func encode(to e: Encoder) throws {
        var c = e.singleValueContainer()
        switch self {
        case .number(let n): try c.encode(n)
        case .string(let s): try c.encode(s)
        case .null: try c.encodeNil()
        }
    }
    var text: String {
        switch self {
        case .number(let n): return n == n.rounded() && abs(n) < 1e9 ? String(Int(n)) : String(n)
        case .string(let s): return s
        case .null: return ""
        }
    }
}

struct APIError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

struct API {
    let base: URL

    static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    func get<T: Decodable>(_ path: String, _ type: T.Type = T.self) async throws -> T {
        try await send(URLRequest(url: url(path)))
    }

    func send<T: Decodable>(_ path: String, method: String, body: [String: Any?]) async throws -> T {
        var req = URLRequest(url: url(path))
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: body.mapValues { $0 ?? NSNull() })
        return try await send(req)
    }

    private func url(_ path: String) -> URL {
        URL(string: path, relativeTo: base)!.absoluteURL
    }

    private func send<T: Decodable>(_ req: URLRequest) async throws -> T {
        var req = req
        req.timeoutInterval = 30
        let (data, resp) = try await URLSession.shared.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(code) else {
            let msg = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["error"] as? String
            throw APIError(message: msg ?? "HTTP \(code)")
        }
        return try Self.decoder.decode(T.self, from: data)
    }
}
