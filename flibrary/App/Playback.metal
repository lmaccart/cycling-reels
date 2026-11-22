#include <metal_stdlib>
using namespace metal;

// Flat view of a levelled 360 frame, drawn by a fragment pass straight into the MTKView drawable (or an offscreen texture for readback). `m` = levelling * view: ray in the source's own sphere.
// eac: the two GoPro EAC track textures (a over b), seam blend + scale as in scripts/reels/sphere.py _track_chain,
// face layout as in the reels-sphere helper's eacPosition. Otherwise `a` is an already levelled equirect.
struct PlayUniforms {
    float3x3 m;
    float tanHalf;
    float aspect;
    int eac;
    int face;
    int overlap;
};

// The v360 EAC layout: top row L/F/R, bottom D/B/U (same as reels-sphere Sphere.metal).
static float3 eacFace(float3 d, thread int &face) {
    float3 ad = abs(d);
    float2 uv;
    if (ad.y > max(ad.x, ad.z)) {
        if (d.y > 0) { face = 3; uv = float2(-d.z / d.y, -d.x / d.y); }
        else { face = 5; uv = float2(-d.z / d.y, d.x / d.y); }
    } else if (ad.x > ad.z) {
        if (d.x > 0) { face = 2; uv = float2(-d.z / d.x, d.y / d.x); }
        else { face = 0; uv = float2(-d.z / d.x, -d.y / d.x); }
    } else {
        if (d.z > 0) { face = 1; uv = float2(d.x / d.z, d.y / d.z); }
        else { face = 4; uv = float2(d.y / d.z, d.x / d.z); }
    }
    return float3(2.f / M_PI_F * atan(uv) + .5f, 0);
}

// One raw pixel of the strip a side face is cut from: the seam band is the blend of two copies.
static float4 strip(texture2d<float, access::read> t, int q, int y, int dx, int p, int o) {
    if (q < p) return t.read(uint2(dx + q, y));
    if (q >= p + o) return t.read(uint2(dx + q + o, y));
    return mix(t.read(uint2(dx + q, y)), t.read(uint2(dx + q + o, y)), (q - p + .5f) / o);
}

static float4 eacSample(texture2d<float, access::read> a, texture2d<float, access::read> b, float3 d, int h, int o) {
    int face;
    float2 uv = eacFace(d, face).xy;
    int col = face % 3, row = face / 3;
    float ax = (uv.x + col) * (3 * h - 4) / 3.f + 1.5f - col * h;   // pixel index inside the face
    float ay = uv.y * (h - 4) + 1.5f;                              // pixel index inside the track
    ax = clamp(ax, 0.f, h - 1.f); ay = clamp(ay, 0.f, h - 1.f);
    texture2d<float, access::read> t = row == 0 ? a : b;
    int g = o / 2, cs = h - g, p = (cs - o) / 2;
    int y0 = int(ay), y1 = min(y0 + 1, h - 1);
    float fy = ay - y0;
    float4 c0, c1;
    if (col == 1) {
        int x0 = int(ax), x1 = min(x0 + 1, h - 1);
        float fx = ax - x0;
        c0 = mix(t.read(uint2(h + g + x0, y0)), t.read(uint2(h + g + x1, y0)), fx);
        c1 = mix(t.read(uint2(h + g + x0, y1)), t.read(uint2(h + g + x1, y1)), fx);
    } else {
        int dx = col == 0 ? 0 : 2 * h + g;
        float sx = clamp((ax + .5f) * cs / h - .5f, 0.f, cs - 1.f);
        int q0 = int(sx), q1 = min(q0 + 1, cs - 1);
        float fx = sx - q0;
        c0 = mix(strip(t, q0, y0, dx, p, o), strip(t, q1, y0, dx, p, o), fx);
        c1 = mix(strip(t, q0, y1, dx, p, o), strip(t, q1, y1, dx, p, o), fx);
    }
    return mix(c0, c1, fy);
}

struct VOut {
    float4 pos [[position]];
    float2 xy;   // -1...1 across the viewport, y down
};

vertex VOut viewVert(uint vid [[vertex_id]]) {
    float2 p = float2((vid << 1) & 2, vid & 2);   // one big triangle: (0,0) (2,0) (0,2)
    VOut o;
    o.pos = float4(p * 2 - 1, 0, 1);
    o.xy = float2(p.x * 2 - 1, 1 - p.y * 2);
    return o;
}

fragment float4 viewFrag(VOut in [[stage_in]],
                         texture2d<float, access::read> a [[texture(0)]],
                         texture2d<float, access::read> b [[texture(1)]],
                         constant PlayUniforms &u [[buffer(0)]]) {
    float3 d = normalize(u.m * normalize(float3(in.xy * float2(u.tanHalf, u.tanHalf * u.aspect), 1)));
    float4 v;
    if (u.eac) v = eacSample(a, b, d, u.face, u.overlap);
    else {
        float2 sph = float2(atan2(d.x, d.z) / M_PI_F, asin(clamp(d.y, -1.f, 1.f)) / (M_PI_F / 2));
        // bilinear by hand: x wraps, y clamps
        float2 size = float2(a.get_width(), a.get_height());
        float2 pos = (sph + 1) * .5f * size - .5f;
        int2 i0 = int2(floor(pos)); float2 f = pos - float2(i0);
        int w = int(size.x), h = int(size.y);
        int xa = ((i0.x % w) + w) % w, xb = (xa + 1) % w;
        int ya = clamp(i0.y, 0, h - 1), yb = clamp(i0.y + 1, 0, h - 1);
        v = mix(mix(a.read(uint2(xa, ya)), a.read(uint2(xb, ya)), f.x), mix(a.read(uint2(xa, yb)), a.read(uint2(xb, yb)), f.x), f.y);
    }
    return float4(v.rgb, 1);
}
