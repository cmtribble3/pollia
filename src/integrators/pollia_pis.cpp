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
 * Experimental Jones / ideal-depolarizer path tracer for pollia_jarecki.
 *
 * This is deliberately a narrow feasibility probe: monochrome and RGB polarized
 * variants, polarized emission, and diffuse Jarecki surfaces only. Each channel
 * carries eight polarization scalars. Before the first sampled depolarizer
 * they encode a 2x2 complex Jones product; afterwards they encode the frozen
 * camera Stokes column and a source-side Stokes adjoint. The scalar throughput
 * and ray/vertex state are stored separately. RGB channels share geometry and
 * an event random draw, with independent Jones/depolarizer states, mixture
 * probabilities, throughput and Stokes results.
 */
template <typename Float, typename Spectrum>
class PolliaPISIntegrator final : public MonteCarloIntegrator<Float, Spectrum> {
public:
    MI_IMPORT_BASE(MonteCarloIntegrator, m_max_depth, m_rr_depth)
    MI_IMPORT_TYPES(Scene, Sampler, Medium, Emitter, EmitterPtr, BSDF, BSDFPtr)

    static constexpr size_t Channels = dr::size_v<UnpolarizedSpectrum>;
    using Polarization = dr::Array<pollia::StokesVector<Float>, Channels>;
    using ChannelMask = dr::Array<Bool, Channels>;
    using Parameters = dr::Array<pollia::JareckiParams<Float>, Channels>;
    using Stokes = pollia::StokesVector<Float>;
    using SpectralStokes = pollia::StokesVector<UnpolarizedSpectrum>;
    using Jones = pollia::JonesMatrix<Float>;

    PolliaPISIntegrator(const Properties &props) : Base(props) {
        if constexpr (!is_polarized_v<Spectrum> ||
                      (!is_monochromatic_v<Spectrum> && !is_rgb_v<Spectrum>))
            Throw("pollia_pis requires a monochrome or RGB polarized variant");
    }

    static Stokes emitted(const Stokes &a, const Stokes &b,
                          Bool depolarized, const Stokes &source) {
        Stokes coherent = pollia::apply_jones(pollia::unpack_jones(a, b), source);
        Stokes depol = a * dr::dot(b, source);
        return dr::select(depolarized, depol, coherent);
    }

