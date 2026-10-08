#include <mitsuba/core/properties.h>
#include <mitsuba/core/warp.h>
#include <mitsuba/render/bsdf.h>
#include <mitsuba/render/emitter.h>
#include <mitsuba/render/integrator.h>
#include <mitsuba/render/pollia_jarecki.h>
#include <mitsuba/render/pollia_polarization.h>
#include <mitsuba/render/records.h>
#include <mitsuba/render/shape.h>

NAMESPACE_BEGIN(mitsuba)

/**
 * Full-Mueller control for pollia_pis. Geometry, scalar BSDF, emitter sampling,
 * MIS, and roulette match the PIS probe; only polarization transport differs.
 * A path carries a complete 4x4 Mueller product. The PIS event random draw is
 * consumed but not used, keeping subsequent ray samples paired.
 */
template <typename Float, typename Spectrum>
class PolliaMuellerIntegrator final : public MonteCarloIntegrator<Float, Spectrum> {
public:
    MI_IMPORT_BASE(MonteCarloIntegrator, m_max_depth, m_rr_depth)
    MI_IMPORT_TYPES(Scene, Sampler, Medium, Emitter, EmitterPtr, BSDF, BSDFPtr)

    using Stokes = pollia::StokesVector<Float>;
    using SpectralStokes = pollia::StokesVector<UnpolarizedSpectrum>;
    using Mueller = MuellerMatrix<Float>;
    using Jones = pollia::JonesMatrix<Float>;

    PolliaMuellerIntegrator(const Properties &props) : Base(props) {
        if constexpr (!is_polarized_v<Spectrum> ||
                      (!is_monochromatic_v<Spectrum> && !is_rgb_v<Spectrum>))
            Throw("pollia_mueller requires a monochrome or RGB polarized variant");
    }

    std::pair<Spectrum, Bool> sample(const Scene *scene, Sampler *sampler,
                                     const Ray3f &ray_, const Medium *,
                                     Bool active) const override {
        MI_MASKED_FUNCTION(ProfilerPhase::SamplingIntegratorSample, active);

        if constexpr (!is_polarized_v<Spectrum> ||
                      (!is_monochromatic_v<Spectrum> && !is_rgb_v<Spectrum>)) {
            return { Spectrum(0.f), false };
        } else {
            if (unlikely(m_max_depth == 0))
                return { Spectrum(0.f), false };

            // Match PIS's supported-scene check before entering the JIT loop.
            for (const auto &shape : scene->shapes()) {
                const BSDF *bsdf = shape->bsdf();
                if (!bsdf || !dr::all(bsdf->has_attribute(
                        std::string("pollia_jarecki_params"))))
                    Throw("pollia_mueller supports only pollia_jarecki surfaces");
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
                Mueller polarization;
                SpectralStokes result;
                UnpolarizedSpectrum beta;
                Bool valid_ray;
                Vertex vertex;
                Bool active;
                Sampler *sampler;
                DRJIT_STRUCT(LoopState, ray, pi, polarization, result, beta,
                             valid_ray, vertex, active, sampler)
            };

            LoopState ls {
                ray_, dr::zeros<PreliminaryIntersection3f>(),
                dr::identity<Mueller>(), dr::zeros<SpectralStokes>(), UnpolarizedSpectrum(1.f), false,
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
                        Float mis = pollia::mis_weight(s.vertex.pdf, em_pdf);
                        SpectralStokes source = pollia::source_stokes(emitter->eval(si));
                        s.result += (s.polarization * source) *
                                    dr::full<SpectralStokes>(s.beta * mis);
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

                    auto jareckiParams = pollia::jarecki_parameters(bsdf, si);
                    UnpolarizedSpectrum reflectance = bsdf->eval_attribute(
                        std::string("pollia_reflectance"), si);
                    Vector3f wo_camera = si.to_world(si.wi);
                    Vector3f normal(si.sh_frame.n);

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
                            jareckiParams);
                        Jones j = pollia::world_jones(eval, ds.d, wo_camera);
                        Mueller m = pollia::mixture_mueller(j, eval.gamma);
                        Float mis = dr::select(ds.delta, 1.f,
                                               pollia::mis_weight(ds.pdf, pdf_em_bsdf));
                        UnpolarizedSpectrum scalar = reflectance * dr::InvPi<Float> * cos_em;
                        SpectralStokes source = pollia::source_stokes(em_weight) *
                                               dr::full<SpectralStokes>(s.beta * scalar * mis);
                        dr::masked(s.result, active_em && eval.valid) +=
                            s.polarization * (m * source);
                    }

                    (void) s.sampler->next_1d(); // Match BSDF lobe draw.
                    Point2f sample2 = s.sampler->next_2d();
                    Vector3f local_light = warp::square_to_cosine_hemisphere(sample2);
                    Float bsdf_pdf = warp::square_to_cosine_hemisphere_pdf(local_light);
                    Vector3f wi_light = si.to_world(local_light);
                    auto eval = pollia::evaluate_jarecki(
                        wi_light, wo_camera, normal,
                        jareckiParams);
                    Jones j = pollia::world_jones(eval, wi_light, wo_camera);
                    Mueller m = pollia::mixture_mueller(j, eval.gamma);
                    (void) s.sampler->next_1d(); // Match PIS event draw.
                    s.polarization = s.polarization * m;
                    s.beta *= reflectance;

                    s.ray = si.spawn_ray(wi_light);
                    s.valid_ray |= si.is_valid();
                    s.vertex = Vertex { Interaction3f(si), bsdf_pdf, false,
                                        s.vertex.depth+1, +RayMask::Secondary };

                    Float rr_prob = dr::minimum(dr::max(s.beta), .95f);
                    Bool rr_active = s.vertex.depth >= m_rr_depth;
                    Bool rr_continue = s.sampler->next_1d() < rr_prob;
                    dr::masked(s.beta, rr_active) *= dr::rcp(dr::detach(rr_prob));
                    s.active = active_next && eval.valid && bsdf_pdf > 0.f &&
                               dr::max(s.beta) > 0.f && (!rr_active || rr_continue);
                    s.pi = scene->ray_intersect_preliminary(
                        s.ray, false, s.vertex.ray_mask, s.active,
                        jit_flag(JitFlag::LoopRecord));
                });

            Spectrum out = pollia::stokes_spectrum<Spectrum>(ls.result);
            return { dr::select(ls.valid_ray, out, Spectrum(0.f)), ls.valid_ray };
        }
    }

    std::string to_string() const override {
        return tfm::format("PolliaMuellerIntegrator[max_depth=%u, rr_depth=%u]",
                           m_max_depth, m_rr_depth);
    }

    MI_DECLARE_CLASS(PolliaMuellerIntegrator)
};

MI_EXPORT_PLUGIN(PolliaMuellerIntegrator)
NAMESPACE_END(mitsuba)
