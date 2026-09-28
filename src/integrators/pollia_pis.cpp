#include <mitsuba/core/properties.h>
#include <mitsuba/core/warp.h>
#include <mitsuba/render/bsdf.h>
#include <mitsuba/render/emitter.h>
#include <mitsuba/render/integrator.h>
#include <mitsuba/render/pollia_jarecki.h>
#include <mitsuba/render/records.h>
#include <mitsuba/render/shape.h>

NAMESPACE_BEGIN(mitsuba)

/**
 * Experimental Jones / ideal-depolarizer path tracer for pollia_jarecki.
 *
 * This is deliberately a narrow feasibility probe: monochrome polarized
 * variants, unpolarized emission, and diffuse Jarecki surfaces only. Each path
 * carries eight polarization scalars. Before the first sampled depolarizer
 * they encode a 2x2 complex Jones product; afterwards they encode the frozen
 * camera Stokes column and a source-side Stokes adjoint. The scalar throughput
 * and ray/vertex state are stored separately.
 */
template <typename Float, typename Spectrum>
class PolliaPISIntegrator final : public MonteCarloIntegrator<Float, Spectrum> {
public:
    MI_IMPORT_BASE(MonteCarloIntegrator, m_max_depth, m_rr_depth)
    MI_IMPORT_TYPES(Scene, Sampler, Medium, Emitter, EmitterPtr, BSDF, BSDFPtr)

    using Stokes = dr::Array<Float, 4>;
    using Jones = pollia::JonesMatrix<Float>;
    using Complex = dr::Complex<Float>;

    PolliaPISIntegrator(const Properties &props) : Base(props) {
        if constexpr (!is_polarized_v<Spectrum> ||
                      dr::size_v<UnpolarizedSpectrum> != 1)
            Throw("pollia_pis requires a mono_polarized variant");
    }

    static Stokes pack_row0(const Jones &j) {
        return { dr::real(j.m00), dr::imag(j.m00),
                 dr::real(j.m01), dr::imag(j.m01) };
    }

    static Stokes pack_row1(const Jones &j) {
        return { dr::real(j.m10), dr::imag(j.m10),
                 dr::real(j.m11), dr::imag(j.m11) };
    }

    static Jones unpack_jones(const Stokes &a, const Stokes &b) {
        return { Complex(a[0], a[1]), Complex(a[2], a[3]),
                 Complex(b[0], b[1]), Complex(b[2], b[3]) };
    }

    static Stokes first_column(const Jones &j) {
        auto ac = j.m00 * dr::conj(j.m10),
             bd = j.m01 * dr::conj(j.m11);
        Float a2 = dr::squared_norm(j.m00), b2 = dr::squared_norm(j.m01),
              c2 = dr::squared_norm(j.m10), d2 = dr::squared_norm(j.m11);
        return { .5f * (a2+b2+c2+d2), .5f * (a2+b2-c2-d2),
                 dr::real(ac)+dr::real(bd), -dr::imag(ac)-dr::imag(bd) };
    }

    // Apply a Jones operator through the coherency matrix without expanding
    // it to sixteen Mueller coefficients.
    static Stokes apply_jones(const Jones &j, const Stokes &s) {
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

    static Stokes apply_adjoint(const Jones &j, const Stokes &s) {
        Jones adj = { dr::conj(j.m00), dr::conj(j.m10),
                      dr::conj(j.m01), dr::conj(j.m11) };
        return apply_jones(adj, s);
    }

    static Float dot_stokes(const Stokes &a, const Stokes &b) {
        return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]+a[3]*b[3];
    }

    static Stokes emitted(const Stokes &a, const Stokes &b,
                          Bool depolarized, Float intensity) {
        Stokes coherent = first_column(unpack_jones(a, b)) * intensity;
        Stokes depol = a * (b[0] * intensity);
        return dr::select(depolarized, depol, coherent);
    }

    static Stokes direct(const Stokes &a, const Stokes &b,
                         Bool depolarized, const Jones &j, Float gamma,
                         Float intensity) {
        Jones acc = unpack_jones(a, b);
        Stokes coherent = (gamma * first_column(pollia::multiply(acc, j)) +
                           (1.f-gamma) * first_column(acc)) * intensity;
        Stokes mixed_source = gamma * first_column(j);
        mixed_source[0] += 1.f-gamma;
        Stokes depol = a * (dot_stokes(b, mixed_source) * intensity);
        return dr::select(depolarized, depol, coherent);
    }

