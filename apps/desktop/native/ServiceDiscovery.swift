import Foundation

private struct ServiceDescription: Decodable {
    let apiURL: String
    enum CodingKeys: String, CodingKey { case apiURL = "api_url" }
}

/// 从用户选择的 Studio 读取 API 地址；显式填写 API 时直接使用，不发送设备凭证
struct ServiceDiscovery: Sendable {
    func resolve(studio: String, api: String) async throws -> ConnectionConfiguration {
        guard !studio.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw ClientError.message("请输入 Studio 地址")
        }
        let studioURL = try checkedServer(studio)
        if !api.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return try ConnectionConfiguration(studioURL: studioURL.absoluteString, apiURL: api).checked()
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.urlCache = nil
        configuration.timeoutIntervalForRequest = 15
        configuration.timeoutIntervalForResource = 15
        let session = URLSession(configuration: configuration, delegate: NoRedirects(), delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        var request = URLRequest(url: studioURL.appendingPathComponent("tinkerfin-connection.json"))
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        do {
            let (bytes, response) = try await session.bytes(for: request)
            guard let response = response as? HTTPURLResponse, response.statusCode == 200 else {
                throw ClientError.message("无法识别 API 地址，请展开填写")
            }
            var data = Data()
            for try await byte in bytes {
                guard data.count < 4096 else { throw ClientError.message("连接配置超过容量") }
                data.append(byte)
            }
            let description = try JSONDecoder().decode(ServiceDescription.self, from: data)
            return try ConnectionConfiguration(studioURL: studioURL.absoluteString, apiURL: description.apiURL).checked()
        } catch is CancellationError { throw CancellationError() }
        catch let error as ClientError { throw error }
        catch let error as URLError where error.code == .cancelled { throw CancellationError() }
        catch is DecodingError { throw ClientError.message("无法识别 API 地址，请展开填写") }
        catch { throw ClientError.message("无法连接到 Studio。请检查地址后重试。") }
    }
}
