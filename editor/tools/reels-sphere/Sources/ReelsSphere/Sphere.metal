#include <metal_stdlib>
using namespace metal;

// The CPU uses the same W4 SphereMath as the live viewer. Offline sampling is
// deliberately planar YCbCr: v360 interpolates chroma on its own half-size grid.
struct Uniforms {
    float3x3 matrix;
    float tanHalf;
    float aspect;
    int eac;
    int planet;
    int fullRange;
    float3x3 secondMatrix;
    float secondTanHalf;
    int proxy;
    int layout;
};

float4 cubic(float t) {
    float tt = t*t, ttt = tt*t;
    return float4(-t/3+tt/2-ttt/6, 1-t/2-tt+ttt/2,
                  t+tt/2-ttt/2, -t/6+ttt/6);
}

// swscale's default B=0 C=.6 bicubic for sphere._track_chain's horizontal scale.
float scaleWeight(float x) {
    x = abs(x);
    if (x < 1) return 1.4*x*x*x - 2.4*x*x + 1;
    if (x < 2) return -.6*x*x*x + 3*x*x - 4.8*x + 2.4;
    return 0;
}
float4 seamPixel(texture2d<float, access::read> t, int x, int y, int dx, int p, int o) {
    if (x < p) return t.read(uint2(dx+x, y));
    if (x >= p+o) return t.read(uint2(dx+x+o, y));
    float u = (x-p+.5f)/o;
    // blend=all_expr truncates, before the scale filter.
    return floor(mix(t.read(uint2(dx+x,y)), t.read(uint2(dx+x+o,y)), u)*255.f)/255.f;
}
float4 atlasPixel(texture2d<float, access::read> t, uint2 pos) {
    int h = t.get_height(), o = int(t.get_width())-3*h, g=o/2;
    int col = pos.x/h, x=pos.x%h;
    if (col == 1) return t.read(uint2(h+g+x,pos.y));
    int cs=h-g, p=(cs-o)/2, dx=col == 0 ? 0 : 2*h+g;
    float sx=(x+.5f)*cs/h-.5f;
    int base=floor(sx);
    float4 sum=0;
    float norm=0;
    for (int k=-1;k<=2;k++) {
        float weight=scaleWeight(sx-(base+k));
        sum+=weight*seamPixel(t,clamp(base+k,0,cs-1),pos.y,dx,p,o);
        norm+=weight;
    }
    return round(clamp(sum/norm,0.f,1.f)*255.f)/255.f;
}
kernel void assemble(texture2d<float, access::read> a [[texture(0)]],
                     texture2d<float, access::read> b [[texture(1)]],
                     texture2d<float, access::write> dst [[texture(2)]],
                     uint2 p [[thread_position_in_grid]]) {
    if (p.x>=dst.get_width() || p.y>=dst.get_height()) return;
    int h=a.get_height();
    dst.write(p.y<uint(h) ? atlasPixel(a,p) : atlasPixel(b,uint2(p.x,p.y-h)),p);
}

float2 eacPosition(float3 d, float2 size) {
    // Inverse of the proven atlas: top L/F/R, bottom D/B/U. The bottom
    // row's rotations come from vf_v360 prepare_eac_in, not a web face table.
    float3 ad=abs(d);
    float2 uv;
    int face;
    if (ad.y>max(ad.x,ad.z)) {
        if(d.y>0) { face=3; uv=float2(-d.z/d.y,-d.x/d.y); }
        else { face=5; uv=float2(-d.z/d.y,d.x/d.y); }
    } else if(ad.x>ad.z) {
        if(d.x>0) { face=2; uv=float2(-d.z/d.x,d.y/d.x); }
        else { face=0; uv=float2(-d.z/d.x,-d.y/d.x); }
    } else {
        if(d.z>0) { face=1; uv=float2(d.x/d.z,d.y/d.z); }
        else { face=4; uv=float2(d.y/d.z,d.x/d.z); }
    }
    uv=2.f/M_PI_F*atan(uv)+.5f;
    return float2((uv.x+face%3)*(size.x-4)/3+2,
                  uv.y*(size.y/2-4)+2+(face/3)*(size.y/2))-.5f;
}
float4 interpolate(texture2d<float, access::read> t, float2 uv, bool eac) {
    int2 base=int2(floor(uv));
    int w=t.get_width(), h=t.get_height();
    float4 wx=cubic(uv.x-base.x), wy=cubic(uv.y-base.y);
    float4 result=0;
    for(int y=0;y<4;y++) for(int x=0;x<4;x++) {
        int2 p=base+int2(x-1,y-1);
        if(eac) p=clamp(p,int2(0),int2(w-1,h-1));
        else {
            if(p.y<0 || p.y>=h) p.x+=w/2;
            p.x=(p.x%w+w)%w;
            p.y=clamp(p.y<0 ? -p.y : (p.y>=h ? 2*h-1-p.y : p.y),0,h-1);
        }
        // vf_v360 uses signed 14-bit kernel weights and truncates the result.
        float weight=rint(wx[x]*wy[y]*16385.f);
        result+=weight*round(t.read(uint2(p))*255.f);
    }
    return floor(clamp(floor(result/16384.f),0.f,255.f))/255.f;
}