    static Stokes direct(const Stokes &a, const Stokes &b,
                         Bool depolarized, const Jones &j, Float gamma,
                         const Stokes &source) {
        Stokes mixed = gamma * pollia::apply_jones(j, source);
        mixed[0] += (1.f-gamma) * source[0];
        return emitted(a, b, depolarized, mixed);
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

            // This prototype does not have a generic polarized BSDF fallback.
            // Validate concrete scene materials once, outside the Dr.Jit loop:
            // a per-lane polymorphic attribute query can report false for
            // inactive JIT lanes even in a scene of supported materials.
            for (const auto &shape : scene->shapes()) {
                const BSDF *bsdf = shape->bsdf();
                if (!bsdf || !dr::all(bsdf->has_attribute(
                        std::string("pollia_jarecki_params"))))
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
                // Two float4s per channel, with a channel-specific depolarization flag.
                Polarization pol0, pol1;
                SpectralStokes result;
                UnpolarizedSpectrum beta;
                ChannelMask depolarized;
                Bool valid_ray;
                Vertex vertex;
                Bool active;
                Sampler *sampler;
                DRJIT_STRUCT(LoopState, ray, pi, pol0, pol1, result, beta,
                             depolarized, valid_ray, vertex, active, sampler)
            };

            LoopState ls {
                ray_, dr::zeros<PreliminaryIntersection3f>(),
                Polarization(Stokes(1.f, 0.f, 0.f, 0.f)),
                Polarization(Stokes(0.f, 0.f, 1.f, 0.f)),
                dr::zeros<SpectralStokes>(), UnpolarizedSpectrum(1.f), ChannelMask(false), false,
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
                        for (size_t c = 0; c < Channels; ++c) {
                            Stokes input(source[0][c], source[1][c], source[2][c], source[3][c]);
                            Stokes output = emitted(s.pol0[c], s.pol1[c], s.depolarized[c], input);
                            for (int i = 0; i < 4; ++i)
                                s.result[i][c] += output[i] * s.beta[c] * mis;
                        }
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

                    Parameters parameters;
                    for (size_t c = 0; c < Channels; ++c)
                        parameters[c] = pollia::jarecki_parameters(bsdf, si, c);
                    UnpolarizedSpectrum reflectance = bsdf->eval_attribute(
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
                        Float mis = dr::select(ds.delta, 1.f,
                                               pollia::mis_weight(ds.pdf, pdf_em_bsdf));
                        UnpolarizedSpectrum scalar = reflectance * dr::InvPi<Float> * cos_em;
                        SpectralStokes source = pollia::source_stokes(em_weight) *
                                               dr::full<SpectralStokes>(s.beta * scalar * mis);
                        for (size_t c = 0; c < Channels; ++c) {
                            auto eval = pollia::evaluate_jarecki(ds.d, wo_camera, normal, parameters[c]);
                            Jones j = pollia::world_jones(eval, ds.d, wo_camera);
                            Stokes input(source[0][c], source[1][c], source[2][c], source[3][c]);
                            Stokes output = direct(s.pol0[c], s.pol1[c], s.depolarized[c], j,
                                                   eval.gamma, input);
                            for (int i = 0; i < 4; ++i)
                                dr::masked(s.result[i][c], active_em && eval.valid) += output[i];
                        }
                    }

                    // Cosine sampling cancels the Lambertian cos/pi factor.
                    (void) s.sampler->next_1d(); // Match the path integrator RNG schedule.
                    Point2f sample2 = s.sampler->next_2d();
                    Vector3f local_light = warp::square_to_cosine_hemisphere(sample2);
                    Float bsdf_pdf = warp::square_to_cosine_hemisphere_pdf(local_light);
                    Vector3f wi_light = si.to_world(local_light);
                    // One uniform draw, each channel's own mixture probability/history.
                    Float event = s.sampler->next_1d();
                    for (size_t c = 0; c < Channels; ++c) {
                        auto eval = pollia::evaluate_jarecki(wi_light, wo_camera, normal, parameters[c]);
                        Jones j = pollia::world_jones(eval, wi_light, wo_camera);
                        Bool draw_jones = event < eval.gamma;
                        Bool coherent_jones = eval.valid && !s.depolarized[c] && draw_jones;
                        Bool first_depolarizer = eval.valid && !s.depolarized[c] && !draw_jones;
                        Bool later_jones = eval.valid && s.depolarized[c] && draw_jones;
                        Bool later_depolarizer = eval.valid && s.depolarized[c] && !draw_jones;

                        Jones acc = pollia::unpack_jones(s.pol0[c], s.pol1[c]);
                        Jones next = pollia::multiply(acc, j);
                        Stokes camera = pollia::jones_first_column(acc);
                        Stokes adjoint = pollia::apply_jones_adjoint(j, s.pol1[c]);
                        dr::masked(s.pol0[c], coherent_jones) = pollia::pack_jones_row0(next);
                        dr::masked(s.pol1[c], coherent_jones) = pollia::pack_jones_row1(next);
                        dr::masked(s.pol0[c], first_depolarizer) = camera;
                        dr::masked(s.pol1[c], first_depolarizer) =
                            Stokes(1.f, 0.f, 0.f, 0.f);
                        dr::masked(s.pol1[c], later_jones) = adjoint;
                        dr::masked(s.pol1[c], later_depolarizer) =
                            Stokes(s.pol1[c][0], 0.f, 0.f, 0.f);
                        s.depolarized[c] |= first_depolarizer;
                        dr::masked(s.beta[c], !eval.valid) = 0.f;
                    }
                    s.beta *= reflectance;

                    s.ray = si.spawn_ray(wi_light);
                    s.valid_ray |= si.is_valid();
                    s.vertex = Vertex { Interaction3f(si), bsdf_pdf, false,
                                        s.vertex.depth+1, +RayMask::Secondary };

                    Float rr_prob = dr::minimum(dr::max(s.beta), .95f);
                    Bool rr_active = s.vertex.depth >= m_rr_depth;
                    Bool rr_continue = s.sampler->next_1d() < rr_prob;
                    dr::masked(s.beta, rr_active) *= dr::rcp(dr::detach(rr_prob));
                    s.active = active_next && bsdf_pdf > 0.f &&
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
        return tfm::format("PolliaPISIntegrator[max_depth=%u, rr_depth=%u]",
                           m_max_depth, m_rr_depth);
    }

    MI_DECLARE_CLASS(PolliaPISIntegrator)
};

MI_EXPORT_PLUGIN(PolliaPISIntegrator)
NAMESPACE_END(mitsuba)