    std::pair<Spectrum, Bool> sample(const Scene *scene, Sampler *sampler,
                                     const Ray3f &ray_, const Medium *,
                                     Bool active) const override {
        MI_MASKED_FUNCTION(ProfilerPhase::SamplingIntegratorSample, active);

        if constexpr (!is_polarized_v<Spectrum> ||
                      dr::size_v<UnpolarizedSpectrum> != 1) {
            return { Spectrum(0.f), false };
        } else {
            if (unlikely(m_max_depth == 0))
                return { Spectrum(0.f), false };

            // This prototype does not have a generic polarized BSDF fallback.
            // Validate concrete scene materials once, outside the Dr.Jit loop:
            // a per-lane polymorphic attribute query can report false for
            // inactive JIT lanes even in a scene of supported materials.
            for (const auto &shape : scene->shapes()) {
                const BSDF *bsdf = shape->bsdf();
                if (!bsdf || !dr::all(bsdf->has_attribute(
                        std::string("pollia_ab_xi"))))
                    Throw("pollia_pis supports only pollia_jarecki surfaces");
            }

            struct Vertex {
                Interaction3f si;
                Float pdf;
                Bool delta;
                UInt32 depth;
                UInt32 ray_mask;
                DRJIT_STRUCT(Vertex, si, pdf, delta, depth, ray_mask)
            };

            struct LoopState {
                Ray3f ray;
                PreliminaryIntersection3f pi;
                // The only persistent polarization payload: two float4s.
                Stokes pol0, pol1;
                Stokes result;
                Float beta;
                Bool depolarized;
                Bool valid_ray;
                Vertex vertex;
                Bool active;
                Sampler *sampler;
                DRJIT_STRUCT(LoopState, ray, pi, pol0, pol1, result, beta,
                             depolarized, valid_ray, vertex, active, sampler)
            };

            LoopState ls {
                ray_, dr::zeros<PreliminaryIntersection3f>(),
                Stokes(1.f, 0.f, 0.f, 0.f),
                Stokes(0.f, 0.f, 1.f, 0.f),
                Stokes(0.f), 1.f, false, false,
                Vertex { dr::zeros<Interaction3f>(), 1.f, true, 0,
                         +RayMask::Primary },
                active, sampler
            };
            ls.pi = scene->ray_intersect_preliminary(ls.ray, true,
                                                     +RayMask::Primary, ls.active);

            dr::tie(ls) = dr::while_loop(dr::make_tuple(ls),
                [](const LoopState &state) { return state.active; },
                [this, scene](LoopState &s) {
                    SurfaceInteraction3f si = scene->compute_surface_interaction(
                        s.ray, s.pi, +RayFlags::Default);
                    EmitterPtr emitter = si.emitter(scene, s.vertex.ray_mask);
                    Bool hit_emitter = emitter != nullptr;
                    s.valid_ray |= hit_emitter;
                    if (dr::any_or<true>(hit_emitter)) {
                        DirectionSample3f ds(scene, si, s.vertex.si,
                                             s.vertex.ray_mask);
                        Float em_pdf = 0.f;
                        if (dr::any_or<true>(!s.vertex.delta))
                            em_pdf = scene->pdf_emitter_direction(
                                s.vertex.si, ds, !s.vertex.delta);
                        Float mis = mis_weight(s.vertex.pdf, em_pdf);
                        Float intensity = emitter->eval(si)(0,0)[0];
                        s.result += emitted(s.pol0, s.pol1, s.depolarized,
                                            intensity * s.beta * mis);
                    }

                    if (dr::none_or<false>(si.is_valid())) {
                        s.active = false;
                        return;
                    }

                    BSDFPtr bsdf = si.bsdf();
                    Bool depth_ok = s.vertex.depth + 1 < m_max_depth;
                    Bool active_next = si.is_valid() && depth_ok;
                    if (dr::none_or<false>(active_next)) {
                        s.active = false;
                        return;
                    }

                    auto abxi = bsdf->eval_attribute_3(
                        std::string("pollia_ab_xi"), si);
                    auto n = bsdf->eval_attribute_3(
                        std::string("pollia_n"), si);
                    Float reflectance = bsdf->eval_attribute_1(
                        std::string("pollia_reflectance"), si);
                    Vector3f wo_camera = si.to_world(si.wi);
                    Vector3f normal(si.sh_frame.n);

                    // Match path.cpp's visible emitter sample and power MIS.
                    Bool active_em = active_next;
                    DirectionSample3f ds = dr::zeros<DirectionSample3f>();
                    Spectrum em_weight = dr::zeros<Spectrum>();
                    if (dr::any_or<true>(active_em)) {
                        std::tie(ds, em_weight) = scene->sample_emitter_direction(
                            si, s.sampler->next_2d(), true, active_em);
                        active_em &= ds.pdf != 0.f;
                    }
                    Vector3f local_em = si.to_local(ds.d);
                    Float cos_em = Frame3f::cos_theta(local_em);
                    active_em &= cos_em > 0.f;
                    Float pdf_em_bsdf = warp::square_to_cosine_hemisphere_pdf(local_em);
                    if (dr::any_or<true>(active_em)) {
                        auto eval = pollia::evaluate_jarecki(
                            ds.d, wo_camera, normal,
                            Float(abxi[0]), Float(abxi[1]), Float(n[0]),
                            Float(n[1]), Float(abxi[2]));
                        Jones j = pollia::world_jones(eval, ds.d, wo_camera);
                        Float mis = dr::select(ds.delta, 1.f,
                                               mis_weight(ds.pdf, pdf_em_bsdf));
                        Float scalar = reflectance * dr::InvPi<Float> * cos_em;
                        Float weight = s.beta * scalar * em_weight(0,0)[0] * mis;
                        dr::masked(s.result, active_em && eval.valid) +=
                            direct(s.pol0, s.pol1, s.depolarized, j,
                                   eval.gamma, weight);
                    }

                    // Cosine sampling cancels the Lambertian cos/pi factor.
                    (void) s.sampler->next_1d(); // Match the path integrator RNG schedule.
                    Point2f sample2 = s.sampler->next_2d();
                    Vector3f local_light = warp::square_to_cosine_hemisphere(sample2);
                    Float bsdf_pdf = warp::square_to_cosine_hemisphere_pdf(local_light);
                    Vector3f wi_light = si.to_world(local_light);
                    auto eval = pollia::evaluate_jarecki(
                        wi_light, wo_camera, normal,
                        Float(abxi[0]), Float(abxi[1]), Float(n[0]),
                        Float(n[1]), Float(abxi[2]));
                    Jones j = pollia::world_jones(eval, wi_light, wo_camera);
                    Bool draw_jones = s.sampler->next_1d() < eval.gamma;
                    Bool coherent_jones = !s.depolarized && draw_jones;
                    Bool first_depolarizer = !s.depolarized && !draw_jones;
                    Bool later_jones = s.depolarized && draw_jones;
                    Bool later_depolarizer = s.depolarized && !draw_jones;

                    Jones acc = unpack_jones(s.pol0, s.pol1);
                    Jones next = pollia::multiply(acc, j);
                    Stokes camera = first_column(acc);
                    Stokes adjoint = apply_adjoint(j, s.pol1);
                    dr::masked(s.pol0, coherent_jones) = pack_row0(next);
                    dr::masked(s.pol1, coherent_jones) = pack_row1(next);
                    dr::masked(s.pol0, first_depolarizer) = camera;
                    dr::masked(s.pol1, first_depolarizer) =
                        Stokes(1.f, 0.f, 0.f, 0.f);
                    dr::masked(s.pol1, later_jones) = adjoint;
                    dr::masked(s.pol1, later_depolarizer) =
                        Stokes(s.pol1[0], 0.f, 0.f, 0.f);
                    s.depolarized |= first_depolarizer;
                    s.beta *= reflectance;

                    s.ray = si.spawn_ray(wi_light);
                    s.valid_ray |= si.is_valid();
                    s.vertex = Vertex { Interaction3f(si), bsdf_pdf, false,
                                        s.vertex.depth+1, +RayMask::Secondary };

                    Float rr_prob = dr::minimum(s.beta, .95f);
                    Bool rr_active = s.vertex.depth >= m_rr_depth;
                    Bool rr_continue = s.sampler->next_1d() < rr_prob;
                    dr::masked(s.beta, rr_active) *= dr::rcp(dr::detach(rr_prob));
                    s.active = active_next && eval.valid && bsdf_pdf > 0.f &&
                               s.beta > 0.f && (!rr_active || rr_continue);
                    s.pi = scene->ray_intersect_preliminary(
                        s.ray, false, s.vertex.ray_mask, s.active,
                        jit_flag(JitFlag::LoopRecord));
                });

            Spectrum out = dr::zeros<Spectrum>();
            for (int i = 0; i < 4; ++i)
                out(i,0) = ls.result[i];
            return { dr::select(ls.valid_ray, out, Spectrum(0.f)), ls.valid_ray };
        }
    }

    static Float mis_weight(Float pdf_a, Float pdf_b) {
        pdf_a *= pdf_a;
        pdf_b *= pdf_b;
        Float w = pdf_a / (pdf_a + pdf_b);
        return dr::detach(dr::select(dr::isfinite(w), w, 0.f));
    }

    std::string to_string() const override {
        return tfm::format("PolliaPISIntegrator[max_depth=%u, rr_depth=%u]",
                           m_max_depth, m_rr_depth);
    }

    MI_DECLARE_CLASS(PolliaPISIntegrator)
};

MI_EXPORT_PLUGIN(PolliaPISIntegrator)
NAMESPACE_END(mitsuba)
