import Foundation

/// 官方桌面发行清单的 Ed25519 公钥；私钥只由发布环境持有
enum UpdateTrust {
    static let publicKey = Data(base64Encoded: "nrRtJHJ5YDGCBYo04gmQXKjxEcITim3VAKkbmR6keSk=")!
}