// vf_v360 interp=linear: four signed 14-bit coefficients, rounded to
// nearest-even, then an integer shift (not a hardware bilinear sampler).
float4 interpolateLinear(texture2d<float, access::read> t, float2 uv, bool eac) {
    int2 base=int2(floor(uv));
    float2 f=uv-float2(base);
    int w=t.get_width(), h=t.get_height();
    int4 result=0;
    for(int y=0;y<2;y++) for(int x=0;x<2;x++) {
        int2 p=base+int2(x,y);
        if(eac) p=clamp(p,int2(0),int2(w-1,h-1));
        else {
            if(p.y<0 || p.y>=h) p.x+=w/2;
            p.x=(p.x%w+w)%w;
            p.y=clamp(p.y<0 ? -p.y : (p.y>=h ? 2*h-1-p.y : p.y),0,h-1);
        }
        int weight=int(rint((x ? f.x : 1-f.x)*(y ? f.y : 1-f.y)*16385.f));
        result+=weight*int4(round(t.read(uint2(p))*255.f));
    }
    return float4(clamp(result>>14,int4(0),int4(255)))/255.f;
}
kernel void reproject(texture2d<float, access::read> src [[texture(0)]],
                      texture2d<float, access::write> dst [[texture(1)]],
                      constant Uniforms &u [[buffer(0)]],
                      constant int &chroma [[buffer(1)]],
                      uint2 p [[thread_position_in_grid]]) {
    if(p.x>=dst.get_width() || p.y>=dst.get_height()) return;
    uint viewWidth=u.layout==2 ? dst.get_width()/2 : dst.get_width();
    bool second=u.layout==2 && p.x>=viewWidth;
    float2 position=float2(second ? p.x-viewWidth : p.x,p.y);
    float2 xy=2*(position+.5f)/float2(viewWidth,dst.get_height())-1;
    float3 d;
    if(u.layout==1) {
        float phi=xy.x*M_PI_F, theta=xy.y*(M_PI_F/2);
        d=float3(cos(theta)*sin(phi),sin(theta),cos(theta)*cos(phi));
        // Whole-sphere render: rotate the output sphere (levelling). Proxies keep the identity.
        if(!u.proxy) d=normalize(u.matrix*d);
    } else {
        float tanHalf=second ? u.secondTanHalf : u.tanHalf;
        xy*=float2(tanHalf,tanHalf*u.aspect);
        float3 ray;
        if(u.planet) {
            float r=length(xy), th=2*atan(r);
            ray=r==0 ? float3(0,0,1) : float3(xy/r*sin(th),cos(th));
        } else ray=normalize(float3(xy,1));
        d=normalize((second ? u.secondMatrix : u.matrix)*ray);
    }
    float2 size=float2(src.get_width(),src.get_height());
    float2 uv;
    if(u.eac) uv=eacPosition(d,size);
    else {
        float2 spherical=float2(atan2(d.x,d.z)/M_PI_F,asin(clamp(d.y,-1.f,1.f))/(M_PI_F/2));
        // v360's equirect input scale uses size-1, not texture pixel centres.
        uv=u.proxy ? (.5f*spherical+.5f)*(size-1) : (spherical+1)*.5f*size-.5f;
    }
    float4 value=u.proxy ? interpolateLinear(src,uv,u.eac) : interpolate(src,uv,u.eac);
    // Proxies preserve the source range, like the existing v360 proxy path.
    // W7 render outputs remain limited BT.709 for subsequent staging.
    if(u.fullRange && !u.proxy)
        value=chroma ? (value*224.f+16.f)/255.f : (value*219.f+16.f)/255.f;
    dst.write(value,p);
}
