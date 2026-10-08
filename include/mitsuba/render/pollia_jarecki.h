#pragma once

#include <mitsuba/render/pollia_polarization.h>

NAMESPACE_BEGIN(mitsuba)
NAMESPACE_BEGIN(pollia)

/// Packed material parameters: [a, b, n, kappa, xi0_max].
template <typename Float> using JareckiParams = dr::Array<Float, 5>;

/// Mitsuba's BSDF attributes expose one or three values. Assemble the two
/// native attribute queries into the same five-value array for both integrators.
template <typename BSDFPtr, typename SurfaceInteraction>
auto jarecki_parameters(const BSDFPtr &bsdf, const SurfaceInteraction &si) {
    auto parameters = bsdf->eval_attribute_3(std::string("pollia_jarecki_params"), si);
    auto opticalConstants = bsdf->eval_attribute_3(std::string("pollia_optical_constants"), si);
    using Float = dr::value_t<decltype(parameters)>;
    return JareckiParams<Float>(parameters[0], parameters[1],
                                opticalConstants[0], opticalConstants[1], parameters[2]);
}

template <typename Float, typename Vector3f> struct JareckiEvaluation {
    JonesMatrix<Float> jones;
    Float gamma;
    Vector3f s_out;
    dr::mask_t<Float> valid;
};

/**
 * Evaluate PolRAY's Jarecki response. Both directions point away from the
 * surface: wi_light toward the light, wo_camera toward the sensor. n is the
 * oriented shading normal. The returned Jones operator is normalized to M00=1
 * in the local s/p bases, with gamma the coherent mixture probability.
 */
template <typename Float, typename Vector3f>
JareckiEvaluation<Float, Vector3f> evaluate_jarecki(
    const Vector3f &wi_light, const Vector3f &wo_camera, const Vector3f &n,
    const JareckiParams<Float> &jareckiParams) {
    Float A = jareckiParams[0], B = jareckiParams[1],
          n_real = jareckiParams[2], n_imag = jareckiParams[3],
          xi0_max = jareckiParams[4];
    using Complex = dr::Complex<Float>;
    using Mask = dr::mask_t<Float>;

    Vector3f h_raw = wi_light + wo_camera;
    Float h_len2 = dr::squared_norm(h_raw);
    Mask valid_h = h_len2 >= 1e-10f;
    Vector3f h = h_raw * dr::rsqrt(dr::maximum(h_len2, 1e-10f));

    Float cos_h = dr::clip(dr::dot(h, n), -1.f, 1.f);
    Float sin_h = dr::sqrt(dr::maximum(0.f, 1.f - cos_h*cos_h));
    Float cos_d = dr::clip(dr::dot(h, wo_camera), -1.f, 1.f);

    Vector3f s_raw = dr::cross(wo_camera, h);
    Float s_len2 = dr::squared_norm(s_raw);
    Mask retro = s_len2 < 1e-10f;
    Vector3f fallback = mueller::stokes_basis(wo_camera);
    Vector3f s_out = dr::select(retro, fallback,
                                s_raw * dr::rsqrt(dr::maximum(s_len2, 1e-10f)));

    Float cos_phi = 1.f, sin_phi = 0.f;
    Vector3f half_plane_raw = dr::cross(h, n);
    Float half_plane_len2 = dr::squared_norm(half_plane_raw);
    Vector3f half_plane = half_plane_raw * dr::rsqrt(dr::maximum(half_plane_len2, 1e-10f));
    Vector3f exit_plane = -s_out;
    Mask phi_valid = half_plane_len2 >= 1e-10f && !retro;
    cos_phi = dr::select(phi_valid, dr::clip(dr::dot(half_plane, exit_plane), -1.f, 1.f), 1.f);
    sin_phi = dr::select(phi_valid, dr::clip(dr::dot(dr::cross(half_plane, exit_plane), h), -1.f, 1.f), 0.f);

    auto [rs, rp] = fresnel_amplitudes(cos_d, n_real, n_imag);
    Float rs2 = dr::squared_norm(rs), rp2 = dr::squared_norm(rp);
    Float gamma_raw = (rs2-rp2) / (rs2+rp2) * ((4.f*xi0_max-1.f)/3.f);
    Float gamma = dr::clip(gamma_raw, 0.f, 1.f);

    Float diffuse = A * dr::pow(sin_h, B);
    Float cc = cos_phi*cos_phi, cs = cos_phi*sin_phi, ss = sin_phi*sin_phi;
    JonesMatrix<Float> j = { rs + Complex(diffuse*cc), Complex(diffuse*cs),
                             Complex(-diffuse*cs), rp - Complex(diffuse*ss) };
    Float m00 = jones_m00(j);
    Float scale = dr::rsqrt(dr::maximum(m00, 1e-30f));
    j = { j.m00*scale, j.m01*scale, j.m10*scale, j.m11*scale };
    // Exact retroreflection limit in PolRAY is an ideal depolarizer.
    j.m00 = dr::select(retro, Complex(1.f), j.m00);
    j.m01 = dr::select(retro, Complex(0.f), j.m01);
    j.m10 = dr::select(retro, Complex(0.f), j.m10);
    j.m11 = dr::select(retro, Complex(1.f), j.m11);
    gamma = dr::select(retro, 0.f, gamma);
    Mask valid = valid_h && dr::isfinite(m00) && m00 > 0.f &&
                 dr::isfinite(gamma_raw) && dr::isfinite(scale);
    valid |= valid_h && retro;
    return { j, gamma, s_out, valid };
}

/**
 * Express local Jones response in Mitsuba's implicit input/output bases.
 * The returned Jones operator is in the coordinate system of the supplied
 * vectors: pass world-space directions for world implicit bases, or local
 * directions for the BSDF's local implicit bases.
 */
template <typename Float, typename Vector3f>
JonesMatrix<Float> world_jones(const JareckiEvaluation<Float, Vector3f> &eval,
                               const Vector3f &wi_light, const Vector3f &wo_camera) {
    Vector3f in_forward = -wi_light, out_forward = wo_camera;
    Vector3f s = eval.s_out;
    Vector3f p_in = dr::cross(in_forward, s), p_out = dr::cross(out_forward, s);
    Vector3f x_in = mueller::stokes_basis(in_forward);
    Vector3f y_in = dr::cross(in_forward, x_in);
    Vector3f x_out = mueller::stokes_basis(out_forward);
    Vector3f y_out = dr::cross(out_forward, x_out);

    JonesMatrix<Float> input = { dr::dot(s,x_in), dr::dot(s,y_in),
                                 dr::dot(p_in,x_in), dr::dot(p_in,y_in) };
    JonesMatrix<Float> output = { dr::dot(x_out,s), dr::dot(x_out,p_out),
                                  dr::dot(y_out,s), dr::dot(y_out,p_out) };
    return multiply(output, multiply(eval.jones, input));
}

NAMESPACE_END(pollia)
NAMESPACE_END(mitsuba)
