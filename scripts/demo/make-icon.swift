// Draws a desktop icon: swift scripts/demo/make-icon.swift out.png [LABEL]   (default START)
import AppKit

let size = 1024.0
let image = NSImage(size: NSSize(width: size, height: size))
image.lockFocus()
let tile = NSRect(x: 100, y: 100, width: 824, height: 824)
NSBezierPath(roundedRect: tile, xRadius: 185, yRadius: 185).addClip()
NSGradient(colors: [NSColor(red: 0.36, green: 0.32, blue: 0.96, alpha: 1),
                    NSColor(red: 0.02, green: 0.17, blue: 0.26, alpha: 1)])!.draw(in: tile, angle: -90)
let flower = NSAttributedString(string: "🪻", attributes: [.font: NSFont.systemFont(ofSize: 430)])
flower.draw(at: NSPoint(x: (size - flower.size().width) / 2, y: 330))
let style = NSMutableParagraphStyle()
style.alignment = .center
let label = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : "START"
NSAttributedString(string: label, attributes: [
    .font: NSFont.systemFont(ofSize: 132, weight: .heavy),
    .foregroundColor: NSColor(red: 0.85, green: 1, blue: 0.2, alpha: 1),
    .paragraphStyle: style, .kern: 8,
]).draw(in: NSRect(x: 100, y: 165, width: 824, height: 170))
image.unlockFocus()
let png = NSBitmapImageRep(data: image.tiffRepresentation!)!.representation(using: .png, properties: [:])!
try! png.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
