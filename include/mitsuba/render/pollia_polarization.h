#pragma once

#include <mitsuba/render/mueller.h>
#include <drjit/complex.h>

NAMESPACE_BEGIN(mitsuba)
NAMESPACE_BEGIN(pollia)

/** General polarization algebra, shared by integrators, materials and sources.
 * Stokes V follows the coherency convention C01 = (U - i V) / 2.
 * Use Mitsuba's mueller::stokes_basis/rotate_stokes_basis for frame rotations.
 */
template <typename Value> using StokesVector = dr::Array<Value, 4>;

/// Jones transport from a light-side transverse basis to a camera-side basis.
template <typename Float> struct JonesMatrix {
    dr::Complex<Float> m00, m01, m10, m11;
};

template <typename Float> Float jones_m00(const JonesMatrix<Float> &j) {
    return .5f * (dr::squared_norm(j.m00) + dr::squared_norm(j.m01) +
                  dr::squared_norm(j.m10) + dr::squared_norm(j.m11));
}

/// Fresnel amplitudes expecting eta = n + i*kappa, with kappa >= 0:
/// the correct complex-index convention for Pollia's PolRAY polarization math.
/// cos_theta is the nonnegative incident cosine; n_imag supplies +kappa.
/// Preserve this sign. Mitsuba's complex fresnel_polarized conjugates eta to
/// n - i*kappa, so it is not a drop-in replacement for this convention.
template <typename Float>
std::pair<dr::Complex<Float>, dr::Complex<Float>>
fresnel_amplitudes(Float cos_theta, Float n_real, Float n_imag) {
    using Complex = dr::Complex<Float>;
    Complex eta(n_real, n_imag), c(cos_theta);
    Complex radicand = eta*eta - Complex(1.f - cos_theta*cos_theta);
    Complex root = dr::sqrt(radicand);
    auto on_cut = dr::real(radicand) < 0.f && dr::imag(radicand) == 0.f &&
                  dr::imag(root) > 0.f;
    root = dr::select(on_cut, dr::conj(root), root);
    Complex rs = (c-root)/(c+root);
    Complex eta2c = eta*eta*c;
    Complex rp = (eta2c-root)/(eta2c+root);
    return { rs, rp };
}

template <typename Float>
JonesMatrix<Float> multiply(const JonesMatrix<Float> &a, const JonesMatrix<Float> &b) {
    return { a.m00*b.m00 + a.m01*b.m10, a.m00*b.m01 + a.m01*b.m11,
             a.m10*b.m00 + a.m11*b.m10, a.m10*b.m01 + a.m11*b.m11 };
}

template <typename Float>
StokesVector<Float> pack_jones_row0(const JonesMatrix<Float> &j) {
    return { dr::real(j.m00), dr::imag(j.m00),
             dr::real(j.m01), dr::imag(j.m01) };
}

template <typename Float>
StokesVector<Float> pack_jones_row1(const JonesMatrix<Float> &j) {
    return { dr::real(j.m10), dr::imag(j.m10),
             dr::real(j.m11), dr::imag(j.m11) };
}

template <typename Float>
JonesMatrix<Float> unpack_jones(const StokesVector<Float> &a, const StokesVector<Float> &b) {
    using Complex = dr::Complex<Float>;
    return { Complex(a[0], a[1]), Complex(a[2], a[3]),
             Complex(b[0], b[1]), Complex(b[2], b[3]) };
}

template <typename Float>
StokesVector<Float> jones_first_column(const JonesMatrix<Float> &j) {
    auto ac = j.m00 * dr::conj(j.m10),
         bd = j.m01 * dr::conj(j.m11);
    Float a2 = dr::squared_norm(j.m00), b2 = dr::squared_norm(j.m01),
          c2 = dr::squared_norm(j.m10), d2 = dr::squared_norm(j.m11);
    return { .5f * (a2+b2+c2+d2), .5f * (a2+b2-c2-d2),
             dr::real(ac)+dr::real(bd), -dr::imag(ac)-dr::imag(bd) };
}

