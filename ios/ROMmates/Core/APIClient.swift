import Foundation

struct APIError: LocalizedError, Sendable {
    let statusCode: Int
    let message: String

    var errorDescription: String? { message }
}

struct ServerAddress {
    static func parse(_ value: String) throws -> URL {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        let candidate = trimmed.contains("://") ? trimmed : "https://\(trimmed)"
        guard
            let url = URL(string: candidate),
            url.scheme?.lowercased() == "https",
            url.host != nil,
            url.user == nil,
            url.password == nil,
            url.query == nil,
            url.fragment == nil
        else {
            throw APIError(
                statusCode: 0,
                message: "Enter your ROMmates public HTTPS address."
            )
        }
        return url
    }
}

struct APIClient: Sendable {
    private let baseURL: URL
    private let token: String?
    private let session: URLSession

    init(baseURL: URL, token: String? = nil, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.token = token
        self.session = session
    }

    func request<Response: Decodable & Sendable>(
        _ path: String,
        method: String = "GET",
        query: [URLQueryItem] = [],
        body: Data? = nil
    ) async throws -> Response {
        guard var components = URLComponents(
            url: baseURL.appending(path: path),
            resolvingAgainstBaseURL: false
        ) else {
            throw APIError(statusCode: 0, message: "The server address is invalid.")
        }
        if !query.isEmpty { components.queryItems = query }
        guard let url = components.url else {
            throw APIError(statusCode: 0, message: "The request address is invalid.")
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 30
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else {
            throw APIError(statusCode: 0, message: "The server returned an invalid response.")
        }
        guard (200..<300).contains(response.statusCode) else {
            let detail = (try? JSONDecoder.rommates.decode(ErrorBody.self, from: data).detail)
            throw APIError(
                statusCode: response.statusCode,
                message: detail ?? HTTPURLResponse.localizedString(forStatusCode: response.statusCode)
            )
        }
        do {
            return try JSONDecoder.rommates.decode(Response.self, from: data)
        } catch {
            throw APIError(statusCode: response.statusCode, message: "ROMmates returned data this app cannot read.")
        }
    }

    func data(_ path: String) async throws -> Data {
        guard let url = URL(string: path, relativeTo: baseURL)?.absoluteURL else {
            throw APIError(statusCode: 0, message: "The artwork address is invalid.")
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 30
        if let token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else {
            throw APIError(statusCode: 0, message: "The server returned an invalid response.")
        }
        guard (200..<300).contains(response.statusCode) else {
            throw APIError(statusCode: response.statusCode, message: "Artwork is unavailable.")
        }
        return data
    }

    func uploadChunk(_ path: String, data: Data, offset: Int64) async throws -> UploadSession {
        guard let url = URL(string: path, relativeTo: baseURL)?.absoluteURL else {
            throw APIError(statusCode: 0, message: "The upload address is invalid.")
        }
        var request = URLRequest(url: url)
        request.httpMethod = "PUT"
        request.timeoutInterval = 120
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        request.setValue(String(offset), forHTTPHeaderField: "Upload-Offset")
        if let token { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        let (responseData, response) = try await session.upload(for: request, from: data)
        guard let response = response as? HTTPURLResponse else {
            throw APIError(statusCode: 0, message: "The server returned an invalid response.")
        }
        guard (200..<300).contains(response.statusCode) else {
            throw APIError(statusCode: response.statusCode, message: "The upload chunk was not accepted.")
        }
        return try JSONDecoder.rommates.decode(UploadSession.self, from: responseData)
    }

    func encode<Body: Encodable>(_ body: Body) throws -> Data {
        try JSONEncoder.rommates.encode(body)
    }

    func absoluteURL(path: String) -> URL? {
        guard let relative = URL(string: path, relativeTo: baseURL) else { return nil }
        return relative.absoluteURL
    }

    private struct ErrorBody: Decodable { let detail: String }
}

extension JSONDecoder {
    static var rommates: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }
}

extension JSONEncoder {
    static var rommates: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        return encoder
    }
}
