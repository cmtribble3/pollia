#pragma once

#include <mitsuba/render/mueller.h>
#include <drjit/complex.h>

NAMESPACE_BEGIN(mitsuba)
NAMESPACE_BEGIN(pollia)

/// Jones transport from a light-side transverse basis to a camera-side basis.
template <typename Float> struct JonesMatrix {
    dr::Complex<Float> m00, m01, m10, m11;
};

template <typename Float, typename Vector3f> struct JareckiEvaluation {
    JonesMatrix<Float> jones;
    Float gamma;
    Vector3f s_out;
    dr::mask_t<Float> valid;
};

template <typename Float> JonesMatrix<Float> identity_jones() {
    return { 1.f, 0.f, 0.f, 1.f };
}

template <typename Float> Float jones_m00(const JonesMatrix<Float> &j) {
    return .5f * (dr::squared_norm(j.m00) + dr::squared_norm(j.m01) +
                  dr::squared_norm(j.m10) + dr::squared_norm(j.m11));
}

template <typename Float> Float clamp_value(Float value, Float lower, Float upper) {
    return dr::minimum(dr::maximum(value, lower), upper);
}

template <typename Float>
JonesMatrix<Float> multiply(const JonesMatrix<Float> &a, const JonesMatrix<Float> &b) {
    return { a.m00*b.m00 + a.m01*b.m10, a.m00*b.m01 + a.m01*b.m11,
             a.m10*b.m00 + a.m11*b.m10, a.m10*b.m01 + a.m11*b.m11 };
}

template <typename Float>
JonesMatrix<Float> real_rotation(Float c, Float s) {
    return { c, -s, s, c };
}

/**
 * Evaluate PolRAY's Jarecki response. Both directions point away from the
 * surface: wi_light toward the light, wo_camera toward the sensor. n is the
 * oriented shading normal. The returned Jones operator is normalized to M00=1
 * in the local s/p bases, with gamma the coherent mixture probability.
 */
template <typename Float, typename Vector3f>
JareckiEvaluation<Float, Vector3f> evaluate_jarecki(
    const Vector3f &wi_light, const Vector3f &wo_camera, const Vector3f &n,
    Float A, Float B, Float n_real, Float n_imag, Float xi0_max) {
    using Complex = dr::Complex<Float>;
    using Mask = dr::mask_t<Float>;

    Vector3f h_raw = wi_light + wo_camera;
    Float h_len2 = dr::squared_norm(h_raw);
    Mask valid_h = h_len2 >= 1e-10f;
    Vector3f h = h_raw * dr::rsqrt(dr::maximum(h_len2, 1e-10f));

    Float cos_h = clamp_value<Float>(dr::dot(h, n), -1.f, 1.f);
    Float sin_h = dr::sqrt(dr::maximum(0.f, 1.f - cos_h*cos_h));
    Float cos_d = clamp_value<Float>(dr::dot(h, wo_camera), -1.f, 1.f);

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
    cos_phi = dr::select(phi_valid, clamp_value<Float>(dr::dot(half_plane, exit_plane), -1.f, 1.f), 1.f);
    sin_phi = dr::select(phi_valid, clamp_value<Float>(dr::dot(dr::cross(half_plane, exit_plane), h), -1.f, 1.f), 0.f);

    // Match PolRAY's complex Fresnel amplitude convention and branch cut.
    Complex eta(n_real, n_imag), c(cos_d), one(1.f);
    Complex radicand = eta*eta - Complex(1.f - cos_d*cos_d);
    Complex root = dr::sqrt(radicand);
    Mask on_cut = dr::real(radicand) < 0.f && dr::imag(radicand) == 0.f &&
                  dr::imag(root) > 0.f;
    root = dr::select(on_cut, dr::conj(root), root);
    Complex rs = (c-root)/(c+root);
    Complex eta2c = eta*eta*c;
    Complex rp = (eta2c-root)/(eta2c+root);
    Float rs2 = dr::squared_norm(rs), rp2 = dr::squared_norm(rp);
    Float gamma_raw = (rs2-rp2) / (rs2+rp2) * ((4.f*xi0_max-1.f)/3.f);
    Float gamma = clamp_value<Float>(gamma_raw, 0.f, 1.f);

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

template <typename Float>
MuellerMatrix<Float> jones_to_mueller(const JonesMatrix<Float> &j) {
    auto a = j.m00, b = j.m01, c = j.m10, d = j.m11;
    Float a2 = dr::squared_norm(a), b2 = dr::squared_norm(b),
          c2 = dr::squared_norm(c), d2 = dr::squared_norm(d);
    auto ab = a*dr::conj(b), cd = c*dr::conj(d),
         ac = a*dr::conj(c), bd = b*dr::conj(d),
         ad = a*dr::conj(d), bc = b*dr::conj(c);
    MuellerMatrix<Float> m = dr::zeros<MuellerMatrix<Float>>();
    m(0,0)=.5f*(a2+b2+c2+d2); m(0,1)=.5f*(a2-b2+c2-d2);
    m(0,2)=dr::real(ab)+dr::real(cd); m(0,3)=dr::imag(ab)+dr::imag(cd);
    m(1,0)=.5f*(a2+b2-c2-d2); m(1,1)=.5f*(a2-b2-c2+d2);
    m(1,2)=dr::real(ab)-dr::real(cd); m(1,3)=dr::imag(ab)-dr::imag(cd);
    m(2,0)=dr::real(ac)+dr::real(bd); m(2,1)=dr::real(ac)-dr::real(bd);
    m(2,2)=dr::real(ad)+dr::real(bc); m(2,3)=dr::imag(ad)-dr::imag(bc);
    m(3,0)=-dr::imag(ac)-dr::imag(bd); m(3,1)=-dr::imag(ac)+dr::imag(bd);
    m(3,2)=-dr::imag(ad)-dr::imag(bc); m(3,3)=dr::real(ad)-dr::real(bc);
    return m;
}

template <typename Float>
MuellerMatrix<Float> mixture_mueller(const JonesMatrix<Float> &j, Float gamma) {
    MuellerMatrix<Float> m = gamma * jones_to_mueller(j);
    m(0,0) += 1.f-gamma;
    return m;
}

NAMESPACE_END(pollia)
NAMESPACE_END(mitsuba)
