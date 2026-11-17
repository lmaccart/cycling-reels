import Metal
import XCTest
import simd
@testable import ReelsEditor

/// Renders the live viewer's real shader offscreen and checks it samples the sphere
/// exactly where the CPU mapping (and so ffmpeg's v360) says it should.
final class SphereShaderTests: XCTestCase {
    func testShaderSamplesWhereV360Does() throws {
        guard let device = MTLCreateSystemDefaultDevice(), let queue = device.makeCommandQueue() else {
            throw XCTSkip("no Metal device")
        }
        // Source sphere: each texel stores its own (u, v).
        let sw = 512, sh = 256
        let srcDesc = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba32Float, width: sw, height: sh, mipmapped: false)
        let src = try XCTUnwrap(device.makeTexture(descriptor: srcDesc))
        var texels = [Float](repeating: 0, count: sw * sh * 4)
        for y in 0..<sh { for x in 0..<sw {
            let i = (y * sw + x) * 4
            texels[i] = (Float(x) + 0.5) / Float(sw)
            texels[i + 1] = (Float(y) + 0.5) / Float(sh)
            texels[i + 3] = 1
        } }
        src.replace(region: MTLRegionMake2D(0, 0, sw, sh), mipmapLevel: 0, withBytes: texels, bytesPerRow: sw * 16)

        let lib = try device.makeLibrary(source: SphereShader.source, options: nil)
        let pd = MTLRenderPipelineDescriptor()
        pd.vertexFunction = lib.makeFunction(name: "sphere_vertex")
        pd.fragmentFunction = lib.makeFunction(name: "sphere_fragment")
        pd.colorAttachments[0].pixelFormat = .rgba32Float
        let pipeline = try device.makeRenderPipelineState(descriptor: pd)

        let ow = 90, oh = 160
        let outDesc = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba32Float, width: ow, height: oh, mipmapped: false)
        outDesc.usage = [.renderTarget, .shaderRead]
        outDesc.storageMode = .shared
        let out = try XCTUnwrap(device.makeTexture(descriptor: outDesc))

        for (view, projection) in [(SphereView(yaw: 30, pitch: -20, fov: 100, roll: 15), SphereProjection.flat),
                                   (SphereView(yaw: 160, pitch: 25, fov: 100, roll: 90), .flat),
                                   (SphereView(yaw: 40, pitch: 0, fov: 100, roll: 0), .tinyPlanet)] {
            let eff = SphereMath.effectiveView(view, projection: projection)
            let stab = SphereMath.v360Matrix(yaw: 3, pitch: -2, roll: 5)
            let state = SphereRenderState(matrix: stab * SphereMath.matrix(for: eff), fov: eff.fov, projection: projection)
            let pass = MTLRenderPassDescriptor()
            pass.colorAttachments[0].texture = out
            pass.colorAttachments[0].loadAction = .clear
            pass.colorAttachments[0].storeAction = .store
            let cmd = try XCTUnwrap(queue.makeCommandBuffer())
            let enc = try XCTUnwrap(cmd.makeRenderCommandEncoder(descriptor: pass))
            var uniforms = SphereUniforms(state: state, drawableSize: CGSize(width: ow, height: oh))
            enc.setRenderPipelineState(pipeline)
            enc.setFragmentBytes(&uniforms, length: MemoryLayout<SphereUniforms>.stride, index: 0)
            enc.setFragmentTexture(src, index: 0)
            enc.drawPrimitives(type: .triangle, vertexStart: 0, vertexCount: 3)
            enc.endEncoding()
            cmd.commit()
            cmd.waitUntilCompleted()

            var pixels = [Float](repeating: 0, count: ow * oh * 4)
            out.getBytes(&pixels, bytesPerRow: ow * 16, from: MTLRegionMake2D(0, 0, ow, oh), mipmapLevel: 0)
            var checked = 0
            for y in stride(from: 2, to: oh - 2, by: 7) { for x in stride(from: 2, to: ow - 2, by: 5) {
                let want = sphereCPUMapping(state, u: (Double(x) + 0.5) / Double(ow), v: (Double(y) + 0.5) / Double(oh))
                // skip the wrap seam and the poles, where linear filtering blends across the edge
                guard want.u > 0.01, want.u < 0.99, want.v > 0.01, want.v < 0.99 else { continue }
                let i = (y * ow + x) * 4
                XCTAssertEqual(Double(pixels[i]), want.u, accuracy: 1.5 / Double(sw), "u at \(x),\(y) \(projection)")
                XCTAssertEqual(Double(pixels[i + 1]), want.v, accuracy: 1.5 / Double(sh), "v at \(x),\(y) \(projection)")
                checked += 1
            } }
            XCTAssertGreaterThan(checked, 200)
        }
    }
}
