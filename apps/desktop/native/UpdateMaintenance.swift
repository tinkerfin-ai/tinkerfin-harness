import Foundation

struct MaintenanceCommand: Encodable, Sendable {
    enum Kind: String, Encodable, Sendable {
        case prepare = "maintenance_prepare", cancel = "maintenance_cancel", commit = "maintenance_commit"
    }
    let kind: Kind
    let requestID: String
    enum CodingKeys: String, CodingKey { case kind; case requestID = "request_id" }
}

struct MaintenanceReceipt: Decodable, Sendable {
    enum State: String, Decodable, Sendable { case ready, cancelled, failed, committed }
    let requestID: String
    let state: State
    enum CodingKeys: String, CodingKey { case requestID = "request_id", state }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        requestID = try values.decode(String.self, forKey: .requestID)
        guard requestID.range(of: "^[0-9a-f]{32}$", options: .regularExpression) != nil else {
            throw DecodingError.dataCorruptedError(forKey: .requestID, in: values, debugDescription: "维护请求身份无效")
        }
        state = try values.decode(State.self, forKey: .state)
    }
}

/// 进程控制回执由管道所有者结算，业务事件才交给客户端状态模型
enum HostMessage: Decodable, Sendable {
    case event(HostEvent), maintenance(MaintenanceReceipt)
    private enum CodingKeys: String, CodingKey { case kind }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        if try values.decode(String.self, forKey: .kind) == "maintenance" { self = .maintenance(try MaintenanceReceipt(from: decoder)) }
        else { self = .event(try HostEvent(from: decoder)) }
    }
}