/// Apply a Jones operator using the coherency matrix of a Stokes state
template <typename Float>
StokesVector<Float> apply_jones(const JonesMatrix<Float> &j, const StokesVector<Float> &s) {
    using Complex = dr::Complex<Float>;
    Float c00 = .5f * (s[0]+s[1]), c11 = .5f * (s[0]-s[1]);
    Complex c01(.5f*s[2], -.5f*s[3]), c10 = dr::conj(c01);
    Complex jc00 = j.m00*c00 + j.m01*c10,
            jc01 = j.m00*c01 + j.m01*c11,
            jc10 = j.m10*c00 + j.m11*c10,
            jc11 = j.m10*c01 + j.m11*c11;
    Float out00 = dr::real(jc00*dr::conj(j.m00) +
                           jc01*dr::conj(j.m01));
    Float out11 = dr::real(jc10*dr::conj(j.m10) +
                           jc11*dr::conj(j.m11));
    Complex out01 = jc00*dr::conj(j.m10) + jc01*dr::conj(j.m11);
    return { out00+out11, out00-out11,
             2.f*dr::real(out01), -2.f*dr::imag(out01) };
}

template <typename Float>
StokesVector<Float> apply_jones_adjoint(const JonesMatrix<Float> &j, const StokesVector<Float> &s) {
    JonesMatrix<Float> adj = { dr::conj(j.m00), dr::conj(j.m10),
                             dr::conj(j.m01), dr::conj(j.m11) };
    return apply_jones(adj, s);
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
    return gamma * jones_to_mueller(j) + mueller::depolarizer(1.f-gamma);
}

/// Extract the emission Stokes column without collapsing spectral channels.
template <typename Spectrum>
StokesVector<unpolarized_spectrum_t<Spectrum>> source_stokes(const Spectrum &emission) {
    using Result = StokesVector<unpolarized_spectrum_t<Spectrum>>;
    if constexpr (is_polarized_v<Spectrum>)
        return { emission(0,0), emission(1,0), emission(2,0), emission(3,0) };
    else
        return dr::zeros<Result>();
}

/// Store a Stokes vector in Mitsuba's first-column spectrum representation.
template <typename Spectrum, typename Value>
Spectrum stokes_spectrum(const StokesVector<Value> &source) {
    if constexpr (is_polarized_v<Spectrum>) {
        Spectrum result = dr::zeros<Spectrum>();
        for (int i = 0; i < 4; ++i)
            result(i,0) = source[i];
        return result;
    } else {
        return source[0];
    }
}

/// PolRAY's planar PSG reference projected into an emitted ray's plane.
/// Unit normal/forward required. Mitsuba handles the subsequent basis rotation.
template <typename Vector3f>
Vector3f source_polarization_basis(const Vector3f &normal, const Vector3f &forward) {
    Vector3f y_raw = Vector3f(0.f, 1.f, 0.f) - normal.y() * normal;
    auto pole = dr::squared_norm(y_raw) <= 1e-10f;
    Vector3f x_fallback = Vector3f(1.f, 0.f, 0.f) - normal.x() * normal;
    Vector3f x_psg = dr::normalize(dr::select(pole, x_fallback,
                                             dr::cross(y_raw, normal)));
    Vector3f y_psg = dr::cross(normal, x_psg);
    Vector3f x_raw = x_psg - dr::dot(x_psg, forward) * forward;
    Vector3f y_projected = y_psg - dr::dot(y_psg, forward) * forward;
    return dr::normalize(dr::select(dr::squared_norm(x_raw) > 1e-10f, x_raw,
                                    dr::cross(y_projected, forward)));
}

/// Detached power-heuristic MIS weight, matching Mitsuba's path.cpp.
/// Hemisphere sampling/PDFs use Mitsuba's core/warp.h directly.
template <typename Float>
Float mis_weight(Float pdf_a, Float pdf_b) {
    pdf_a *= pdf_a;
    pdf_b *= pdf_b;
    Float w = pdf_a / (pdf_a + pdf_b);
    return dr::detach(dr::select(dr::isfinite(w), w, 0.f));
}

NAMESPACE_END(pollia)
NAMESPACE_END(mitsuba)
