import AppKit

private struct IconBuildError: Error, CustomStringConvertible {
    let description: String
}

@MainActor
private func renderPNG(_ image: NSImage, pixels: Int) throws -> Data {
    guard let space = CGColorSpace(name: CGColorSpace.sRGB),
          let bitmap = CGContext(data: nil, width: pixels, height: pixels,
                                 bitsPerComponent: 8, bytesPerRow: 0, space: space,
                                 bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
        throw IconBuildError(description: "无法创建 \(pixels) 像素图标画布")
    }
    let side = CGFloat(pixels)
    // 沿用 512 像素方形画布中 456 像素宽的主标比例，四周保持透明
    let scale = side * (456.0 / 512.0) / max(image.size.width, image.size.height)
    let artwork = NSSize(width: image.size.width * scale, height: image.size.height * scale)
    let frame = NSRect(x: (side - artwork.width) / 2, y: (side - artwork.height) / 2,
                       width: artwork.width, height: artwork.height)
    bitmap.clear(CGRect(x: 0, y: 0, width: side, height: side))
    NSGraphicsContext.saveGraphicsState()
    defer { NSGraphicsContext.restoreGraphicsState() }
    NSGraphicsContext.current = NSGraphicsContext(cgContext: bitmap, flipped: false)
    image.draw(in: frame, from: .zero, operation: .copy, fraction: 1,
               respectFlipped: false, hints: [.interpolation: NSImageInterpolation.high])
    guard let rendered = bitmap.makeImage(),
          let data = NSBitmapImageRep(cgImage: rendered).representation(using: .png, properties: [:]) else {
        throw IconBuildError(description: "无法编码 \(pixels) 像素图标")
    }
    return data
}

@MainActor
private func buildIcon(source: URL, output: URL) throws {
    guard source.pathExtension == "svg", let image = NSImage(contentsOf: source),
          image.size.width > 0, image.size.height > 0 else {
        throw IconBuildError(description: "无法读取 SVG 品牌源：\(source.path)")
    }
    guard output.pathExtension == "icns" else {
        throw IconBuildError(description: "输出路径必须使用 .icns 扩展名")
    }
    let manager = FileManager.default
    let temporary = output.deletingLastPathComponent()
        .appendingPathComponent(".tinkerfin-icon-\(UUID().uuidString)", isDirectory: true)
    try manager.createDirectory(at: temporary, withIntermediateDirectories: false)
    defer { try? manager.removeItem(at: temporary) }
    let iconset = temporary.appendingPathComponent("TinkerFin.iconset", isDirectory: true)
    try manager.createDirectory(at: iconset, withIntermediateDirectories: false)
    for size in [16, 32, 128, 256, 512] {
        for scale in [1, 2] {
            let suffix = scale == 1 ? "" : "@2x"
            let filename = "icon_\(size)x\(size)\(suffix).png"
            try renderPNG(image, pixels: size * scale).write(to: iconset.appendingPathComponent(filename))
        }
    }
    let generated = temporary.appendingPathComponent("TinkerFin.icns")
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
    process.arguments = ["-c", "icns", "-o", generated.path, iconset.path]
    try process.run()
    process.waitUntilExit()
    guard process.terminationStatus == 0 else {
        throw IconBuildError(description: "iconutil 生成失败：\(process.terminationStatus)")
    }
    try Data(contentsOf: generated).write(to: output, options: .atomic)
}

do {
    guard CommandLine.arguments.count == 3 else {
        throw IconBuildError(description: "用法：render_app_icon.swift <品牌 SVG> <输出 ICNS>")
    }
    try buildIcon(source: URL(fileURLWithPath: CommandLine.arguments[1]),
                  output: URL(fileURLWithPath: CommandLine.arguments[2]))
} catch {
    FileHandle.standardError.write(Data("生成应用图标失败：\(error)\n".utf8))
    exit(1)
}
