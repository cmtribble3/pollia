#include <metal_stdlib>
using namespace metal;

struct Uniforms {
    uint4 dimensions; // width, height, channel count, filter
    float4 settings; // exposure EV, analyzer degrees, range min, range max
    uint4 mode; // selected spectral channel, log scale, color map, reserved
    uint4 red, green, blue; // indices of I, Q, U, V in the interleaved film
};

float4 stokes(device const float *pixels, uint offset, uint4 channels) {
    return float4(pixels[offset + channels.x], pixels[offset + channels.y],
                  pixels[offset + channels.z], pixels[offset + channels.w]);
}

float scalar_value(float4 s, uint filter, float angle) {
    if (!all(isfinite(s))) return NAN;
    if (filter == 2) return s.x;
    if (filter == 3) {
        float a = angle * M_PI_F / 180;
        return 0.5 * (s.x + s.y * cos(2 * a) + s.z * sin(2 * a));
    }
    if (s.x <= 0) return NAN;
    // Common scaling cancels in the polarization formulas, preventing squared overflow.
    float scale = max(abs(s.y), max(abs(s.z), abs(s.w)));
    float3 p = scale > 0 ? s.yzw / scale : float3(0);
    if (filter == 4) return (scale / s.x) * length(p);
    if (filter == 5) return length(p.xy) > 0 ? 0.5 * atan2(p.y, p.x) * 180 / M_PI_F : NAN;
    // Signed minor/major axis ratio = tan(0.5 * atan2(V, hypot(Q, U))).
    // Mitsuba defines positive V as right-handed, negative V as left-handed.
    // The half-angle identity keeps the ratio in [-1, 1] without trig conversion.
    if (filter == 6) return scale > 0 ? p.z / (length(p) + length(p.xy)) : NAN;
    if (filter == 7) return s.y / s.x;
    if (filter == 8) return s.z / s.x;
    return s.w / s.x;
}

float3 srgb(float3 linear) {
    linear = saturate(linear);
    return select(1.055 * pow(linear, float3(1.0/2.4)) - 0.055, 12.92 * linear, linear <= 0.0031308);
}

kernel void display_frame(device const float *pixels [[buffer(0)]],
                          constant Uniforms &u [[buffer(1)]],
                          device float *values [[buffer(2)]],
                          texture2d<float, access::write> image [[texture(0)]],
                          uint2 xy [[thread_position_in_grid]]) {
    if (xy.x >= u.dimensions.x || xy.y >= u.dimensions.y) return;
    uint pixel = xy.y * u.dimensions.x + xy.x;
    uint offset = pixel * u.dimensions.z;
    uint filter = u.dimensions.w;
    float3 color;
    if (filter <= 1) {
        float4 r = stokes(pixels, offset, u.red), g = stokes(pixels, offset, u.green), b = stokes(pixels, offset, u.blue);
        float3 rgb = filter == 0 ? float3(r.x, g.x, b.x)
                                : float3(scalar_value(r, 3, u.settings.y), scalar_value(g, 3, u.settings.y), scalar_value(b, 3, u.settings.y));
        values[pixel] = rgb.x;
        color = all(isfinite(rgb)) ? srgb(rgb * exp2(u.settings.x)) : float3(0);
    } else {
        uint4 channel = u.mode.x == 0 ? u.red : (u.mode.x == 1 ? u.green : u.blue);
        float value = scalar_value(stokes(pixels, offset, channel), filter, u.settings.y);
        values[pixel] = value;
        float shown = value * ((filter == 2 || filter == 3) ? exp2(u.settings.x) : 1);
        float lo = u.settings.z, hi = u.settings.w;
        float t = (shown - lo) / max(hi - lo, 1e-20);
        if (u.mode.y && (filter == 2 || filter == 3 || filter == 4)) {
            if (lo > 0) t = (log(max(shown, lo)) - log(lo)) / (log(hi) - log(lo));
            else {
                float span = hi - lo, floor = span * 1e-3;
                t = (log(max(shown - lo, floor)) - log(floor)) / (log(span) - log(floor));
            }
        }
        color = isfinite(value) ? display_colormap(t, u.mode.z) : float3(0);
    }
    image.write(float4(color, 1), xy);
}

struct Vertex { float4 position [[position]]; float2 uv; };
vertex Vertex display_vertex(uint id [[vertex_id]]) {
    float2 p = float2((id << 1) & 2, id & 2);
    return {float4(p * 2 - 1, 0, 1), float2(p.x, 1 - p.y)};
}
fragment float4 display_fragment(Vertex in [[stage_in]], texture2d<float> image [[texture(0)]]) {
    constexpr sampler sample(filter::nearest, address::clamp_to_edge);
    return image.sample(sample, in.uv);
}
